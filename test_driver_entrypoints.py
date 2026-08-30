#!/usr/bin/env python3
"""Smoke test: the Driver class must expose its public coroutine entrypoints.

Guards against a regression where a patch clobbers a method header (e.g. the
rest-watchdog insertion in b873b2e dropped `async def run(self):`, turning the
entire connection loop into unreachable dead code after an infinite `while True`;
`py_compile` stayed green and the message-handler tests passed, but
`Driver(args, rules, wm).run()` raised AttributeError: 'Driver' object has no
attribute 'run' at startup). That break is only catchable by asserting the
entrypoints exist on the *class*, not by exercising handlers.
"""
import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import warpdrive


REQUIRED = ["__init__", "run", "_respawn_watch", "_rest_watch", "_on_message",
            "_decide", "send"]


# NOTE: we don't need a real driver; checking the class contract is enough and
# avoids constructing the websocket machinery. If that ever changes, build one
# with the same fixture the other tests use.
def main():
    missing = [m for m in REQUIRED if not hasattr(warpdrive.Driver, m)]
    assert not missing, f"Driver missing entrypoints: {missing}"

    # The connection loop must be an async method named exactly `run`.
    assert inspect.iscoroutinefunction(warpdrive.Driver.run), \
        "Driver.run must be an async coroutine (a clobbered header broke startup)"
    assert inspect.iscoroutinefunction(warpdrive.Driver._respawn_watch)
    assert inspect.iscoroutinefunction(warpdrive.Driver._rest_watch)
    assert inspect.iscoroutinefunction(warpdrive.Driver._on_message)
    assert inspect.iscoroutinefunction(warpdrive.Driver._decide)
    print("PASS: Driver exposes all required entrypoints; run() is an async "
          "coroutine (no clobbered method header).")


if __name__ == "__main__":
    main()
