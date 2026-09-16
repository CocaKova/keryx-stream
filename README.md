# keryx-stream

A standalone [Hermes](https://github.com/NousResearch/hermes-agent) plugin that
gives the **Keryx** Android client live token streaming and toolset control —
using only the public plugin surface, with **no patches to the hermes-agent
core tree**.

Keryx chats over Matrix (multi-device sync, history, push). Matrix delivers a
message per turn, not a token stream, so this plugin subscribes to Hermes'
shipped streaming and tool observer hooks (`on_stream_start` /
`on_stream_delta` / `on_stream_end` / `on_interim_message` plus
`pre_tool_call` / `post_tool_call` — every one carries the `session_id`) and
mirrors each turn's tokens and tool activity to its own Server-Sent-Events
side-channel that the app renders live, then lets Matrix sync the single final
message.

## What it exposes

Its own small HTTP server (default `:8646`, bearer-authed), never touching the
gateway's api_server:

| route | purpose |
|-------|---------|
| `GET /keryx/stream?platform=<p>&chat_id=<id>` | transient SSE of one turn — `event: start` / `delta` / `reasoning` / `interim` / `tool` / `stop` / `ping` |
| `POST /keryx/publish` | ingest for non-hub processes (see "Foreign sessions: forward mode") |
| `GET /keryx/toolsets?platform=<p>` | toolset view for the platform the turn runs on |
| `PUT /keryx/toolsets/{name}` | enable/disable a toolset (`{"enabled": true}`) |
| `GET /keryx/health` | liveness |

## Requirements

A hermes-agent with the shipped stream observer hooks
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
```

<details>
<summary>Manual alternatives</summary>

**Directory install** — symlink (or copy) the package into your plugins dir:

```bash
ln -s "$PWD/keryx_stream" ~/.hermes/plugins/keryx_stream
```

**pip install** — discovered via the `hermes_agent.plugins` entry point:

```bash
pip install keryx-stream          # or: pip install -e .
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
  toolsets:
    locked: []                 # toolsets the app may not disable
    forbidden: []              # toolsets the app may not enable
```

The bearer token is a **secret**, so it comes from the environment — set
`KERYX_STREAM_TOKEN`, or reuse your existing `API_SERVER_KEY`. Every route
except `/keryx/health` requires `Authorization: Bearer <token>`.

## How it works

- `register(ctx)` registers the three observer hooks and starts the SSE server
  on its own daemon thread (its own event loop) — so it adds no core route.
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

## Promotion

Published as a standalone plugin per Hermes' third-party-integration policy;
announced in the Nous Research Discord `#plugins-skills-and-skins`.
