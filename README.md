# keryx-stream

A standalone [Hermes](https://github.com/NousResearch/hermes-agent) plugin that
gives the **Keryx** Android client live token streaming and everything behind
its hub panels — using only the public plugin surface, with **no patches to
the hermes-agent core tree**.

**Point the app at this plugin's port and you are done**: it serves the
`/keryx/*` routes itself and relays everything else to Hermes' native API
server, so the app's single gateway URL covers both.

Keryx chats over Matrix (multi-device sync, history, push). Matrix delivers a
message per turn, not a token stream, so this plugin subscribes to Hermes'
shipped streaming and tool observer hooks (`on_stream_start` /
`on_stream_delta` / `on_stream_end` / `on_interim_message` plus
`pre_tool_call` / `post_tool_call` — every one carries the `session_id`) and
mirrors each turn's tokens and tool activity to its own Server-Sent-Events
side-channel that the app renders live, then lets Matrix sync the single final
message.

## What it exposes

Its own small HTTP server (default `:8646`, bearer-authed). It adds no route
to the gateway's api_server.

| route | purpose |
|-------|---------|
| `GET /keryx/stream?platform=<p>&chat_id=<id>` | transient SSE of one turn — `event: start` / `delta` / `reasoning` / `interim` / `segment` / `tool` / `stop` / `ping` |
| `POST /keryx/publish` | ingest for non-hub processes (see "Foreign sessions: forward mode") |
| `GET /keryx/health` | liveness, plugin `version`, and the `features` list (open, no token) |
| `GET`/`PUT /keryx/toolsets[/{name}]` | toolset view + toggle for a platform |
| `GET /keryx/capabilities`, `PUT /keryx/reasoning` | what the active brain accepts on the reasoning dial, and setting it |
| `GET /keryx/commands` | the gateway's slash-command catalog |
| `GET`/`PUT /keryx/config`, `/keryx/config/raw` | whitelisted config knobs; the raw file with hash-guarded, backed-up writes |
| `GET /keryx/brains`, `GET /keryx/model/options`, `POST /keryx/brain` | model picker; operator-defined brain swaps |
| `GET /keryx/logs?lines=` | redacted tail of the gateway log |
| `/keryx/kanban/*` | Missions: board, task detail, create, comment, settings, events, notify subs |
| `/keryx/skills/*`, `/keryx/skill-trash/*` | read/write/create skills; delete goes to a restorable trash |
| `POST /keryx/sessions/prune` | preview or prune old sessions |
| `/keryx/pet*` | companion pet: active, gallery, select, thumbnail |
| `GET`/`POST /keryx/update*` | commits-behind count; run the update command |
| `/keryx/git/*` | Shipyard: repos, status, diff review, stage, commit, push |
| anything else | relayed to the native API server (`/health`, `/v1/*`, `/api/*` …) |

### One URL

The app has one gateway URL. Set it to this plugin — `http://<host>:8646` —
with the same key you would give the API server. `/keryx/*` is answered here;
every other path is relayed to `keryx_stream.upstream_url` (default
`http://127.0.0.1:$API_SERVER_PORT`, i.e. `:8642`). The relay never widens
access: a request must pass the plugin's bearer check before it is forwarded
(only the bare `/health` probe is open), and it is then presented upstream with
`API_SERVER_KEY`. Set `upstream_url: ""` to turn the relay off.

### What can change the host, and how it is gated

Every route needs the bearer token. The routes that can run commands or write
outside Hermes' own state are additionally **off until you configure them** in
`config.yaml`, and the app only ever sees names and labels, never commands:

```yaml
keryx:
  git:
    enabled: true            # Shipyard. Off by default; repos are confined to $HOME
  brains:                    # brain picker swaps. No entries = read-only picker
    - name: my-brain
      command: "/path/to/swap-script"
  update:
    enabled: true            # default: Hermes' own `hermes update`
    command: "my-update-wrapper"   # set this if you carry local patches
```

## Requirements

Python 3.10+ and `aiohttp` (already in Hermes' environment). A hermes-agent with the shipped stream observer hooks
(`on_stream_start` / `on_stream_delta` / `on_stream_end` /
`on_interim_message`; landed upstream in #84924). Without them the plugin
loads and serves toolsets, but live streaming stays inactive (it logs a
one-line notice). Tool mirroring additionally uses the shipped
`pre_tool_call` / `post_tool_call` hooks.

## Foreign sessions: forward mode

Turns driven from OUTSIDE the gateway process — a `hermes chat` one-shot, a
cron run, any CLI process — fire the same hooks in their own process, where no
subscriber is attached. The plugin handles this with two run modes, picked
automatically at startup:

- **Hub mode** — this process binds the SSE port (the gateway, normally) and
  serves subscribers.
- **Forward mode** — the port is already bound by another keryx-stream
  instance, so hook events are POSTed to that instance's `/keryx/publish`
  route. A CLI session's deltas and tool events then appear on the hub
  owner's side-channel exactly like a gateway turn's, keyed by session id:
  subscribe with `GET /keryx/stream?platform=cli&chat_id=<session_id>`.

Forward mode is automatic — no flag. To point a forwarder at a specific hub
owner (e.g. a remote gateway), set `forward_url` in the plugin's config.yaml
block:

```yaml
keryx_stream:
  forward_url: "http://gateway-host:8646/keryx/publish"
```

The bearer token is shared between modes: `KERYX_STREAM_TOKEN` (or
`API_SERVER_KEY`) authenticates both the subscriber's `GET /keryx/stream` and
the forwarder's `POST /keryx/publish`.

## Install

**One command** — clone and run the installer (symlinks the plugin into
`~/.hermes/plugins/` and prints the config + token you still need):

```bash
git clone https://github.com/CocaKova/keryx-stream.git
cd keryx-stream
./install.sh            # or ./install.sh --copy to copy instead of symlink
hermes plugins enable keryx-stream
hermes gateway restart
curl -s localhost:8646/keryx/health    # → {"ok": true, "version": "…", "features": […]}
```

Hermes loads user plugins only once they are **enabled** — without the
`enable` step the plugin is discovered and then ignored. `hermes plugins list`
shows its status; `hermes plugins compat` tells you if a Hermes update retired
an import this plugin uses (it is tested nightly against hermes-agent `main`).

Then in Keryx → Settings → Gateways → **Hermes Link**: Gateway URL
`http://<host>:8646`, your key, **Test link**.

<details>
<summary>Manual alternatives</summary>

**Directory install** — symlink (or copy) the package into your plugins dir:

```bash
ln -s "$PWD/keryx_stream" ~/.hermes/plugins/keryx_stream
```

**pip install from a clone** — discovered via the `hermes_agent.plugins` entry point (the package is not on PyPI):

```bash
pip install -e .
```

</details>

## Configure

Non-secret settings go in `~/.hermes/config.yaml` (the plugin follows Hermes'
"secrets in env, everything else in config.yaml" rule):

```yaml
keryx_stream:
  enabled: true
  host: "0.0.0.0"
  port: 8646
  default_platform: matrix     # platform key used when a turn's metadata omits one
  panels: true                 # serve the app's /keryx/* panel routes
  upstream_url: "http://127.0.0.1:8642"   # native API server to relay to; "" = off
  toolsets:
    locked: []                 # toolsets the app may not disable
    forbidden: []              # toolsets the app may not enable
  config_locked: []            # config knob keys the app may not change
```

`host: "0.0.0.0"` lets a phone on your LAN or tailnet reach the port. Every
route is bearer-authed and the plugin refuses all requests when no token is
set, but if the machine is reachable from networks you don't trust, bind
`127.0.0.1` and publish the port through your VPN (e.g. `tailscale serve`).

The bearer token is a **secret**, so it comes from the environment — set
`KERYX_STREAM_TOKEN`, or reuse your existing `API_SERVER_KEY`. Every route
except `/keryx/health` requires `Authorization: Bearer <token>`.

## How it works

- `register(ctx)` registers the observer hooks and starts the server on its
  own daemon thread (its own event loop) — so it adds no core route.
- A turn is published under two keys: `(surface, session_id)`, which is what
  the hooks carry, and the chat it belongs to — `(platform, chat_id)`, e.g.
  `matrix` + room id — resolved from the gateway's session store (borrowed via
  the `pre_gateway_dispatch` hook, observer only). The app on a chat transport
  only knows the room, so that is the key it subscribes with.
- On a multiplexed (multi-profile) gateway each request enters the process
  profile's secret scope, as the native API server does.
- Hook callbacks run on the gateway's stream worker thread and hand tokens to an
  in-process hub via `call_soon_threadsafe`; the SSE handler drains and
  **coalesces** bursts so a fast model can't overflow a subscriber's bounded
  queue and drop a token (which would break the client's stream/commit match).
- Toolsets are read/written through the same `hermes_cli` helpers the desktop
  picker uses, so the app and desktop agree on state.

## Development

```bash
pip install -e '.[dev]'   # or just: pip install aiohttp pytest pytest-asyncio
pytest -q
```

The panel tests import Hermes itself and skip without it. Point them at a
checkout with `HERMES_AGENT_ROOT=/path/to/hermes-agent` (default
`~/.hermes/hermes-agent`) and run pytest with that tree's Python.

## Promotion

Published as a standalone plugin per Hermes' third-party-integration policy;
announced in the Nous Research Discord `#plugins-skills-and-skins`.
