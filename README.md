# keryx-stream

A standalone [Hermes](https://github.com/NousResearch/hermes-agent) plugin that
gives the **Keryx** Android client live token streaming and toolset control —
using only the public plugin surface, with **no patches to the hermes-agent
core tree**.

Keryx chats over Matrix (multi-device sync, history, push). Matrix delivers a
message per turn, not a token stream, so this plugin subscribes to the gateway's
generic streaming observer hooks and mirrors each turn's tokens to its own
Server-Sent-Events side-channel that the app renders live, then lets Matrix sync
the single final message.

## What it exposes

Its own small HTTP server (default `:8646`, bearer-authed), never touching the
gateway's api_server:

| route | purpose |
|-------|---------|
| `GET /keryx/stream?platform=<p>&chat_id=<id>` | transient SSE of one turn — `event: delta` / `segment` / `reasoning` / `stop` / `ping` |
| `GET /keryx/toolsets?platform=<p>` | toolset view for the platform the turn runs on |
| `PUT /keryx/toolsets/{name}` | enable/disable a toolset (`{"enabled": true}`) |
| `GET /keryx/health` | liveness |

## Requirements

A hermes-agent that provides the streaming observer hooks
(`on_stream_delta` / `on_stream_segment` / `on_stream_end`) —
[NousResearch/hermes-agent#65077](https://github.com/NousResearch/hermes-agent/pull/65077).
Without them the plugin loads and serves toolsets, but live streaming stays
inactive (it logs a one-line notice).

## Install

**Directory install** — symlink (or copy) the package into your plugins dir:

```bash
ln -s "$PWD/keryx_stream" ~/.hermes/plugins/keryx_stream
```

**pip install** — discovered via the `hermes_agent.plugins` entry point:

```bash
pip install keryx-stream          # or: pip install -e .
```

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
