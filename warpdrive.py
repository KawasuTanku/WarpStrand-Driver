#!/usr/bin/env python3
"""warpdrive.py — standalone, rules-driven WarpStrand auto-player (personal use).

Connects to a WarpStrand game server over WebSocket and drives a character
according to a RULES SCRIPT you control. No TUI pane scraping — it speaks the
server protocol directly:

  client -> server: {"line": "<command or login answer>"}
  server -> client: {"ch": "hello"|"prompt"|"room"|"stats"|"line"|"error"|...}

The room graph is DISCOVERED at runtime from each `room` message's `exits` and
PERSISTED to map.<world>.json, so the first run learns the layout and later runs
start with a full map (it keeps updating as you explore).

RULES SCRIPT (default: warpdrive.script next to this file, or --script PATH):
  Each line:  when <cond> [and <cond> ...] do <action> [args...]
  Conditions:  in "Room" | has-stat-points | mob "Name" | hp-below 15 | hp-above 25 | always
  Actions:     train str con dex | goto "Room" | kill "Mob" | retreat "Room" |
               rest | retreat-rest "Room" | say "text" | look
  Lines starting with # are comments. First matching rule wins (firewall style).

Usage:
  python warpdrive.py --host 127.0.0.1 --port 4000 --name KAWASU --password SECRET \
                      --script warpdrive.script
Env fallbacks: WARP_HOST, WARP_PORT, WARP_NAME, WARP_PASS, WARP_SCRIPT.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time

try:
    import websockets
except ImportError:
    sys.exit("This script needs 'websockets': pip install websockets")

# Reverse direction map (for synthesizing symmetric edges from server exits).
REVERSE_DIR = {"n": "s", "s": "n", "e": "w", "w": "e", "u": "d", "d": "u"}


# --------------------------------------------------------------------------
# WorldMap: discovered room graph, persisted to JSON.
# --------------------------------------------------------------------------
class WorldMap:
    def __init__(self, path: str):
        self.path = path
        self.edges = {}        # name -> {dir: neighbor}
        self.current = None
        self._load()

    def _load(self):
        if self.path and os.path.exists(self.path):
            try:
                with open(self.path) as fh:
                    self.edges = json.load(fh)
                print(f"[map] loaded {len(self.edges)} rooms from {self.path}")
            except (json.JSONDecodeError, OSError):
                self.edges = {}

    def save(self):
        if not self.path:
            return
        with open(self.path, "w") as fh:
            json.dump(self.edges, fh, indent=1, sort_keys=True)

    def observe(self, room_msg: dict) -> bool:
        """Update graph from a room message. Returns True if something changed."""
        name = room_msg.get("name")
        if not name:
            return False
        prev_room = self.current
        self.current = name
        ex = {}
        for e in room_msg.get("exits", []):
            d, to = e.get("dir"), e.get("to")
            if d and to:
                ex[d] = to
        # Synthesize the reverse edge: coming FROM prev_room via direction d,
        # the reverse direction from THIS room leads back to prev_room. This
        # repairs asymmetric / mislabeled server exit names so BFS can always
        # navigate back. (MUD rooms are bidirectional.)
        if prev_room and prev_room in self.edges:
            for d, nxt in list(self.edges.get(prev_room, {}).items()):
                if nxt == name:
                    rev = REVERSE_DIR.get(d)
                    if rev and rev not in ex:
                        ex[rev] = prev_room
        changed = name not in self.edges or self.edges[name] != ex
        self.edges[name] = ex
        return changed

    def find_path(self, start: str, goal: str) -> list[str]:
        if not start or not goal or start == goal:
            return []
        # Only require the START room to be known. The GOAL may be an unknown room
        # we've only seen as a neighbor (edge target) — BFS discovers it below.
        if start not in self.edges:
            return []
        prev_dir, prev_room, seen, queue = {}, {}, {start}, [start]
        while queue:
            cur = queue.pop(0)
            if cur == goal:
                break
            for d, nxt in self.edges.get(cur, {}).items():
                if nxt not in seen:
                    seen.add(nxt)
                    prev_room[nxt], prev_dir[nxt] = cur, d
                    queue.append(nxt)
        if goal not in prev_room:
            return []
        out, room = [], goal
        while room != start:
            out.append(prev_dir[room])
            room = prev_room[room]
        out.reverse()
        return out


# --------------------------------------------------------------------------
# Rule parsing
# --------------------------------------------------------------------------
_TOKEN = re.compile(r'"[^"]*"|[\w%-]+')

def _strip_quotes(tok: str) -> str:
    return tok[1:-1] if tok.startswith('"') and tok.endswith('"') else tok

def parse_script(text: str, vars: dict | None = None) -> list[dict]:
    """Parse the rules script into a list of {"conds":[...], "action":(...),}.

    Tokens of the form $NAME (e.g. $mob, $train_room) are substituted from `vars`
    before parsing, so the script can reference the configured mob / train room
    instead of hardcoding them. Unknown $tokens are left as-is.
    """
    if vars:
        # Replace longest tokens first so e.g. $mob_room is substituted before
        # $mob (which is a prefix of it) would otherwise orphan the "_room" tail.
        for k in sorted(vars, key=lambda x: len(x), reverse=True):
            text = text.replace(f"${k}", str(vars[k]))
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^when\s+(.*?)\s+do\s+(.*)$", line, re.IGNORECASE)
        if not m:
            print(f"[script] skipping unparsed line: {line!r}")
            continue
        cond_part, action_part = m.group(1), m.group(2)
        conds = [_strip_quotes(t) for t in _TOKEN.findall(cond_part)]
        acts = [_strip_quotes(t) for t in _TOKEN.findall(action_part)]
        rules.append({"conds": conds, "action": acts})
    return rules


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
class Driver:
    def __init__(self, args, rules, world_map):
        self.host, self.port = args.host, args.port
        self.name, self.password = args.name, args.password
        self.tls = getattr(args, "tls", False)
        self.verify = getattr(args, "verify", True)
        self.delay = getattr(args, "delay", 0) or 0
        self.mob_room = getattr(args, "mob_room", "Cave")
        self.mob = getattr(args, "mob", "Cave Wyrm")
        self.home = getattr(args, "home", "Town Square")
        self.look_interval = getattr(args, "look_interval", 10) or 10
        self.rules, self.map = rules, world_map
        self.world_name = "zen"
        self.stats = {}
        self.creatures = []
        self._authed = False
        self._ws = None
        self._acted = False   # whether a rule fired this tick
        # Movement throttle: once we send a move, don't issue another until the
        # server confirms arrival via a `room` message. Without this, the driver
        # spams moves every tick (faster than the room message round-trips) and
        # thrashes without ever "arriving" anywhere.
        self._pending_move = False
        self._moved_from = None
        self._last_dir = None
        self._move_ts = 0.0   # time the in-flight move was sent (watchdog)
        self._tried = set()   # "room|dir" steps already attempted (exploration)
        self.rest_hp = int(getattr(args, "rest_hp", 95) or 95)  # heal-to threshold (%)
        self._resting_local = False  # we believe we are resting (server confirmed)
        self._resting_room = None    # room we rested in (spurious-echo guard)
        # Training guard: only re-send `train` when the banked stat_points count
        # changes (e.g. after more kills). Stops rule 3 from spamming train every
        # stats tick while sitting home with points already spent.
        self._trained_points = -1
        self._look_task = None
        self._last_look = 0.0
        self._prev_room = None   # room occupied two arrivals ago (used by _moved_from echo guard)
        # Move-then-verify: the room we EXPECT to be in after the in-flight move
        # completes. The driver navigates off its LOCAL map belief, but the map
        # can be wrong (a mislabeled exit, a stale edge learned from a flipped
        # echo). If the server's actual arrival room differs from what we moved
        # toward, we repair the bad edge and re-plan from the TRUE position
        # instead of trusting a divergent belief and looping forever (e.g.
        # thinking we're home in Town Square while the server says we're not, so
        # every `rest` is rejected -> infinite spam loop). Set in _move(),
        # consumed & cleared in the `room` handler.
        self._expected_room = None
        # True once we've issued a move and are awaiting the server's confirming
        # `room` message to validate our navigation (see the `room` handler).
        self._pendVERIFY_armed = False
        # Rest rejection guard: once `rest` is rejected as "not in Town Square"
        # (server disagrees with our local position), stop re-sending it every
        # tick. Only cleared when a `room`/`stats` update proves we really ARE in
        # Town Square, so we don't freeze HP at a low value forever.
        self._rest_rejected = False

    async def send(self, text: str):
        await self._ws.send(json.dumps({"line": text}))
        if self.delay:
            await asyncio.sleep(self.delay)

    async def _respawn_watch(self) -> None:
        """Background task: the server lazy-respawns mobs only when `look` (or a
        move) is issued, and does NOT push a room refresh on respawn. So while we
        sit in the mob's room with the mob absent, periodically `look` to catch
        the respawn and re-engage the kill rule."""
        print(f"[respawn] watcher started: interval={self.look_interval}s "
              f"mob={self.mob!r} room={self.mob_room!r}")
        while True:
            await asyncio.sleep(self.look_interval)
            try:
                if self._ws is None:
                    continue
                if self._pending_move:
                    continue
                if self._resting_local:
                    continue
                if self.map.current != self.mob_room:
                    continue
                names = self._creature_names(self.creatures)
                if self.mob in names:
                    continue  # mob present -> no need to probe
                # Look backoff: don't probe again so soon after any other `look`
                # (e.g. the death handler's immediate post-death probe). The server
                # only lazy-respawns on `look`, and a single `look` per interval is
                # enough to catch it — stacking probes in the death->respawn gap is
                # just redundant spam that can interrupt the kill loop cadence.
                if time.time() - self._last_look < self.look_interval:
                    continue
                print(f"[respawn] mob {self.mob!r} absent in {self.mob_room!r}; "
                      f"creatures seen={names}; sending 'look'")
                await self.send("look")
                self._last_look = time.time()
                print(f"[respawn] looked for {self.mob} in {self.mob_room}")
            except Exception as e:
                print(f"[respawn] error: {e}")

    async def run(self):
        scheme = "wss" if self.tls else "ws"
        uri = f"{scheme}://{self.host}:{self.port}"
        print(f"[connect] {uri} as {self.name}")
        kwargs = {}
        if self.tls:
            import ssl
            if self.verify:
                kwargs["ssl"] = ssl.create_default_context()
            else:
                # Skip certificate verification (self-signed / internal CA).
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kwargs["ssl"] = ctx
        async with websockets.connect(uri, **kwargs) as ws:
            self._ws = ws
            await self.send(self.name)   # server reads name as the first line
            self._look_task = asyncio.create_task(self._respawn_watch())
            try:
                async for raw in ws:
                    try:
                        obj = json.loads(raw) if isinstance(raw, str) else raw
                    except (json.JSONDecodeError, TypeError):
                        continue
                    await self._on_message(obj)
            finally:
                if self._look_task is not None:
                    self._look_task.cancel()
                    self._look_task = None

    async def _on_message(self, obj: dict):
        ch = obj.get("ch")
        if ch == "hello":
            self.world_name = obj.get("world_name") or self.world_name
            # rebind the map file to this world if not yet keyed
            print(f"[hello] world={self.world_name!r} federated={obj.get('federated')}")
        elif ch == "prompt" and obj.get("field") == "secret":
            await self.send(self.password)
            print("[auth] password sent")
        elif ch == "room":
            name = obj.get("name")
            # The server echoes the room we just left BEFORE sending the new one
            # (sometimes duplicated too). If this room is the one we just moved
            # FROM, treat it as a stale echo: unblock the move throttle but do not
            # update our position or re-decide from the wrong room.
            if self._moved_from is not None and name == self._moved_from:
                # This is the room we just LEFT, echoed back by the server before
                # it sends the arrival room. It is NOT our new position, so ignore
                # it entirely. Critically, do NOT clear the move throttle here:
                # clearing it lets _decide() re-fire (on the post-move `stats`
                # push) and double-move before arrival is confirmed, which flips
                # our position belief and produces the endless "rest rejected: not
                # in Town Square" bounce. The throttle is cleared only by the real
                # arrival room (handled further down) or the stale-move watchdog.
                return
            # NOTE: previously a second guard dropped any `room` whose name matched
            # the room we were settled in (a "late echo"). That guard only ever
            # matched SAME-room messages -- exactly the legitimate ones (our own
            # `look` response, and the server's arrival-room confirmation after a
            # move). Dropping them left self.map.current / self.creatures stale,
            # which (a) made the respawn watcher keep firing `look` after leaving
            # the mob room, and (b) made the server-side room_id diverge from the
            # driver's belief so `rest` was rejected ("You can only rest in Town
            # Square") while the driver thought it was home. We now accept same-room
            # room messages: the _moved_from guard above still catches the genuine
            # "room I just left" echo, and the resting guard below still pins us
            # while resting.
            # While we believe we're resting we cannot have moved, so any `room`
            # for a *different* room is a spurious echo (the server can re-send a
            # room from our recent history). Ignoring it stops a phantom flip of
            # our position that would cancel the server heal and bounce us out of
            # Town Square. We pin to the room we actually rested in.
            if self._resting_local and name != self._resting_room:
                return
            changed = self.map.observe(obj)
            self.creatures = obj.get("creatures", []) or []
            if changed:
                self.map.save()
            # If the wire says we're home (Town Square), our local belief now
            # agrees with the server's authoritative position, so clear any prior
            # "not in Town Square" rest-rejection guard. Done up here so it fires
            # on EVERY arrival room message (including the mismatch-repair path
            # below, where we `return` early) — otherwise a just-repaired map
            # would still leave us thinking `rest` was rejected and we'd walk back
            # to the mob half-healed instead of resting.
            if name == self.home:
                self._rest_rejected = False
            # --- Move-then-verify (the fix for the endless rest/spam loop) ---
            # We navigated off our LOCAL map belief. If the room the server says
            # we actually arrived in is NOT the room we expected from that move,
            # our local map has a wrong edge. Repair it now (drop the lying
            # direction from where we moved FROM) so find_path() never believes
            # it again, then re-decide from the TRUE position. Without this we'd
            # keep trusting the bad edge and oscillate (e.g. Town Square<->East
            # Field) forever.
            if self._pendVERIFY_armed and name != self._expected_room:
                from_room = self._moved_from if self._moved_from is not None else self._prev_room
                if from_room and self._last_dir:
                    bad = self.map.edges.get(from_room, {})
                    if bad.get(self._last_dir) == name:
                        del bad[self._last_dir]
                        self.map.save()
                        print(f"[nav] mismatch: moved {self._last_dir} from "
                              f"{from_room!r} but server says we're in {name!r}; "
                              f"repaired bad edge {from_room!r}->{self._last_dir}"
                              f"->{name!r}")
                # We are NOT where we thought; do not treat the stale belief as
                # truth. Re-decide immediately from the real room so the next
                # action uses correct context (and the move throttle is cleared
                # below regardless).
                self._expected_room = None
                self._pendVERIFY_armed = False
                self._pending_move = False
                self._moved_from = None
                await self._decide()
                return
            elif self._pendVERIFY_armed:
                # Arrived exactly where expected: navigation was correct. Clear
                # the verify latch and let the normal arrival bookkeeping run.
                self._expected_room = None
                self._pendVERIFY_armed = False
            # Arrival confirmed: this direction is now a known (tried) branch.
            if self._moved_from is not None and self._last_dir:
                self._tried.add(f"{self._moved_from}|{self._last_dir}")
            # Arrival confirmed: allow the next movement decision.
            self._prev_room = self.map.current
            self._pending_move = False
            self._moved_from = None
            await self._decide()
        elif ch == "stats":
            self.stats = obj
            # The server's stats payload carries the authoritative room name
            # ("room": ...). If the server says we're home, our local belief
            # agrees -> allow `rest` to be (re-)sent by clearing any prior
            # rejection guard. This is what eventually breaks the spam loop
            # once we actually navigate back to Town Square.
            if obj.get("room") == self.home:
                self._rest_rejected = False
            # HP/stat view changed (e.g. the server's per-second `rest` heal tick).
            # Re-evaluate rules so rest-completion -> leave triggers promptly at the
            # configured $rest_hp threshold instead of stalling at 100%. The move
            # throttle (_pending_move) still blocks re-deciding mid-move, and
            # clearing _decided_room prevents the same-room de-dupe from suppressing
            # a re-check when only HP changed while standing still.
            if self._hp_ratio() * 100 >= self.rest_hp:
                # Healed enough (or the server stopped resting) -- allow re-engage.
                self._resting_local = False
            self._decided_room = None
            await self._decide()
        elif ch == "error":
            print(f"[error] {obj.get('text')}")
        elif ch == "line":
            text = obj.get("text") or ""
            if not text:
                return
            # Loot / drops arrive as narration lines (the server broadcasts kill
            # results and roll_mob_loot() strings as `line` messages). Surface them
            # so the driver's operator can see what was picked up. Lines that look
            # like loot/drops are tagged [loot]; other narration is shown as [server]
            # only when it mentions a reward, so routine flavor text stays quiet.
            low = text.lower()
            LOOT_WORDS = ("loot", "drop", "gain", "receive", "found", "obtain",
                          "xp", "gold", "you get", "picked up")
            if "dies" in low or "slain" in low or "slay" in low:
                print(f"[kill] {text}")
                # The server does NOT push a `room` refresh on death -- only on
                # an explicit look/move -- so self.creatures keeps the dead mob
                # forever. That makes the respawn watcher log "already present"
                # every tick (never re-probes) and lets the kill rule re-fire on
                # a corpse. Drop the dead mob(s) from the local list now so the
                # watcher can re-detect the respawn via its periodic look.
                removed = []
                keep = []
                for c in self.creatures:
                    name = c.get("name") if isinstance(c, dict) else (
                        c if isinstance(c, str) else "")
                    if name and name.lower() in low:
                        removed.append(name)
                    else:
                        keep.append(c)
                if removed:
                    self.creatures = keep
                    print(f"[respawn] {', '.join(removed)} removed from local "
                          f"list on death; watcher will re-probe")
                    # Probe immediately instead of waiting up to look_interval s:
                    # send 'look' so the server's lazy respawn can surface the mob
                    # (the room handler now accepts our own look response). Only if
                    # we're actually in the mob room.
                    if self.map.current == self.mob_room and self._ws is not None:
                        await self.send("look")
                        self._last_look = time.time()
                        print(f"[respawn] immediate 'look' after death in "
                              f"{self.mob_room!r}")
            elif "shard" in low:
                # Capacity Shard is a special/universal drop (the only way to
                # expand the storage ring) -- highlight it distinctly.
                print(f"[shard] {text}")
            elif any(w in low for w in LOOT_WORDS):
                print(f"[loot] {text}")
            else:
                # Rest-state feedback from the server. The driver used to set
                # _resting_local on SEND and swallow these lines, so a rejected
                # `rest` (e.g. server-side room_id != Town Square) looked like
                # "resting" while HP never climbed. Now we trust the server's own
                # words: only the confirmation flips _resting_local on; rejections
                # are surfaced so the operator can see why healing didn't start.
                if "sit down to rest" in low:
                    self._resting_local = True
                    self._resting_room = self.map.current
                    print(f"[rest] confirmed: {text}")
                elif "already resting" in low:
                    self._resting_local = True
                    print(f"[rest] {text}")
                elif "can only rest in town square" in low:
                    self._resting_local = False
                    self._rest_rejected = True
                    # We believe we're home but the server's room_id says otherwise.
                    # Rather than freeze (rule 2 keeps matching on the stale local
                    # belief and our reject-guard blocks `rest`), force a fresh
                    # `room` payload from the server. That is authoritative and
                    # resyncs self.map.current, so the next decide() navigates from
                    # where we REALLY are instead of looping on a wrong belief.
                    if self._ws is not None and not self._pending_move:
                        await self.send("look")
                        print(f"[rest] REJECTED -> forcing 'look' to resync "
                              f"position (was {self.map.current!r})")
                    print(f"[rest] REJECTED (not in Town Square): {text}")
                elif "already at full hp" in low:
                    self._resting_local = False
                    print(f"[rest] {text}")

    @staticmethod
    def _creature_names(creatures) -> list[str]:
        """Return mob names whether the server sends a list of dicts
        ({"name": ...}) or a list of plain strings."""
        names = []
        for c in creatures or []:
            if isinstance(c, dict):
                n = c.get("name")
                if n:
                    names.append(n)
            elif isinstance(c, str):
                names.append(c)
        return names

    def _cond(self, tok: str) -> bool:
        if tok == "always":
            return True
        if tok == "has-stat-points":
            return int(self.stats.get("stat_points", 0) or 0) > 0
        if tok == "mob":
            return False  # handled with the name below
        if tok == "in":
            return False  # handled with the name below
        if tok == "hp-below":
            return self._hp_ratio() * 100 < float(self._next_of(tok))
        if tok == "hp-above":
            return self._hp_ratio() * 100 >= float(self._next_of(tok))
        return False

    def _next_of(self, tok: str) -> str:
        # small helper: find the value token that follows `tok` in conds
        try:
            i = self._active_conds.index(tok)
            return self._active_conds[i + 1]
        except (ValueError, IndexError):
            return ""

    def _hp_ratio(self) -> float:
        hp, mh = self.stats.get("hp"), self.stats.get("maxhp")
        if not hp or not mh:
            # Unknown HP: assume we're healthy so survival rules (rest/retreat)
            # don't fire on a stale empty state at spawn -- we wait for the real
            # stats packet (the server sends one on entry) before deciding to
            # pull out of the mob room or rest. Returning 0.0 here previously made
            # the driver retreat from the mob room at 88% the instant it logged in
            # (before stats loaded), which is wrong: it should fight, not flee.
            return 1.0
        try:
            return float(hp) / float(mh)
        except (TypeError, ValueError):
            return 1.0

    def _explore_direction(self) -> str | None:
        """Pick a direction that expands our knowledge of the map toward the goal.

        Systematic exploration so we never get stuck bouncing in a dead-end branch:
          1. Among the current room's neighbors we have NOT yet visited, prefer one
             whose destination is still an unknown room (unmapped). Skip a direction
             we already stepped from here, and skip an immediate reverse of the last
             move (so we don't ping-pong between two rooms).
          2. If every neighbor here is already mapped, BFS the KNOWN graph to the
             nearest room that still has an unexplored exit (a "frontier") and step
             toward it. This expands outward until the goal becomes reachable.
          3. Fallback: any exit we haven't tried yet, else None.
        Returns None if we have no exits at all.
        """
        cur = self.map.current
        exits = self.map.edges.get(cur, {})
        if not exits:
            return None
        reverse = REVERSE_DIR.get(self._last_dir)

        # 1) unexplored / untried neighbor, preferring a genuinely unknown room
        candidates = []
        for d, nxt in exits.items():
            if f"{cur}|{d}" in self._tried:
                continue
            if nxt not in self.map.edges:
                # unknown room -> top priority (expands the map)
                candidates.insert(0, d)
            else:
                candidates.append(d)
        if candidates:
            # avoid immediately reversing the last move unless it's the only choice
            pick = candidates[0]
            if reverse and len(candidates) > 1 and pick == reverse:
                pick = candidates[1]
            return pick

        # 2) all neighbors here are mapped -> head to the nearest frontier
        step = self._frontier_step()
        if step:
            return step

        # 3) fallback: any untried exit
        for d in exits:
            if f"{cur}|{d}" not in self._tried:
                return d
        return None

    def _frontier_step(self) -> str | None:
        """BFS known edges from current room to the nearest room that has an exit
        leading to an as-yet-unknown room; return the first direction to take."""
        start = self.map.current
        prev: dict[str, tuple[str, str]] = {}
        seen = {start}
        q = [start]
        while q:
            r = q.pop(0)
            for d, nxt in self.map.edges.get(r, {}).items():
                if nxt in seen:
                    continue
                seen.add(nxt)
                prev[nxt] = (r, d)
                # Is nxt a frontier? Either it has an exit to a room we haven't
                # mapped yet, OR we've never actually arrived there (empty edges),
                # so stepping toward it expands the map. This is what lets the
                # driver discover the Cave branch after a map wipe instead of
                # bouncing between already-mapped dead-ends.
                nxt_edges = self.map.edges.get(nxt)
                if nxt_edges is None or not nxt_edges or any(
                        e not in self.map.edges for e in nxt_edges.values()):
                    room = nxt
                    path = []
                    while room != start:
                        pr, pd = prev[room]
                        path.append(pd)
                        room = pr
                    return path[-1] if path else None
                q.append(nxt)
        return None

    async def _decide(self):
        if not self.map.current:
            return
        if self._pending_move:
            # We already issued a move; wait for the server's `room` message to
            # confirm arrival before deciding the next action (prevents move-spam).
            # Watchdog: if no arrival room has shown up in a long time (the packet
            # was dropped), free the throttle so we don't deadlock forever. A real
            # arrival resets _pending_move well within this window.
            if time.time() - self._move_ts > 5.0:
                self._pending_move = False
                self._moved_from = None
                self._pendVERIFY_armed = False
                print("[move] watchdog: no arrival within 5s; releasing throttle")
            return
        if not self._authed:
            self._authed = True
            print(f"[in-world] at {self.map.current!r}")
        hp, mh = self.stats.get("hp"), self.stats.get("maxhp")
        loc = f"[loc] {self.map.current}"
        if hp is not None and mh is not None:
            loc += f"  HP {hp}/{mh} ({int(self._hp_ratio()*100)}%)"
        if self.creatures:
            loc += f"  creatures={self._creature_names(self.creatures)}"
        print(loc)
        for rule in self.rules:
            conds = rule["conds"]
            self._active_conds = conds
            # Evaluate: pairs of (keyword, [value]); support `and` between them.
            ok = True
            i = 0
            while i < len(conds):
                kw = conds[i].lower()
                if kw == "and":
                    i += 1
                    continue
                if kw == "always":
                    ok = True
                elif kw == "has-stat-points":
                    ok = ok and int(self.stats.get("stat_points", 0) or 0) > 0
                elif kw == "hp-below":
                    i += 1
                    ok = ok and self._hp_ratio() * 100 < float(conds[i])
                elif kw == "hp-above":
                    i += 1
                    ok = ok and self._hp_ratio() * 100 >= float(conds[i])
                elif kw == "in":
                    i += 1
                    ok = ok and self.map.current == conds[i]
                elif kw == "mob":
                    i += 1
                    ok = ok and conds[i] in self._creature_names(self.creatures)
                else:
                    ok = False
                i += 1
            if ok:
                await self._run_action(rule["action"])
                return   # first match wins

    async def _move(self, direction: str):
        """Send a movement command and record bookkeeping for throttle/explore."""
        await self.send(direction)
        self._pending_move = True
        self._resting_local = False   # moving interrupts rest (server-side too)
        self._moved_from = self.map.current
        self._last_dir = direction
        # Arm move-then-verify: remember where we EXPECT to land per the local
        # map, so the `room` handler can detect a divergence (wrong edge) and
        # repair it. This is the core guard against the infinite navigation/
        # rest loop.
        self._expected_room = self.map.edges.get(self.map.current, {}).get(direction)
        self._pendVERIFY_armed = True
        self._move_ts = time.time()

    async def _run_action(self, acts: list[str]):
        verb = acts[0].lower() if acts else "look"
        if verb == "train":
            have = int(self.stats.get("stat_points", 0) or 0)
            if have == self._trained_points:
                return  # already trained this bank; wait for more points
            for stat in acts[1:]:
                await self.send(f"train {stat}")
            self._trained_points = have
            print(f"[act] train {acts[1:]} ({have} pts)")
        elif verb == "goto" or verb == "retreat":
            # Never walk away while we believe we're resting: leaving cancels the
            # server's rest (and its heal), and we'd just bounce back — freezing HP.
            if self._resting_local:
                return
            dest = acts[1] if len(acts) > 1 else None
            if not dest:
                return
            dirs = self.map.find_path(self.map.current, dest)
            if dirs:
                await self._move(dirs[0])
                print(f"[act] {verb} -> {dest} ({dirs[0]})")
            elif self.map.current == dest:
                # Already at the destination; do nothing (don't "explore" away
                # from it — that caused an infinite Cave<->neighbor loop).
                print(f"[act] {verb} -> {dest}: already here")
                return
            else:
                # No known path yet. `look` would teach us nothing (the server
                # only sends a `room` message on actual movement), so step through
                # an exit whose destination we haven't fully mapped. This is how
                # the driver discovers the world: walk, learn, repeat, until the
                # goal becomes reachable.
                step = self._explore_direction()
                if step:
                    await self._move(step)
                    print(f"[act] {verb} -> {dest}: exploring ({step})")
                else:
                    # Truly stuck (no exits at all from here): look as a fallback.
                    await self.send("look")
                    print(f"[act] {verb} -> {dest}: no path, no exits; looking")
        elif verb == "kill":
            # Don't start a fight while resting (combat cancels the server heal).
            if self._resting_local:
                return
            target = acts[1] if len(acts) > 1 else None
            if target:
                await self.send(f"kill {target}")
                print(f"[act] kill {target}")
        elif verb == "rest" or verb == "retreat-rest":
            # `rest` tells the server to begin recovering HP (server feature).
            # `retreat-rest` is sugar: go to <room> first, then rest once there.
            if verb == "retreat-rest" and len(acts) > 1:
                dest = acts[1]
                if self.map.current == dest:
                    # Already home: don't call find_path (it can spuriously return
                    # a path for the current room and make us walk AWAY instead of
                    # resting -- which bounces Town Square <-> neighbors forever and
                    # never actually heals). Fall straight through to rest.
                    pass
                else:
                    dirs = self.map.find_path(self.map.current, dest)
                    if dirs:
                        await self._move(dirs[0])
                        print(f"[act] retreat-rest -> heading to {dest} ({dirs[0]})")
                        return
                    # no path yet: step toward it (explore) instead of resting
                    step = self._explore_direction()
                    if step:
                        await self._move(step)
                        print(f"[act] retreat-rest -> heading to {dest}: exploring ({step})")
                        return
                # already at dest (or no path/exits): fall through to rest
            # Don't re-send `rest` every tick: the server heals on its own task once
            # resting begins; spamming `rest` is harmless but noisy. We only send
            # once, then wait for the server's confirmation line ("sit down to
            # rest...") which sets _resting_local — so a REJECTED rest (e.g. we're
            # not actually in Town Square server-side) does NOT leave us believing
            # we're resting while HP stays frozen.
            if self._resting_local:
                return
            # If a prior `rest` was rejected because the server's position disagreed
            # with our local belief (we thought we were home but weren't), don't
            # spam `rest` every tick into a wall. Only retry once a `room`/`stats`
            # message proves we really are in Town Square (which clears
            # _rest_rejected). This breaks the infinite "REJECTED ... not in Town
            # Square" loop from the bug report.
            if self._rest_rejected and self.map.current == self.home:
                return
            await self.send("rest")
            print("[act] rest (sent; waiting for server confirm)")
        elif verb == "say":
            msg = " ".join(acts[1:])
            await self.send(f"say {msg}")
            print(f"[act] say {msg}")
        else:
            await self.send(verb)
            print(f"[act] {verb}")


def _load_config(path: str | None) -> dict:
    """Load client settings from YAML (same schema as the TUI client's
    ~/.config/warpstrand/client.yaml: host/port/name/password, optional
    mob/train_room/hp_floor/script). Self-contained: uses PyYAML only, no
    dependency on the WarpStrand-Client package."""
    import yaml
    if path is None:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
        path = os.path.join(base, "warpstrand", "client.yaml")
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_args():
    p = argparse.ArgumentParser(description="WarpStrand rules-driven auto-player")
    p.add_argument("--host", default=os.getenv("WARP_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("WARP_PORT", "4000")))
    p.add_argument("--name", default=os.getenv("WARP_NAME", ""))
    p.add_argument("--password", default=os.getenv("WARP_PASS", ""))
    p.add_argument("--mob", default=os.getenv("WARP_MOB", "Cave Wyrm"))
    p.add_argument("--mob-room", default=os.getenv("WARP_MOB_ROOM", "Cave"),
                   help="Room the target mob lives in (default 'Cave').")
    p.add_argument("--home", default=os.getenv("WARP_HOME", "Town Square"),
                   help="Rest / retreat destination (default 'Town Square').")
    p.add_argument("--train-room", default=os.getenv("WARP_TRAIN_ROOM", "Town Square"))
    p.add_argument("--rest-hp", type=int, default=int(os.getenv("WARP_REST_HP", "95")),
                   help="Rest in Town Square until HP reaches this percent (default 95).")
    p.add_argument("--retreat-hp", type=int, default=int(os.getenv("WARP_RETREAT_HP", "50")),
                   help="When HP drops below this percent and we're NOT home, retreat "
                        "to $home and heal (default 50). Keeps the driver from grinding "
                        "in the mob room down to single digits before healing.")
    p.add_argument("--look-interval", type=int, default=int(os.getenv("WARP_LOOK_INTERVAL", "10")),
                   help="Seconds between 'look' probes in the mob room to catch respawns "
                        "(server doesn't push a room refresh on respawn). Default 10.")
    p.add_argument("--hp-floor", type=float, default=float(os.getenv("WARP_HP_FLOOR", "0.25")))
    p.add_argument("--tls", action="store_true",
                   help="Connect over wss:// (TLS). Reads 'tls' from client.yaml if set.")
    p.add_argument("--no-verify", dest="verify", action="store_false",
                   help="Do not verify the TLS certificate (self-signed / internal CA).")
    here = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("--script", default=os.getenv("WARP_SCRIPT",
                                                  os.path.join(here, "warpdrive.script")))
    p.add_argument("--delay", type=float, default=float(os.getenv("WARP_DELAY", "0")),
                   help="Seconds to wait after each command sent (pace a server "
                        "that drops rapid commands). Default 0.")
    p.add_argument("--config", default=None,
                   help="YAML config (default: ~/.config/warpstrand/client.yaml)")
    args = p.parse_args()

    # Merge credentials from client.yaml.
    # Precedence: CLI arg (if explicitly set) > yaml > env > built-in default.
    cfg = _load_config(args.config)
    if not args.tls:
        args.tls = bool(cfg.get("tls", False))
    if args.verify and not cfg.get("verify", True):
        args.verify = False
    if args.host == os.getenv("WARP_HOST", "127.0.0.1"):
        args.host = cfg.get("host", args.host)
    if args.port == int(os.getenv("WARP_PORT", "4000")):
        args.port = int(cfg.get("port", args.port))
    if not args.name:
        args.name = cfg.get("name", "")
    if not args.password:
        args.password = cfg.get("password", "")
    if args.mob == os.getenv("WARP_MOB", "Cave Wyrm"):
        args.mob = cfg.get("mob", args.mob)
    if args.mob_room == os.getenv("WARP_MOB_ROOM", "Cave"):
        args.mob_room = cfg.get("mob_room", args.mob_room)
    if args.home == os.getenv("WARP_HOME", "Town Square"):
        args.home = cfg.get("home", args.home)
    if args.train_room == os.getenv("WARP_TRAIN_ROOM", "Town Square"):
        args.train_room = cfg.get("train_room", args.train_room)
    if args.rest_hp == int(os.getenv("WARP_REST_HP", "95")):
        args.rest_hp = int(cfg.get("rest_hp", args.rest_hp))
    if args.retreat_hp == int(os.getenv("WARP_RETREAT_HP", "50")):
        args.retreat_hp = int(cfg.get("retreat_hp", args.retreat_hp))
    if args.look_interval == int(os.getenv("WARP_LOOK_INTERVAL", "10")):
        args.look_interval = int(cfg.get("look_interval", args.look_interval))
    if args.hp_floor == float(os.getenv("WARP_HP_FLOOR", "0.25")):
        args.hp_floor = float(cfg.get("hp_floor", args.hp_floor))
    if args.script == os.getenv("WARP_SCRIPT",
                                os.path.join(here, "warpdrive.script")):
        args.script = cfg.get("script", args.script)
    return args


def _dump_config(args, map_path: str) -> None:
    """Print the resolved startup configuration so the operator can verify what
    was actually loaded (CLI > yaml > env > built-in default merge in
    parse_args). The password is masked: we only reveal whether it resolved to a
    non-empty value, never the secret itself."""
    def mask(v):
        if v is None or v == "":
            return "<unset>"
        return "*" * max(6, min(12, len(str(v))))
    rows = [
        ("host", args.host),
        ("port", args.port),
        ("name", args.name),
        ("password", mask(args.password)),
        ("mob", args.mob),
        ("mob_room", args.mob_room),
        ("home", args.home),
        ("train_room", args.train_room),
        ("rest_hp", f"{args.rest_hp}%"),
        ("retreat_hp", f"{args.retreat_hp}%"),
        ("look_interval", f"{args.look_interval}s"),
        ("hp_floor", args.hp_floor),
        ("tls", args.tls),
        ("verify", args.verify),
        ("delay", f"{args.delay}s"),
        ("script", args.script),
        ("map_path", map_path),
    ]
    w = max(len(k) for k, _ in rows)
    print("[config] resolved startup variables:")
    for k, v in rows:
        print(f"  {k.ljust(w)} = {v}")


def _dump_rules(rules) -> None:
    """Print the parsed rule list (order = evaluation order = firewall
    priority) so the operator can verify the conditions/actions that will
    actually drive behavior. First match wins, so ordering IS the priority —
    a rule placed above another overrides it for any state both would match."""
    print(f"[config] {len(rules)} rules loaded (evaluated top-to-bottom, "
          f"first match wins):")
    for i, r in enumerate(rules, 1):
        conds = " ".join(r["conds"])
        action = " ".join(r["action"])
        print(f"  {i:>2}. when {conds} do {action}")


async def main():
    args = parse_args()
    if not args.name or not args.password:
        sys.exit("Need --name and --password (or WARP_NAME / WARP_PASS).")
    if not os.path.exists(args.script):
        sys.exit(f"No script at {args.script}")
    with open(args.script) as fh:
        rules = parse_script(fh.read(), vars={
            "mob": args.mob,
            "mob_room": args.mob_room,
            "home": args.home,
            "train_room": args.train_room,
            "rest_hp": args.rest_hp,
            "retreat_hp": args.retreat_hp,
        })
    if not rules:
        sys.exit("Script parsed to zero rules.")
    print(f"[script] {len(rules)} rules loaded")
    # Map file keyed by world name once we learn it; start with a temp name.
    map_path = os.path.join(os.path.dirname(os.path.abspath(args.script)),
                            f"map.{args.name}.json")
    wm = WorldMap(map_path)
    _dump_config(args, map_path)
    _dump_rules(rules)
    await Driver(args, rules, wm).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Ctrl-C cancels the `async for` over the socket; asyncio.run then
        # re-raises KeyboardInterrupt. Swallow it like the server's run() does
        # so we exit cleanly instead of dumping a stack trace.
        print("\n[stopped] interrupted (Ctrl-C)")
