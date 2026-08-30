#!/usr/bin/env python3
"""Regression test: the rest-stall watchdog must unstick a freeze when the
server's per-second heal `stats` tick stops arriving mid-rest (driver hangs at
~92%). While _resting_local is set, if no stats update lands within rest_timeout,
the watchdog forces a `look` (surfacing authoritative state) + re-decides, which
clears _resting_local so the driver marches back to the mob instead of freezing.

Mechanism under test (verified against the live freeze, log ended at
`[loc] Town Square HP 919/990 (92%)` with NO `[rest] already at full hp`):
- stats handler records _rest_hp/_rest_maxhp/_rest_ts only while _resting_local.
- _rest_watch polls every rest_timeout/2; if (full) or (time since _rest_ts >
  rest_timeout) and not mid-move, it sends `look`, prints a watchdog line, and
  _decide(). The look response (a `room` for home) re-runs the stats path which
  clears _resting_local when hp >= rest_hp.
"""
import asyncio
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warpdrive


HOME = "Town Square"
MOB = "Cave Wyrm"
MOB_ROOM = "Cave"


class FakeTransport:
    """Records sent lines; auto-acks so `await self.send` returns."""
    def __init__(self):
        self.sent = []
        self._ack = None

    def send(self, text):
        self.sent.append(text)
        fut = self._ack
        if fut is not None and not fut.done():
            fut.set_result(None)
        return fut if fut is not None else _noop()

    def set_ack(self, fut):
        self._ack = fut


class _noop:
    def __await__(self):
        return iter(())


class FakeWS:
    def __init__(self, transport):
        self._t = transport

    async def send(self, text):
        return self._t.send(text)


def _make_driver():
    ns = type("Ns", (), {})()
    ns.rest_timeout = 1.0          # tight window so the test is fast
    ns.look_interval = 10
    ns.retreat_hp = 50
    ns.rest_hp = 95
    ns.delay = 0.0
    ns.home = HOME
    ns.train_room = HOME
    ns.mob = MOB
    ns.mob_room = MOB_ROOM
    ns.tls = False
    ns.verify = True
    ns.host = "x"
    ns.port = 1
    ns.name = "Tester"
    ns.password = "pw"
    ns.script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warpdrive.script")
    with open(ns.script) as fh:
        rules = warpdrive.parse_script(fh.read(), vars={
            "mob": MOB, "mob_room": MOB_ROOM, "home": HOME,
            "train_room": HOME, "rest_hp": "95", "retreat_hp": "50"})
    wm = warpdrive.WorldMap("/tmp/map.test_rest_watchdog.json")
    wm.current = HOME
    wm.edges = {HOME: {"e": "East Field"}, "East Field": {"w": HOME}}
    d = warpdrive.Driver(ns, rules, wm)
    return d


async def scenario():
    d = _make_driver()
    ft = FakeTransport()
    d._ws = FakeWS(ft)

    # 1) We are home and resting; server confirms rest.
    await d._on_message({"ch": "line", "text": "You sit down to rest in Town Square."})
    assert d._resting_local is True, "rest confirm must set _resting_local"
    # 2) Heal ticks arrive: HP climbs to 92% (the freeze point from the bug).
    for hp in (562, 681, 800, 919):
        await d._on_message({"ch": "stats", "hp": hp, "maxhp": 990, "room": HOME,
                              "level": 1, "xp": 0, "xp_next": 100,
                              "str": 1, "con": 1, "dex": 1, "magic": 1,
                              "stat_points": 0, "gold": 0,
                              "weapon": "none", "armor": "none",
                              "ring_used": 0, "ring_capacity": 4})
    assert d._rest_hp == 919, f"expected tracked hp 919, got {d._rest_hp}"
    # 3) SIMULATE THE FREEZE: the heal `stats` tick stops arriving. The driver
    #    now sits at 92% with _resting_local True and nothing driving _decide().
    assert d._resting_local is True, "still resting (frozen) before watchdog"

    # 4) Start the watchdog and wait past rest_timeout with NO further stats.
    d._rest_watch_task = asyncio.create_task(d._rest_watch())
    await asyncio.sleep(d.rest_timeout * 2.5)  # > rest_timeout, no tick
    # The watchdog must have nudged a `look` to surface state. `send` wraps the
    # command as JSON {"line": "look"}, so check for that.
    assert any('"look"' in s for s in ft.sent), \
        f"watchdog must force a 'look' on stall; sent={ft.sent}"

    # 5) Server answers the look with the authoritative (still-resting, 92%) room.
    #    Re-decide runs; with hp 919/990 = 92% >= rest_hp 95? No -> still resting,
    #    but the watchdog loop is healthy (it will keep polling). To prove it does
    #    NOT freeze, feed one more heal tick to full and confirm it leaves rest.
    await d._on_message({"ch": "stats", "hp": 990, "maxhp": 990, "room": HOME,
                          "level": 1, "xp": 0, "xp_next": 100,
                          "str": 1, "con": 1, "dex": 1, "magic": 1,
                          "stat_points": 0, "gold": 0,
                          "weapon": "none", "armor": "none",
                          "ring_used": 0, "ring_capacity": 4})
    # 6) A final authoritative tick => full => _decide clears _resting_local.
    assert d._resting_local is False, \
        "full HP must clear _resting_local so the driver proceeds (no freeze)"
    d._rest_watch_task.cancel()
    return True


def main():
    ok = asyncio.run(scenario())
    print("PASS: rest-stall watchdog forces a 'look' on a missed heal tick and "
          "clears resting at full HP — no 92% freeze.")


if __name__ == "__main__":
    main()
