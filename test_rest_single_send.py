#!/usr/bin/env python3
"""Regression test: `rest` must be sent EXACTLY ONCE when we believe we're home.

Previously the driver sent `rest` every decide() tick until the server's
confirmation line arrived (a round-trip later), because _resting_local was only
set on server confirm. Three `rest` sends showed in the log (each tagged
"waiting for server confirm") and during that window a stray `room` echo could
flip map.current to a neighbor and bounce us out -- the "heal stops on
occasion" transient. Now _resting_local is set optimistically on SEND, so the
resting-room guard shields us through the round-trip.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())
import warpdrive  # type: ignore


HOME = "Town Square"
MOB = "Cave Wyrm"
MOB_ROOM = "Cave"


class _FakeWS:
    def __init__(self):
        self.sent = []          # raw json strings the driver emitted

    async def send(self, text):
        self.sent.append(text)


def _args():
    class A:
        pass
    a = A()
    for k, v in dict(host="x", port=1, name="kawasu", password="p",
                     script="warpdrive.script", tls=False, verify=False,
                     delay=0.0, mob=MOB, mob_room=MOB_ROOM, home=HOME,
                     train_room=HOME, rest_hp=95, retreat_hp=50,
                     look_interval=0.5, hp_floor=0.1,
                     map_path="/tmp/map.test_rest_single_send.json",
                     connect=False, rules=None, world=None).items():
        setattr(a, k, v)
    return a


def _make_driver():
    ns = _args()
    with open("warpdrive.script") as fh:
        rules = warpdrive.parse_script(fh.read(), vars={
            "mob": MOB, "mob_room": MOB_ROOM, "home": HOME,
            "train_room": HOME, "rest_hp": "95", "retreat_hp": "50"})
    wm = warpdrive.WorldMap("/tmp/map.test_rest_single_send.json")
    wm.current = HOME
    wm.edges = {HOME: {"e": "East Field"}, "East Field": {"w": HOME}}
    d = warpdrive.Driver(ns, rules, wm)
    d._ws = _FakeWS()
    d._last_look = 0.0
    d._rest_rejected = False
    d.stats = {"hp": 470, "maxhp": 960, "stat_points": 0, "room": HOME,
               "level": 5, "xp": 0, "gold": 0}
    return d


async def scenario():
    d = _make_driver()
    # 1) First decide tick: we're home + hurt -> should send `rest` exactly once
    #    and optimistically set _resting_local before the server confirms.
    await d._decide()
    assert d._resting_local is True, "rest send must set _resting_local optimistically"
    # 2) A stray `room` echo from recent history (a neighbor) arrives during the
    #    pre-confirm round-trip window. The resting-room guard must ignore it so
    #    our position belief stays HOME (no bounce).
    await d._on_message({
        "ch": "room", "name": "East Field", "creatures": [],
        "exits": [{"dir": "w", "name": "West", "to": HOME}]})
    assert d.map.current == HOME, (
        f"stray room echo flipped position to {d.map.current!r}; "
        "resting-room guard failed to shield the round-trip")
    # 3) Server rest-confirm line + a stats hp-tick re-trigger decide().
    await d._on_message(
        {"ch": "line", "text": "You sit down to rest in Town Square."})
    d.stats = dict(d.stats, hp=560)
    await d._decide()
    # 4) Another decide tick; must NOT re-send `rest` (we're already resting).
    await d._decide()
    return [json.loads(s).get("line") for s in d._ws.sent]


def test():
    sent = asyncio.run(scenario())
    rests = [s for s in sent if s == "rest"]
    print("rest commands sent:", rests)
    assert rests == ["rest"], (
        f"expected exactly ONE `rest` send when home; got {rests}\n"
        "A stray room echo during the pre-confirm window must not re-trigger "
        "rest or bounce us out (the 'heal stops on occasion' transient).")
    print("PASS: rest sent exactly once; optimistic pending shields round-trip")


if __name__ == "__main__":
    test()
