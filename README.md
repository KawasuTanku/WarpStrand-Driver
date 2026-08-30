# WarpStrand-Driver

Standalone, **rules-driven auto-player** for the WarpStrand MUD server. It connects
to a game server over WebSocket and drives a character according to a plain-text
rules script you control — no TUI pane-scraping, just the server protocol.

## Features

- **Self-contained** — only needs `websockets` + `PyYAML`. No dependency on the
  WarpStrand-Client (TUI) package.
- **Rules engine** — a firewall-style script (`warpdrive.script`) decides every
  action. Conditions: `in`, `has-stat-points`, `mob`, `hp-below`, `hp-above`,
  `always`. Actions: `train`, `goto`, `kill`, `retreat`, `rest`, `retreat-rest`,
  `say`, `look`. First matching rule wins.
- **Self-discovering map** — learns room exits from each `room` message, persists
  them to `map.<name>.json`, and routes with BFS. Later runs start with full
  knowledge of the world.
- **Reads your existing client config** — `~/.config/warpstrand/client.yaml`
  (host/port/name/password), the same file the TUI client uses. CLI flags and
  env vars override.

## Install

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Configure

Copy the example and fill in your credentials:

```bash
cp client.yaml.example ~/.config/warpstrand/client.yaml
$EDITOR ~/.config/warpstrand/client.yaml
```

(Or pass `--name` / `--password` on the command line, or use `WARP_NAME` /
`WARP_PASS` env vars.)

## Run

```bash
.venv/bin/python warpdrive.py --name YOURNAME --password YOURPASS
```

Optional flags: `--host`, `--port`, `--mob`, `--mob-room`, `--home`, `--train-room`,
`--rest-hp`, `--hp-floor`, `--script PATH`, `--config PATH`, `--delay N`, `--tls`, `--no-verify`.

All of these (plus `tls`/`verify`) can also be set in `~/.config/warpstrand/client.yaml`
under the same keys (e.g. `mob:`, `mob_room:`, `home:`, `rest_hp:`). CLI > yaml > env > default.

### TLS / wss:// servers

If your server speaks TLS, add it to `client.yaml`:

```yaml
host: your.server.example
port: 443
tls: true
# verify: false   # uncomment for a self-signed / internal-CA certificate
```

Or pass `--tls` (and `--no-verify` to skip certificate checks):

```bash
.venv/bin/python warpdrive.py --tls --no-verify --name YOURNAME --password YOURPASS
```

## The rules script

Edit `warpdrive.script` (or pass `--script`). Example:

```
# Survival first
when hp-below 15 do retreat-rest "Town Square"
when in "Town Square" and hp-below 80 do rest

# Train when banked stat points exist
when in "Town Square" and has-stat-points do train str con dex

# Fight the target mob when healthy
when mob "Cave Wyrm" and hp-above 25 do kill "Cave Wyrm"

# Otherwise head to the mob's room (BFS over the discovered map)
when always do goto "Cave Mouth"
```

Lines starting with `#` are comments. Rules are evaluated top-to-bottom each
tick; the first whose conditions all hold fires (and only that one). The
`rest` / `retreat-rest` actions require a corresponding `rest` command on the
server (a planned server feature); the client sends them correctly and the
server will honor them once implemented.

## Protocol

```
client -> server:  {"line": "<command or login answer>"}
server -> client:  {"ch": "hello"|"prompt"|"room"|"stats"|"line"|"error"|...}
```

The driver sends the username as the first line, answers the `secret` prompt,
then acts on `room` (exits + creatures) and `stats` (hp / stat_points) messages.
