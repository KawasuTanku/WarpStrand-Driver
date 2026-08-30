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
        for k, v in vars.items():
            text = text.replace(f"${k}", str(v))
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
        self._tried = set()   # "room|dir" steps already attempted (exploration)

    async def send(self, text: str):
        await self._ws.send(json.dumps({"line": text}))
        if self.delay:
            await asyncio.sleep(self.delay)

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
            async for raw in ws:
                try:
                    obj = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    continue
                await self._on_message(obj)

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
                self._pending_move = False
                self._moved_from = None
                return
            changed = self.map.observe(obj)
            self.creatures = obj.get("creatures", []) or []
            if changed:
                self.map.save()
            # Arrival confirmed: allow the next movement decision.
            self._pending_move = False
            self._moved_from = None
            await self._decide()
        elif ch == "stats":
            # Update HP/stat view, but do NOT re-decide here. Decisions only run
            # on `room` messages (authoritative position). Acting on stats let the
            # driver issue commands against a stale `current` room when a stats
            # packet arrived before the room confirmation — causing it to send a
            # move for the wrong room and drift from the server's real position.
            self.stats = obj
        elif ch == "error":
            print(f"[error] {obj.get('text')}")
        elif ch == "line":
            pass  # narration; ignored

    # --- condition + action helpers ---
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
                # Is nxt a frontier (has an exit to an unknown room)?
                if any(e not in self.map.edges
                       for e in self.map.edges.get(nxt, {}).values()):
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
        self._moved_from = self.map.current
        self._last_dir = direction
        self._tried.add(f"{self.map.current}|{direction}")

    async def _run_action(self, acts: list[str]):
        verb = acts[0].lower() if acts else "look"
        if verb == "train":
            for stat in acts[1:]:
                await self.send(f"train {stat}")
            print(f"[act] train {acts[1:]}")
        elif verb == "goto" or verb == "retreat":
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
            target = acts[1] if len(acts) > 1 else None
            if target:
                await self.send(f"kill {target}")
                print(f"[act] kill {target}")
        elif verb == "rest" or verb == "retreat-rest":
            # `rest` tells the server to begin recovering HP (server feature).
            # `retreat-rest` is sugar: go to <room> first, then rest once there.
            if verb == "retreat-rest" and len(acts) > 1:
                dest = acts[1]
                dirs = self.map.find_path(self.map.current, dest)
                if dirs:
                    await self._move(dirs[0])
                    print(f"[act] retreat-rest -> heading to {dest} ({dirs[0]})")
                    return
                # already at dest (or no path): fall through to rest
            await self.send("rest")
            print("[act] rest (recover HP)")
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
    p.add_argument("--train-room", default=os.getenv("WARP_TRAIN_ROOM", "Town Square"))
    p.add_argument("--rest-hp", type=int, default=int(os.getenv("WARP_REST_HP", "95")),
                   help="Rest in Town Square until HP reaches this percent (default 95).")
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
    if args.train_room == os.getenv("WARP_TRAIN_ROOM", "Town Square"):
        args.train_room = cfg.get("train_room", args.train_room)
    if args.rest_hp == int(os.getenv("WARP_REST_HP", "95")):
        args.rest_hp = int(cfg.get("rest_hp", args.rest_hp))
    if args.hp_floor == float(os.getenv("WARP_HP_FLOOR", "0.25")):
        args.hp_floor = float(cfg.get("hp_floor", args.hp_floor))
    if args.script == os.getenv("WARP_SCRIPT",
                                os.path.join(here, "warpdrive.script")):
        args.script = cfg.get("script", args.script)
    return args


async def main():
    args = parse_args()
    if not args.name or not args.password:
        sys.exit("Need --name and --password (or WARP_NAME / WARP_PASS).")
    if not os.path.exists(args.script):
        sys.exit(f"No script at {args.script}")
    with open(args.script) as fh:
        rules = parse_script(fh.read(), vars={
            "mob": args.mob,
            "train_room": args.train_room,
            "rest_hp": args.rest_hp,
        })
    if not rules:
        sys.exit("Script parsed to zero rules.")
    print(f"[script] {len(rules)} rules loaded")
    # Map file keyed by world name once we learn it; start with a temp name.
    map_path = os.path.join(os.path.dirname(os.path.abspath(args.script)),
                            f"map.{args.name}.json")
    wm = WorldMap(map_path)
    await Driver(args, rules, wm).run()


if __name__ == "__main__":
    asyncio.run(main())
