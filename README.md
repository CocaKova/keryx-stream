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
shipped observer hooks and mirrors each turn to its own Server-Sent-Events
side-channel that the app renders live, then lets Matrix sync the single final
message:

| hook | what it feeds |
|------|---------------|
| `on_stream_start` / `on_stream_delta` / `on_stream_end` / `on_interim_message` | text and reasoning tokens, segment boundaries (from the per-call `iteration`) |
| `pre_tool_call` / `post_tool_call` | tool rows, and inline edit diffs matched by `tool_call_id` |
| `post_api_request` | the last call's prompt size, for the `usage` frame |
| `post_llm_call` | the end of the turn: exactly one `stop`, held until the last delta is out |
| `subagent_start` / `subagent_stop` | the delegation wing |
| `pre_auxiliary_call` / `post_auxiliary_call` | compaction `status` rows |
| `pre_gateway_dispatch` | borrows the session store so a turn also lands on its chat key (observer only) |
| `llm_request` middleware | the reasoning dial for local brains (vLLM / SGLang / llama.cpp) |

What the hooks cannot feed, and the hook Hermes would need for each, is in
[GAPS.md](GAPS.md).

## What it exposes

Its own small HTTP server (default `:8646`, bearer-authed). It adds no route
to the gateway's api_server.

| route | purpose |
|-------|---------|
| `GET /keryx/stream?platform=<p>&chat_id=<id>` | transient SSE of one turn — `event: start` / `delta` / `reasoning` / `interim` / `segment` / `tool` / `status` / `usage` / `stop` / `ping` |
| `POST /keryx/publish` | ingest for non-hub processes (see "Foreign sessions: forward mode") |
| `GET /keryx/health` | liveness, `version`, `requires_hermes`, the derived `features` list and config `hints` (open, no token) |
| `GET`/`PUT /keryx/toolsets[/{name}]` | toolset view + toggle for a platform |
| `GET /keryx/capabilities`, `PUT /keryx/reasoning` | what the active brain accepts on the reasoning dial, and setting it |
| `GET /keryx/commands` | the gateway's slash-command catalog |
| `GET`/`PUT /keryx/config`, `/keryx/config/raw` | whitelisted config knobs; the raw file with hash-guarded, backed-up writes |
| `GET /keryx/brains`, `GET /keryx/model/options`, `POST /keryx/brain` | model picker; operator-defined brain swaps |
| `GET /keryx/logs?lines=` | redacted tail of the gateway log |
| `/keryx/kanban/*` | Missions: board, task detail, create, comment, reply / approve / request-changes, settings, events, notify subs |
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

### `/keryx/health`: features and hints

```json
{"ok": true, "plugin": "keryx-stream", "version": "0.4.0", "requires_hermes": ">=0.21.3",
 "features": ["stream", "stream.chat_key", "publish", "toolsets", "reasoning", "interim",
              "tools", "tools.diff", "segment", "stop.turn", "usage", "thinking", "..."],
 "hints": [{"key": "compression.progress_notices", "value": true, "why": "..."}]}
```

`features` is worked out at runtime, not hard-coded. A feature is listed only
when the running Hermes provides what it needs: the hook registered, the
payload field turned up on a real call, the config knob is on. Features that
depend on events (`usage`, `status`, `subagents`) are listed once such an event
has actually gone out, including events another process pushed through
`POST /keryx/publish`. The app uses the list to tell "this install cannot do
that" from "that is broken".

| feature | listed when |
|---------|-------------|
| `stream`, `stream.chat_key`, `publish`, `toolsets` | always |
| `reasoning` | `on_stream_delta` registered and `plugins.stream_reasoning_deltas: true` |
| `interim` | `on_interim_message` registered |
| `tools` | `pre_tool_call` + `post_tool_call` registered |
| `tools.diff` | a tool call carried `tool_call_id` and Hermes' edit-diff helpers imported |
| `segment` | a stream delta carried `iteration` |
| `stop.turn` | `post_llm_call` registered (one `stop` per turn, not per API call) |
| `usage` / `status` / `subagents` | a frame of that kind has been published |
| `thinking` | the `llm_request` middleware registered |
| panel names, `proxy` | the panel routes mounted; the relay is on |

`hints` lists the Hermes config knobs that are still off, each with a reason.
The gateway also logs each one at startup.

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

Python 3.10+ and `aiohttp` (already in Hermes' environment), and hermes-agent
**0.21.3 or newer**. `plugin.yaml` declares `requires_hermes: ">=0.21.3"`, so on
a directory install an older Hermes skips the plugin before importing it and
logs why.

Every hook is checked against the host's `VALID_HOOKS` before registering.
Hermes accepts an unknown hook name with only a warning, so a hook that would
never fire has to be caught here. A hook this Hermes lacks is skipped with an
info line, and the feature it feeds is not advertised. 0.21.3, for example, has
no `pre_auxiliary_call` / `post_auxiliary_call`, so it gets no compaction
status rows. The same goes for the `llm_request` middleware.

At load Hermes logs `capability_check plugin=keryx-stream
capability=tools.override decision=deny`. That is expected. The loader checks
the tool-override grant for every non-bundled plugin, whatever it registers,
and keryx-stream registers no tools and overrides none. No grant is needed.

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
  thinking_kwargs: auto        # reasoning-dial middleware: auto (local custom brains only) | true | false
  stop_hold_ms: 3000           # longest a turn's stop waits for lagging deltas
  stop_quiet_ms: 300           # a wire this quiet after post_llm_call counts as done
  idle_stop_s: 45              # watchdog stop for a turn post_llm_call never ended
```

For full parity also set these Hermes knobs. `/keryx/health` lists whichever
are still off under `hints`:

```yaml
plugins:
  stream_reasoning_deltas: true   # Hermes hands reasoning tokens to plugins only with this on
compression:
  progress_notices: true          # otherwise a compacting turn reads as a hang
display:
  platforms:
    matrix:
      streaming: false            # Matrix users: the side-channel already carries the tokens;
                                  # edit-streaming would repeat them as m.replace edits
```

`KERYX_THINKING_KWARGS=off` in the environment turns the reasoning middleware
off without editing config.

`host: "0.0.0.0"` lets a phone on your LAN or tailnet reach the port. Every
route is bearer-authed and the plugin refuses all requests when no token is
set, but if the machine is reachable from networks you don't trust, bind
`127.0.0.1` and publish the port through your VPN (e.g. `tailscale serve`).

The bearer token is a **secret**, so it comes from the environment — set
`KERYX_STREAM_TOKEN`, or reuse your existing `API_SERVER_KEY`. Every route
except `/keryx/health` requires `Authorization: Bearer <token>`.

Not every process that loads the plugin has the gateway's environment. A cron
or kanban worker on a multi-profile gateway starts with a scrubbed one. So the
token is looked up in this order: the process environment, the `.env` of the
process's own Hermes home, the active profile's secret scope, and the `.env` of
the routed profile. Nothing found is written back into the environment. A
forwarder that started with no token retries the lookup (at most every 30 s)
instead of posting unauthenticated for the life of the worker.

## How it works

- `register(ctx)` registers the observer hooks and starts the server on its
  own daemon thread (its own event loop) — so it adds no core route.
- `on_stream_end` fires once per API call, so it never ends the turn;
  `post_llm_call` does. The `stop` it produces waits (at most `stop_hold_ms`)
  until the last delta is on the wire, goes out once, and is preceded by the
  `usage` frame, because the app hangs up at `stop`.
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
