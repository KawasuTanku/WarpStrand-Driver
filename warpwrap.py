#!/usr/bin/env python3
"""warpwrap.py — Textual split-screen front-end for warpdrive.py.

    Left pane : live player panel (level, gold, xp, str/con/dex/magic,
                stat points, hp, room).
    Right pane: the driver's normal [act]/[map]/[rest]… log output,
                which is captured by redirecting builtins.print into a
                RichLog so nothing is lost.

The driver itself is unchanged — WarpWrap just instantiates
warpdrive.Driver and runs its async `run()` as a Textual worker on the
app's event loop, then steers its print output and stats into widgets.

Usage is identical to warpdrive.py:

    python warpwrap.py --host ... --port ... --name ... --password ...

Quit with 'q' or Ctrl-C.
"""

import asyncio
import builtins
import os

import textual
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.theme import Theme
from textual.widgets import Footer, Header, RichLog, Static
from textual.worker import Worker, WorkerState

import warpdrive


def _parse_rgb(val: str) -> tuple[int, int, int] | None:
    """Parse 'R,G,B' into a (r, g, b) tuple, or None on malformed input."""
    parts = val.split(",")
    if len(parts) != 3:
        return None
    try:
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        if 0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255:
            return r, g, b
    except ValueError:
        pass
    return None


def _read_tankuos_palette() -> dict[str, tuple[int, int, int]] | None:
    """Read TankuOS palette from env vars. Returns None if any are missing."""
    names = ("BG", "PANEL", "FG", "ACCENT", "SECONDARY", "ERROR")
    result = {}
    for n in names:
        val = _parse_rgb(os.environ.get(f"TANKUOS_THEME_{n}", ""))
        if val is None:
            return None
        result[n.lower()] = val
    return result


def _theme_from_env() -> Theme | None:
    """Build a Theme from TankuOS palette env vars.

    Returns None when not running under TankuOS (env vars unset/malformed),
    falling back to the caller's default theme selection.
    """
    p = _read_tankuos_palette()
    if p is None:
        return None
    bg, panel, fg = p["bg"], p["panel"], p["fg"]
    accent, secondary, error = p["accent"], p["secondary"], p["error"]

    def hx(t: tuple[int, int, int]) -> str:
        return f"#{t[0]:02x}{t[1]:02x}{t[2]:02x}"

    return Theme(
        name="tankuos-runtime",
        primary=hx(accent),
        secondary=hx(secondary),
        surface=hx(bg),
        panel=hx(panel),
        background=hx(bg),
        foreground=hx(fg),
        accent=hx(accent),
        error=hx(error),
        success="#6ec86e",
        warning="#e5c07b",
        dark=True,
    )

# Left-panel width as a fraction of the terminal. Tune to taste.
LEFT_FRACTION = "34%"


class LogMessage(Message):
    """Posted for every line the driver prints (via the print redirect)."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class StatsPanel(Static):
    """Left pane. Renders the driver's latest stats snapshot on refresh."""

    DEFAULT_CSS = """
    StatsPanel {
        width: 100%;
        height: 100%;
        padding: 1 2;
        border: round $secondary;
        content-align: left top;
    }
    """

    def __init__(self, get_driver, **kwargs) -> None:
        super().__init__(**kwargs)
        self._get_driver = get_driver

    def render(self) -> str:
        d = self._get_driver()
        if d is None:
            return "[b]connecting…[/b]"
        s = d.stats
        if not s:
            return f"[b]{d.name}[/b]\n\nwaiting for stats…"
        name = s.get("name") or d.name
        room = d.map.current or "?"
        return (
            f"[b]{name}[/b]\n"
            f"Room : {room}\n"
            f"Level: {s.get('level', '?')}\n"
            f"XP   : {s.get('xp', '?')}\n"
            f"Gold : {s.get('gold', '?')}\n"
            f"\n"
            f"STR {s.get('str', '?'):>4}   CON {s.get('con', '?'):>4}\n"
            f"DEX {s.get('dex', '?'):>4}   MAG {s.get('magic', '?'):>4}\n"
            f"\n"
            f"Stat pts : {s.get('stat_points', '?')}\n"
            f"HP  : {s.get('hp', '?')}/{s.get('maxhp', '?')}\n"
        )


