#!/usr/bin/env python3
"""Regression test: move-then-verify must break the endless rest/spam loop.

Reproduces the failure in ~/output.log:

  The driver's local map had a WRONG edge:
      Town Square --e--> East Field   (believed)
  but the server's authoritative graph is:
      Town Square --e--> West River    (real)

  Old behavior: the driver believes it is home (Town Square) and rule 2
  ("in Town Square and hp-below 95 -> rest") fires, but the server's room_id
  is NOT Town Square, so `rest` is rejected ("You can only rest in Town
  Square"). The driver never reconciles its position, so it spam-loops `rest`
  every tick with HP frozen at 44% (see the ~350 identical REJECTED lines).

  New behavior (move-then-verify + rest-rejection guard + look-on-reject):
    * every move arms a verify latch; a divergence between the expected room
      and the server's actual arrival room repairs the bad edge;
    * a `rest` rejection forces a `look` so the authoritative `room` payload
      resyncs self.map.current, and the reject-guard blocks re-spamming `rest`
      until the server confirms we're home;
    * the result: the driver navigates to the REAL Town Square and actually
      rests / heals instead of freezing.

The harness drives the REAL Driver machinery with a mocked websocket transport
and a faithful server-protocol emulator built from WarpStrand-Server:

  * room id 1 == "Town Square"; `rest` rejects unless room_id == 1.
  * a successful move ends with a fresh `room` payload (server._move -> _look).
  * exits are server-authoritative; the driver's local map may disagree.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warpdrive  # noqa: E402


# --- Server-authoritative world (matches real server graph) -------------------
# room id -> (name, {dir: neighbor_room_id})
WORLD = {
    1: ("Town Square", {"e": 2, "w": 5}),   # e -> West River (the REAL edge)
    2: ("West River", {"w": 1, "n": 3}),
    3: ("North Road", {"s": 2, "n": 4}),
    4: ("Forest", {"s": 3, "n": 6}),
    5: ("East Field", {"w": 1}),
    6: ("Cave", {"s": 4}),
}
HOME_ID = 1
HOME_NAME = "Town Square"
DIR_NAMES = {"n": "north", "s": "south", "e": "east", "w": "west"}


class FakeServer:
    """Emulates the parts of WarpStrand-Server the driver depends on."""

    def __init__(self):
        self.room_id = HOME_ID
        self.hp = 431
        self.maxhp = 960
        self._resting = False
        self.outbox = []

    def _room_payload(self, room_id):
        name, exits = WORLD[room_id]
        return {
            "ch": "room",
            "name": name,
            "description": f"You are in {name}.",
            "exits": [{"dir": d, "name": DIR_NAMES[d], "to": WORLD[nid][0]}
                      for d, nid in exits.items()],
            "here": [],
            "creatures": [],
        }

    def _push_stats(self):
        name, _ = WORLD[self.room_id]
        self.outbox.append(json.dumps({
            "ch": "stats",
            "hp": self.hp,
            "maxhp": self.maxhp,
            "stat_points": 0,
            "room": name,
        }))

    def handle_line(self, line):
        cmd = line.strip().lower()
        if cmd in ("n", "s", "e", "w"):
            d = cmd
            _, exits = WORLD[self.room_id]
            if d in exits:
                self.room_id = exits[d]
                # A successful move ends with a fresh room payload + stats,
                # exactly like server._move -> _look() -> _push_stats().
                self.outbox.append(json.dumps(self._room_payload(self.room_id)))
                self._push_stats()
        elif cmd in ("look", "l"):
            self.outbox.append(json.dumps(self._room_payload(self.room_id)))
            self._push_stats()
        elif cmd == "rest":
            if self.room_id != HOME_ID:
                self.outbox.append(json.dumps({
                    "ch": "line",
                    "text": "You can only rest in Town Square.",
                }))
            elif self.hp >= self.maxhp:
                self.outbox.append(json.dumps({
                    "ch": "line",
                    "text": "You're already at full HP.",
                }))
            else:
                self._resting = True
                self.outbox.append(json.dumps({
                    "ch": "line",
                    "text": "You sit down to rest in Town Square. HP will recover each second.",
                }))

    def drain(self):
        msgs = self.outbox
        self.outbox = []
        return msgs


class Harness:
    def __init__(self):
        script_path = os.path.join(os.path.dirname(__file__), "warpdrive.script")
        self.rules = warpdrive.parse_script(
            open(script_path).read(),
            vars={
                "mob": "Cave Wyrm",
                "mob_room": "Cave",
                "home": HOME_NAME,
                "train_room": HOME_NAME,
                "rest_hp": 95,
                "retreat_hp": 50,
            },
        )
        map_path = "/tmp/map.test_nav_verify.json"
        if os.path.exists(map_path):
            os.remove(map_path)
        m = warpdrive.WorldMap(map_path)
        # Seed the BAD edge from the bug report: driver believes
        # Town Square --e--> East Field, but the server says --e--> West River.
        m.edges = {
            HOME_NAME: {"e": "East Field", "w": "West River"},
            "East Field": {"w": HOME_NAME},
            "West River": {"e": HOME_NAME, "n": "North Road"},
            "North Road": {"s": "West River", "n": "Forest"},
            "Forest": {"s": "North Road", "n": "Cave"},
            "Cave": {"s": "Forest"},
        }
        m.current = HOME_NAME
        self.map = m
        self.server = FakeServer()
        import argparse
        args = argparse.Namespace(
            host="x", port=1, name="KawasuTanku", password="x",
            mob="Cave Wyrm", mob_room="Cave", home=HOME_NAME,
            train_room=HOME_NAME, rest_hp=95, retreat_hp=50,
            look_interval=10, hp_floor=0.25, tls=False, verify=True,
            delay=0, script=script_path,
        )
        self.driver = warpdrive.Driver(args, self.rules, m)
        self.driver._ws = self  # mock transport
        self.sent_lines = []

    async def send(self, text):
        obj = json.loads(text)
        line = obj.get("line", "")
        self.sent_lines.append(line)
        self.server.handle_line(line)

    async def feed(self, raw):
        await self.driver._on_message(json.loads(raw))

    async def pump(self, max_ticks=400):
        for _ in range(max_ticks):
            for raw in self.server.drain():
                await self.feed(raw)
            # Heal simulation: if the server started resting, deliver the heal.
            if self.server._resting:
                self.server.hp = self.server.maxhp  # healed to full
                self.server._resting = False
                self.server._push_stats()
                for raw in self.server.drain():
                    await self.feed(raw)
                return True  # healed
            # Terminal: reached Town Square server-side and map reconciles home.
            if self.server.room_id == HOME_ID and self.map.current == HOME_NAME:
                if self.rest_count() >= 3 and not self.server._resting:
                    # already sent several rests without healing -> still stuck?
                    pass
        return False

    def rest_count(self):
        return sum(1 for l in self.sent_lines if l == "rest")


async def run():
    h = Harness()
    assert h.map.edges[HOME_NAME]["e"] == "East Field", "seed bad edge missing"
    print("[test] seeded bad map edge Town Square --e--> East Field "
          "(server says -> West River)")

    # Simulate the bug's starting divergence: the driver BELIEVES it is home
    # (Town Square) but the server's authoritative room_id is West River (the
    # real position after the retreat). Feed the authoritative room + stats.
    h.server.room_id = 2  # West River (server truth)
    await h.feed(json.dumps(h.server._room_payload(2)))
    h.server._push_stats()
    for raw in h.server.drain():
        await h.feed(raw)
    print(f"[test] after sync: driver.map.current={h.driver.map.current!r} "
          f"server_room={WORLD[h.server.room_id][0]!r} "
          f"rest_rejected={h.driver._rest_rejected}")

    healed = await h.pump(max_ticks=400)

    bad_edge = h.map.edges.get(HOME_NAME, {}).get("e") == "East Field"
    print(f"[test] rest commands issued total: {h.rest_count()}")
    print(f"[test] bad edge still present: {bad_edge}")
    print(f"[test] server final room: {WORLD[h.server.room_id][0]} "
          f"hp={h.server.hp}/{h.server.maxhp} healed={healed}")

    # 1) The driver must NOT spam `rest` endlessly. In the original log it sent
    #    one `rest` per tick for ~350 ticks. We bound it well under that.
    assert h.rest_count() < 15, (
        f"Driver spammed `rest` {h.rest_count()} times (infinite-loop symptom). "
        f"It should reconcile position and rest at most a few times.")

    # 2) The bad map edge must be repaired by move-then-verify (the driver must
    #    have moved 'e' at least once and learned the real neighbor West River).
    assert not bad_edge, (
        "Bad map edge Town Square->East Field was NOT repaired; navigation "
        "would keep diverging.")

    # 3) The driver must actually heal (proving it reached the REAL Town Square
    #    and rested) — i.e. the loop is broken, not just suppressed.
    assert healed, (
        "Driver never reached a resting/healed state; position belief did not "
        "reconcile to the server's authoritative Town Square.")

    print("[test] PASS: no infinite rest loop; bad edge repaired; "
          "position reconciled; HP healed.")


if __name__ == "__main__":
    asyncio.run(run())
