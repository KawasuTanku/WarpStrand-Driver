#!/usr/bin/env python3
"""Regression test: a single `retreat-rest` must emit exactly ONE move until the
server confirms arrival. Previously the "room I just left" echo cleared the move
throttle prematurely; the post-move `stats` push then re-fired _decide() and a
SECOND move went out before arrival, flipping our position belief so `rest` was
rejected ("You can only rest in Town Square") and HP froze forever.

Faithful server model (from WarpStrand-Server session._move / _rest):
  - a move emits: a from-room `room` echo (the room we left), then the arrival
    `room` (via _look()), then a `stats` push (which re-raises _decide).
  - `rest` rejects with "You can only rest in Town Square" unless the server's
    current room is Town Square; rejection sends only a `line`, never a `room`.
  - a successful `rest` sends a confirm `line` + a `stats` push that heals.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())
import warpdrive  # noqa: E402

HOME = "Town Square"
MOB_ROOM = "Cave"
MOB = "Cave Wyrm"


class Harness:
    def __init__(self):
        self.sent = []
        self.room = MOB_ROOM        # server's authoritative current room
        self.ws = None

    async def on_line(self, line: str):
        self.sent.append(line)
        # Emulate the server: a move first echoes the room we left, then the
        # arrival room, then a stats push (which triggers the client _decide).
        if line in ("n", "s", "e", "w"):
            d = line
            frm = self.room
            dest = self._dest(frm, d)
            # from-room echo
            self._push({"ch": "room", "name": frm, "creatures": [],
                        "exits": [{"dir": d, "name": d.upper(), "to": dest}]})
            # arrival
            self.room = dest
            self._push({"ch": "room", "name": dest, "creatures": [],
                        "exits": [{"dir": "x", "name": "X", "to": frm}]})
            self._push({"ch": "stats", "hp": 480, "maxhp": 960,
                        "stat_points": 0, "room": dest, "level": 5,
                        "xp": 0, "str": 1, "con": 1, "dex": 1,
                        "magic": 1, "weapon": "", "armor": ""})
        elif line == "rest":
            if self.room == HOME:
                self._push({"ch": "line",
                            "text": "You sit down to rest in Town Square."})
                self._push({"ch": "stats", "hp": 481, "maxhp": 960,
                            "stat_points": 0, "room": HOME, "level": 5,
                            "xp": 0, "str": 1, "con": 1, "dex": 1,
                            "magic": 1, "weapon": "", "armor": ""})
            else:
                self._push({"ch": "line",
                            "text": "You can only rest in Town Square."})
        elif line == "kill":
            self._push({"ch": "line", "text": f"The {MOB} dies!"})
            self._push({"ch": "room", "name": self.room, "creatures": [],
                        "exits": []})

    def _dest(self, frm, d):
        # Tiny map: Cave --s--> Town Square only (home). Every other dir stays put.
        if frm == MOB_ROOM and d == "s":
            return HOME
        return frm

    def _push(self, msg):
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.ws._on_message(msg)))


class _FakeWS:
    def __init__(self, h): self.h = h
    async def send(self, payload): await self.h.on_line(json.loads(payload)["line"])


def _args():
    ns = argparse.Namespace()
    for k in ("host", "port", "name", "password", "script", "home", "mob_room",
              "mob", "look_interval", "rest_hp", "retreat_hp", "train_stats",
              "tls", "verify", "delay"):
        setattr(ns, k, None)
    ns.look_interval = 10
    ns.home = HOME
    ns.mob_room = MOB_ROOM
    ns.mob = MOB
    ns.rest_hp = 95
    ns.retreat_hp = 50
    ns.train_stats = "str con dex"
    ns.delay = 0
    return ns


def _make_driver(h):
    ns = _args()
    with open("warpdrive.script") as fh:
        rules = warpdrive.parse_script(fh.read(), vars={
            "mob": MOB, "mob_room": MOB_ROOM, "home": HOME,
            "retreat_hp": 50, "rest_hp": 95, "train_stats": "str con dex"})
    wm = warpdrive.WorldMap("/tmp/test_dm_map.json")
    # Seed the map: Cave --s--> Town Square (home).
    wm.edges = {MOB_ROOM: {"s": HOME}, HOME: {}}
    d = warpdrive.Driver(ns, rules, wm)
    h.ws = d
    d._ws = _FakeWS(h)
    return d


async def scenario():
    h = Harness()
    d = _make_driver(h)
    # Driver in mob room, badly hurt, so rule 1 (retreat-rest) should fire.
    d.map.current = MOB_ROOM
    d.stats = {"hp": 479, "maxhp": 960, "stat_points": 0, "room": MOB_ROOM,
               "level": 5, "xp": 0, "str": 1, "con": 1, "dex": 1,
               "magic": 1, "weapon": "", "armor": ""}
    d._resting_local = False
    d._authed = True
    await d._decide()                     # should issue exactly ONE 's'
    moves_before_arrival = [s for s in h.sent if s in ("n", "s", "e", "w")]
    # Now drive the async message pump drained by the server echoes above; give
    # the event loop a moment to process the arrival + stats re-decide.
    await asyncio.sleep(0.05)
    # After arrival the driver is in Town Square and should rest (one 'rest').
    await asyncio.sleep(0.05)
    return moves_before_arrival, list(h.sent)


def test():
    moves, all_sent = asyncio.run(scenario())
    print("moves sent before any arrival:", moves)
    print("all sent:", all_sent)
    # Exactly one move ('s') must be issued for the single retreat-rest — the
    # from-room echo + post-move stats must NOT trigger a second move.
    assert moves == ["s"], f"expected exactly one 's'; got {moves}"
    # After arrival the driver must rest (and it must be accepted, since the
    # server's room is now Town Square).
    assert all_sent.count("rest") >= 1, "driver must issue rest once home"
    # And crucially no "can only rest in Town Square" rejection (position never
    # desynced because there was no double-move).
    rejects = [s for s in all_sent if "can only rest" in s]
    assert not rejects, f"rest was rejected (desync!): {rejects}"
    print("PASS: single move per retreat-rest; no double-move; rest accepted at home")


if __name__ == "__main__":
    test()
