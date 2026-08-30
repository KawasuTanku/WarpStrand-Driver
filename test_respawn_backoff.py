#!/usr/bin/env python3
"""Regression test: respawn watcher must not redundantly probe right after the
death handler's immediate `look`, but MUST still probe after the backoff window
if the mob has not yet respawned.

Reproduces the post-death probe cluster from ~/output.log: the death handler
already sends an immediate `look`; before the fix the watcher sent another `look`
~10s later (and again) in the death->respawn gap, stacking redundant probes.

Server model: mob is lazy-respawned only when a `look` is issued. Death clears
the mob from the room's creature list until a `look` re-adds it.
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
    """Minimal faithful server: room = mob room, mob lazy-respawns on `look`."""
    def __init__(self):
        self.sent = []          # lines the driver sent
        self.room = MOB_ROOM     # driver's authoritative room
        self.mob_alive = True    # mob present in room?
        self.driver = None

    async def on_line(self, line: str):
        self.sent.append(line)
        if line == "look" and not self.mob_alive:
            self.mob_alive = True   # lazy respawn on look
        elif line == "kill":
            self.mob_alive = False
            self._push({"ch": "line", "text": f"The {MOB} dies! You gain experience."})
        creatures = [MOB] if self.mob_alive else []
        self._push({"ch": "room", "name": self.room,
                    "creatures": creatures, "exits": [{"dir": "w", "name": "West", "to": HOME}]})

    def _push(self, msg):
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: asyncio.ensure_future(self.driver._on_message(msg)))


class _FakeWS:
    def __init__(self, h): self.h = h
    async def send(self, payload): await self.h.on_line(json.loads(payload)["line"])


def _args(interval):
    ns = argparse.Namespace()
    for k in ("host", "port", "name", "password", "script", "home", "mob_room",
              "mob", "look_interval", "rest_hp", "retreat_hp", "train_stats",
              "tls", "verify", "delay"):
        setattr(ns, k, None)
    ns.look_interval = interval
    ns.home = HOME
    ns.mob_room = MOB_ROOM
    ns.mob = MOB
    ns.rest_hp = 95
    ns.retreat_hp = 50
    ns.train_stats = "str con dex"
    ns.delay = 0
    return ns


def _make_driver(interval, h):
    ns = _args(interval)
    with open("warpdrive.script") as fh:
        rules = warpdrive.parse_script(fh.read(), vars={
            "mob": MOB, "mob_room": MOB_ROOM, "home": HOME,
            "retreat_hp": 50, "rest_hp": 95, "train_stats": "str con dex"})
    wm = warpdrive.WorldMap("/tmp/test_map_kawasu.json")
    d = warpdrive.Driver(ns, rules, wm)
    h.driver = d
    d._ws = _FakeWS(h)
    d.look_interval = interval
    return d


async def scenario(interval):
    h = Harness()
    d = _make_driver(interval, h)
    # Place driver in mob room with mob present.
    d.map.current = MOB_ROOM
    d.creatures = [MOB]
    d._respawn_watch_task = asyncio.ensure_future(d._respawn_watch())
    await asyncio.sleep(0.05)
    # Kill the mob -> death handler sends immediate look.
    d._last_kill_ts = 0.0
    await d._on_message({"ch": "line", "text": f"The {MOB} dies! ..."})
    looks_before = [s for s in h.sent if s == "look"]
    # Watcher fires at +interval. The death-handler look just happened (t~0),
    # so the backoff must suppress the watcher's probe.
    await asyncio.sleep(interval * 1.2)
    looks_mid = [s for s in h.sent if s == "look"]
    d._respawn_watch_task.cancel()
    return looks_before, looks_mid


def test():
    interval = 0.5
    looks_before, looks_mid = asyncio.run(scenario(interval))
    print("looks after death-handler:", looks_before)
    print("looks during backoff window (watcher):", looks_mid)
    assert looks_before.count("look") == 1, f"expected 1 death-handler look, got {looks_before.count('look')}"
    assert looks_mid.count("look") == 1, f"watcher must NOT probe during backoff; got {looks_mid.count('look')}"
    print("PASS: respawn watcher does not redundantly probe during backoff; "
          "death-handler probe preserved")


if __name__ == "__main__":
    test()