class WarpWrap(App):
    TITLE = "WarpStrand Driver"
    BINDINGS = [("q", "quit", "Quit")]

    CSS = f"""
    #body {{ height: 1fr; }}
    #left {{ width: {LEFT_FRACTION}; }}
    #log {{
        width: 1fr;
        height: 100%;
        border: round $primary;
    }}
    RichLog#log {{ padding: 0 1; }}
    """

    def _apply_theme(self) -> None:
        """Apply TankuOS palette at runtime if available, else fall back.

        1. Try TANKUOS_THEME_* env vars from ptyhost → register and use custom
           theme that matches the active TankuOS palette exactly.
        2. Fall back to name→builtin mapping when running standalone (no
           TANKUOS_THEME_* env vars present).
        """
        env_theme = _theme_from_env()
        if env_theme is not None:
            self.register_theme(env_theme)
            self.theme = env_theme.name
            return
        # Standalone fallback: map name to a built-in Textual theme.
        theme = os.environ.get("TANKUOS_THEME", "").lower().strip()
        mapping = {
            "midnight": "textual-dark",
            "nord": "nord",
            "gruvbox": "gruvbox",
            "dracula": "dracula",
            "osaka-jade": "textual-dark",
        }
        self.theme = mapping.get(theme, "textual-dark")

    def __init__(self) -> None:
        super().__init__()
        self._game_driver: "warpdrive.Driver | None" = None
        self._orig_print = builtins.print
        self._running = True

    # ---- stats snapshot accessor (passed into StatsPanel) ----
    def _get_driver(self) -> "warpdrive.Driver | None":
        return self._game_driver

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            yield StatsPanel(self._get_driver, id="left")
            yield RichLog(id="log", wrap=True, markup=False)
        yield Footer()

    def on_mount(self) -> None:
        # Apply theme based on TANKuOS env var
        self._apply_theme()
        # Periodically repaint the left panel from the driver's stats.
        self.set_interval(0.25, self._refresh_stats)
        # Redirect the driver's prints into the log pane.
        builtins.print = self._captured_print
        # Launch the driver as an on-loop worker (it uses asyncio +
        # websockets, so it must share the app's event loop, not a thread).
        self.run_worker(self._drive(), thread=False, name="warpdrive")

    def _refresh_stats(self) -> None:
        panel = self.query_one("#left", StatsPanel)
        panel.refresh()

    def _captured_print(self, *args, **kwargs) -> None:
        if not self._running:
            self._orig_print(*args, **kwargs)
            return
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args)
        if end and not text.endswith(end):
            text += end
        # post_message is safe from the app's own event loop (the worker
        # runs on that same loop), so no cross-thread dance is needed.
        self.post_message(LogMessage(text))

    def on_log_message(self, message: LogMessage) -> None:
        self.query_one("#log", RichLog).write(message.text.rstrip("\n"))

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        # Surface worker death (e.g. connection refused) in the log instead
        # of silently vanishing.
        if event.worker.name == "warpdrive" and event.state == WorkerState.ERROR:
            err = event.worker.error or "unknown error"
            self.query_one("#log", RichLog).write(
                f"[driver worker exited: {err}]"
            )

    async def _drive(self) -> None:
        args = warpdrive.parse_args()
        log = self.query_one("#log", RichLog)
        if not args.name or not args.password:
            log.write("Need --name and --password (or WARP_NAME / WARP_PASS).")
            return
        if not os.path.exists(args.script):
            log.write(f"No script at {args.script}")
            return
        with open(args.script) as fh:
            rules = warpdrive.parse_script(fh.read(), vars={
                "mob": args.mob,
                "mob_room": args.mob_room,
                "home": args.home,
                "train_room": args.train_room,
                "rest_hp": args.rest_hp,
                "retreat_hp": args.retreat_hp,
            })
        if not rules:
            log.write("Script parsed to zero rules.")
            return
        map_path = os.path.join(
            os.path.dirname(os.path.abspath(args.script)),
            f"map.{args.name}.json",
        )
        wm = warpdrive.WorldMap(map_path)
        driver = warpdrive.Driver(args, rules, wm)
        self._game_driver = driver
        try:
            await driver.run()
        except asyncio.CancelledError:
            pass  # app is shutting down

    def on_unmount(self) -> None:
        self._running = False
        builtins.print = self._orig_print


if __name__ == "__main__":
    WarpWrap().run()
