"""keryx-stream — a standalone Hermes plugin: live token streaming + tool
mirroring for the Keryx Android client, over the plugin surface only.

It subscribes to Hermes' shipped observer hooks and mirrors each turn to its own
SSE side-channel the app subscribes to, so Keryx renders a turn live while the
chat itself stays on its transport (Matrix multi-device sync, history, push):

- ``on_stream_start`` / ``on_stream_delta`` / ``on_stream_end`` /
  ``on_interim_message`` — tokens (text + reasoning), segment boundaries;
- ``pre_tool_call`` / ``post_tool_call`` — tool frames, and inline edit diffs
  keyed by ``tool_call_id``;
- ``post_api_request`` — the last call's prompt size, for the usage frame;
- ``post_llm_call`` — the end of the turn: one held ``stop`` (turns.py);
- ``subagent_start`` / ``subagent_stop`` — the delegation wing;
- ``pre_auxiliary_call`` / ``post_auxiliary_call`` — compaction status;
- ``pre_gateway_dispatch`` — borrows the session store for chat-key routing;
- ``llm_request`` middleware — the thinking dial for local brains (thinking.py).

Two run modes, picked at register time:

- **Hub mode** — this process is the first to bind the SSE port (the gateway,
  normally). Hook events publish to the in-process hub and subscribers read
  them over ``GET /keryx/stream``.
- **Forward mode** — the port is already bound by another keryx-stream
  instance (a ``hermes chat`` one-shot, a cron or kanban worker). Hook events
  are POSTed to that instance's ``/keryx/publish`` route, so a session driven
  from OUTSIDE the gateway process still streams live to its subscribers.

No core files are patched and nothing on the agent is monkeypatched: streaming
comes from the public hooks, the SSE server runs on the plugin's own thread, and
toolsets are read/written through the same ``hermes_cli`` helpers core uses.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .frames import (
    EditDiffs,
    compaction_text,
    status_frame,
    subagent_start_frame,
    subagent_stop_frame,
    subagent_tool_frame,
    tool_end_frame,
    tool_start_frame,
)
from .hub import hub
from .probe import config_hints, probe
from .routing import SessionRoutes
from .turns import TurnTracker

logger = logging.getLogger("keryx_stream")

_SURFACE_CACHE_MAX = 512
_COMPACTION_TASKS = ("compression",)  # aux_task of the compaction summary call


@dataclass
class PluginConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8646
    default_platform: str = "matrix"
    token: str = ""  # secret — from env / .env / profile scope, never config.yaml
    token_source: str = ""
    forward_url: str = ""  # hub owner's publish endpoint; derived from port when empty
    panels: bool = True  # serve the app's /keryx/* panel routes (panels.py)
    upstream_url: str = "http://127.0.0.1:8642"  # native API server to relay to; "" = off
    toolsets_locked: list[str] = field(default_factory=list)
    toolsets_forbidden: list[str] = field(default_factory=list)
    # "auto" = register the thinking middleware when the configured provider is a
    # local custom endpoint; True = always; False = never.
    thinking_kwargs: Any = "auto"
    stop_hold_ms: int = 3000   # longest a stop waits for lagging deltas
    stop_quiet_ms: int = 300   # a wire this quiet after post_llm_call is done
    idle_stop_s: float = 45.0  # watchdog for turns post_llm_call never ends


def load_config() -> PluginConfig:
    """Build config from the ``keryx_stream`` block in config.yaml (non-secret)
    plus the bearer token (secret; see auth.py for where it is looked up)."""
    cfg: dict = {}
    try:
        from hermes_cli.config import load_config as _load
        cfg = (_load() or {}).get("keryx_stream", {}) or {}
    except Exception:
        logger.debug("keryx-stream: could not load config.yaml block", exc_info=True)
    toolsets = cfg.get("toolsets", {}) or {}
    from .auth import resolve_token

    token, source = resolve_token()
    return PluginConfig(
        enabled=bool(cfg.get("enabled", True)),
        host=str(cfg.get("host", "0.0.0.0")),
        port=int(cfg.get("port", 8646)),
        default_platform=str(cfg.get("default_platform", "matrix")).strip().lower(),
        token=token,
        token_source=source,
        forward_url=str(cfg.get("forward_url", "")).strip(),
        panels=bool(cfg.get("panels", True)),
        upstream_url=_upstream_url(cfg),
        toolsets_locked=list(toolsets.get("locked", []) or []),
        toolsets_forbidden=list(toolsets.get("forbidden", []) or []),
        thinking_kwargs=cfg.get("thinking_kwargs", "auto"),
        stop_hold_ms=int(cfg.get("stop_hold_ms", 3000)),
        stop_quiet_ms=int(cfg.get("stop_quiet_ms", 300)),
        idle_stop_s=float(cfg.get("idle_stop_s", 45.0)),
    )


def _upstream_url(cfg: dict) -> str:
    """The native API server the plugin's port fronts. Explicit config wins
    (``""`` or ``false`` turns the relay off); otherwise follow the port the
    API server itself is told to use."""
    import os

    if "upstream_url" in cfg:
        raw = cfg.get("upstream_url")
        return "" if raw in (None, False) else str(raw).strip()
    port = os.environ.get("API_SERVER_PORT", "").strip() or "8642"
    return f"http://127.0.0.1:{port}"


# --- context window for the usage frame ---------------------------------------

_CTX_CACHE: dict[tuple[str, str, str], int] = {}


def _context_length(model: str, provider: str, base_url: str) -> int:
    """The window the agent compacts against: ``model.context_length`` when the
    turn ran on the configured default model (what agent_init scopes it to),
    else Hermes' own resolver. Cached per route; 0 when unknown."""
    key = (model, provider, base_url)
    if key in _CTX_CACHE:
        return _CTX_CACHE[key]
    value = 0
    try:
        from hermes_cli.config import load_config as _load

        model_cfg = (_load() or {}).get("model") or {}
        default = model_cfg.get("default")
        cfg_provider = str(model_cfg.get("provider") or "").strip().lower()
        if (isinstance(default, str) and default.strip() == model
                and (not provider or not cfg_provider or provider.strip().lower() == cfg_provider)):
            value = int(model_cfg.get("context_length") or 0)
    except Exception:
        logger.debug("keryx-stream: config context_length unreadable", exc_info=True)
    if value <= 0 and model:
        try:
            from agent.model_metadata import get_model_context_length

            value = int(get_model_context_length(model, base_url=base_url or "", provider=provider or "") or 0)
        except Exception:
            logger.debug("keryx-stream: context length lookup failed", exc_info=True)
            value = 0
    if value > 0:
        _CTX_CACHE[key] = value
    return value


# --- hook callbacks -----------------------------------------------------------

class HookSet(dict):
    """hook name → callback, plus the tracker they share (exposed for tests)."""

    tracker: TurnTracker


def _make_hook_callbacks(config: PluginConfig, publish: Callable[..., None],
                         routes: SessionRoutes | None = None, *, background: bool = True,
                         diffs: EditDiffs | None = None) -> HookSet:
    """Return the hook callbacks bound to this config and a publish function
    (hub or forwarder). Exposed for tests."""
    routes = routes or SessionRoutes()
    diffs = diffs or EditDiffs()
    surfaces: dict[str, str] = {}  # session_id -> surface, learned from the stream hooks

    def learn(surface: Any, session_id: Any) -> str:
        sid = str(session_id or "").strip()
        platform = str(surface or "").strip().lower()
        if sid and platform:
            surfaces[sid] = platform
            while len(surfaces) > _SURFACE_CACHE_MAX:
                surfaces.pop(next(iter(surfaces)))
        return sid

    def emit(sid: str, event: str, text: str | None) -> None:
        """Publish under the session key AND the chat key the session routes to
        (see routing.py) — a chat-transport client only knows the latter."""
        # The tool / api / llm hooks carry no surface — reuse the one this
        # session's stream hooks announced so every frame lands on one key.
        platform = surfaces.get(sid) or config.default_platform
        publish(platform, sid, event, text)
        # Resolve on the per-API-call events; tokens ride the cached key.
        chat = routes.chat_key(sid) if event in ("delta", "reasoning") else routes.resolve(sid)
        if chat and chat != (platform, sid):
            publish(chat[0], chat[1], event, text)

    tracker = TurnTracker(
        emit,
        hold_s=max(0.0, config.stop_hold_ms / 1000.0),
        quiet_s=max(0.0, config.stop_quiet_ms / 1000.0),
        idle_stop_s=max(1.0, float(config.idle_stop_s)),
        context_length=_context_length,
        background=background,
    )

    def on_dispatch(*, session_store=None, gateway=None, **_):
        """Observer only — borrows the gateway's session store, never touches
        the event (returning None lets dispatch proceed untouched)."""
        routes.bind(session_store or getattr(gateway, "session_store", None))
        return None

    def on_start(*, session_id=None, surface=None, turn_id="", **_):
        sid = learn(surface, session_id)
        if sid:
            tracker.start(sid, str(turn_id or ""))

    def on_delta(*, session_id=None, surface=None, delta="", kind="text", turn_id="", iteration=None, **_):
        sid = learn(surface, session_id)
        if not sid or not delta:
            return
        if isinstance(iteration, int) and not isinstance(iteration, bool):
            probe.seen("iteration")
        else:
            iteration = None
        tracker.delta(sid, str(turn_id or ""), iteration, "reasoning" if kind == "reasoning" else "text", delta)

    def on_interim(*, session_id=None, surface=None, text="", already_streamed=False, turn_id="", **_):
        # Text the subscriber already saw as deltas would duplicate on the wire.
        sid = learn(surface, session_id)
        if sid and text and not already_streamed:
            tracker.interim(sid, str(turn_id or ""), text)

    def on_end(*, session_id=None, surface=None, turn_id="", **_):
        """Once per API call — never the end of the turn (post_llm_call is)."""
        sid = learn(surface, session_id)
        if sid:
            tracker.stream_end(sid, str(turn_id or ""))

    def on_pre_tool(*, session_id=None, tool_name=None, args=None, tool_call_id="", task_id="",
                    turn_id="", surface=None, **_):
        sid = learn(surface, session_id)
        if not sid:
            return None
        child = tracker.child_of(sid)
        if child is not None:
            tracker.child_frame(child, subagent_tool_frame(
                child_id=child.child_id, tool_name=tool_name, args=args, child_session=sid))
        if tool_call_id:
            probe.seen("tool_call_id")
            diffs.capture(str(tool_call_id), str(tool_name or ""), args, task_id or None)
        tracker.tool(sid, str(turn_id or ""), tool_start_frame(tool_name, args), phase="start")
        return None  # never a block directive

    def on_post_tool(*, session_id=None, tool_name=None, args=None, result=None, status=None,
                     duration_ms=0, error_message=None, tool_call_id="", turn_id="", surface=None, **_):
        sid = learn(surface, session_id)
        if not sid:
            return
        tracker.tool(sid, str(turn_id or ""), tool_end_frame(
            tool_name, result=result, status=status, duration_ms=duration_ms,
            error_message=error_message), phase="end")
        if tool_call_id:
            # Its own frame after "end": the app attaches it to the closed row.
            diff = diffs.frame(str(tool_call_id), str(tool_name or ""), args, result)
            if diff is not None:
                tracker.tool(sid, str(turn_id or ""), diff, phase="diff")

    def on_api_end(*, session_id=None, turn_id="", usage=None, model="", provider="", base_url="",
                   **_):
        sid = str(session_id or "").strip()
        if not sid or not isinstance(usage, dict):
            return
        try:
            prompt = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            prompt = 0
        if prompt > 0:
            tracker.api_end(sid, str(turn_id or ""), {
                "prompt_tokens": prompt, "model": str(model or ""),
                "provider": str(provider or ""), "base_url": str(base_url or ""),
            })

    def on_turn_end(*, session_id=None, turn_id="", assistant_response=None, **_):
        sid = str(session_id or "").strip()
        if sid:
            tracker.turn_end(sid, str(turn_id or ""),
                             assistant_response if isinstance(assistant_response, str) else "")

    def on_subagent_start(*, parent_session_id=None, parent_turn_id="", child_session_id=None,
                          child_subagent_id=None, child_role=None, child_goal=None, **_):
        parent = str(parent_session_id or "").strip()
        if not parent:
            return
        child_sid = str(child_session_id or "").strip()
        child_id = str(child_subagent_id or "").strip() or child_sid or "child"
        tracker.child_started(parent, str(parent_turn_id or ""), child_sid, child_id,
                              subagent_start_frame(child_id=child_id, role=child_role,
                                                   goal=child_goal, child_session=child_sid))

    def on_subagent_stop(*, parent_session_id=None, parent_turn_id="", child_session_id=None,
                         child_role=None, child_summary=None, child_status=None,
                         tool_call_history=None, duration_ms=0, **_):
        parent = str(parent_session_id or "").strip()
        if not parent:
            return
        child_sid = str(child_session_id or "").strip()
        known = tracker.child_of(child_sid) if child_sid else None
        child_id = known.child_id if known is not None else (child_sid or "child")
        tracker.child_stopped(parent, str(parent_turn_id or ""), child_sid, subagent_stop_frame(
            child_id=child_id, role=child_role, status=child_status, summary=child_summary,
            duration_ms=duration_ms, tool_calls=tool_call_history, child_session=child_sid))

    def on_aux_pre(*, aux_task="", session_id="", turn_id="", approx_input_tokens=0, **_):
        sid = str(session_id or "").strip()
        if sid and str(aux_task or "") in _COMPACTION_TASKS:
            tracker.status(sid, str(turn_id or ""),
                           status_frame("compacting", compaction_text(approx_input_tokens)),
                           compacting=True)

    def on_aux_post(*, aux_task="", session_id="", turn_id="", error=None, **_):
        sid = str(session_id or "").strip()
        if sid and str(aux_task or "") in _COMPACTION_TASKS:
            if error:
                tracker.status(sid, str(turn_id or ""), status_frame("warning", f"⚠ Compaction call failed: {error}"))
            tracker.status(sid, str(turn_id or ""), status_frame("ready"), compacting=False)

    hooks = HookSet({
        "on_stream_start": on_start,
        "on_stream_delta": on_delta,
        "on_interim_message": on_interim,
        "on_stream_end": on_end,
        "pre_tool_call": on_pre_tool,
        "post_tool_call": on_post_tool,
        "post_api_request": on_api_end,
        "post_llm_call": on_turn_end,
        "subagent_start": on_subagent_start,
        "subagent_stop": on_subagent_stop,
        "pre_auxiliary_call": on_aux_pre,
        "post_auxiliary_call": on_aux_post,
        "pre_gateway_dispatch": on_dispatch,
    })
    hooks.tracker = tracker
    return hooks


# --- server / publisher -------------------------------------------------------

def _start_sse_server(config: PluginConfig) -> asyncio.AbstractEventLoop | None:
    """Bind the SSE side-channel synchronously (so register() knows whether
    this process owns the hub) and return the loop its thread should run.

    Returns None when the port is taken — that instance is the hub owner and
    this process must forward to it instead."""
    from aiohttp import web

    from .server import build_app

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = build_app(config)
    runner = web.AppRunner(app)
    try:
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, config.host, config.port)
        loop.run_until_complete(site.start())
    except OSError as exc:
        logger.info(
            "keryx-stream: SSE port %s not bindable here (%s) — another keryx-stream "
            "instance owns the hub; hook events will be forwarded to it.",
            config.port, exc,
        )
        try:
            loop.run_until_complete(runner.cleanup())
        except Exception:
            pass
        loop.close()
        return None
    logger.info("keryx-stream: SSE side-channel listening on %s:%s", config.host, config.port)
    return loop


def _serve_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


def _publisher(config: PluginConfig, loop: asyncio.AbstractEventLoop | None):
    """The publish function the hook callbacks close over: hub (this process
    owns the side-channel) or HTTP forwarder (someone else does). Either way
    every event is noted by the probe, so /keryx/health reflects what was fed."""
    if loop is not None:
        def publish(platform, chat_id, event, text):
            probe.note_event(event, text)
            hub.publish_threadsafe(platform, chat_id, event, text)
        return publish
    from .auth import resolve_token
    from .forwarder import Forwarder
    forwarder = Forwarder(
        config.forward_url or f"http://127.0.0.1:{config.port}/keryx/publish",
        config.token,
        resolve=resolve_token,
    )
    return forwarder.publish


def _valid_names(module: str, attr: str) -> set[str] | None:
    """The hook / middleware names this Hermes accepts, or None when it cannot say.
    ``register_hook`` stores unknown names with only a warning
    (hermes_cli/plugins.py ``_track_callback``), so registration succeeding is
    not detection — membership is."""
    try:
        import importlib

        return set(getattr(importlib.import_module(module), attr))
    except Exception:
        return None


def _wants_thinking(config: PluginConfig) -> bool:
    from .thinking import is_local_provider, killed

    if killed(config.thinking_kwargs):
        return False
    if config.thinking_kwargs is True or str(config.thinking_kwargs).strip().lower() in {"on", "true", "yes", "1"}:
        return True
    # auto: only when the configured brain is a local custom endpoint — Hermes
    # deep-copies the provider request for every registered llm_request
    # middleware, so a cloud-only install should not pay for a no-op.
    try:
        from hermes_cli.config import load_config as _load

        provider = str(((_load() or {}).get("model") or {}).get("provider") or "")
    except Exception:
        return False
    return is_local_provider(provider)


def _log_hints(forwarding: bool) -> None:
    try:
        from hermes_cli.config import load_config as _load

        hints = config_hints(_load() or {})
    except Exception:
        return
    for hint in hints:
        # One line per missing knob; quiet in forward-mode children (cron/kanban
        # workers load the plugin on every run).
        (logger.debug if forwarding else logger.warning)(
            "keryx-stream: config hint — set %s: %s (%s)", hint["key"],
            str(hint["value"]).lower(), hint["why"])


_server_thread: threading.Thread | None = None


def register(ctx) -> None:
    """Plugin entry point (called by Hermes' PluginManager at discovery)."""
    global _server_thread
    config = load_config()
    if not config.enabled:
        logger.info("keryx-stream: disabled via config.yaml (keryx_stream.enabled=false)")
        return
    if not config.token:
        logger.warning(
            "keryx-stream: no bearer token found (KERYX_STREAM_TOKEN or API_SERVER_KEY in the "
            "environment, the Hermes home's .env, or the profile secret scope) — the SSE "
            "side-channel will refuse all requests until one is set."
        )

    loop = _start_sse_server(config)
    publish = _publisher(config, loop)
    if loop is None and not config.token:
        logger.warning("keryx-stream: forward mode without a bearer token — "
                       "the hub owner will refuse every forwarded event.")

    valid_hooks = _valid_names("hermes_cli.plugins", "VALID_HOOKS")
    for hook_name, cb in _make_hook_callbacks(config, publish).items():
        if valid_hooks is not None and hook_name not in valid_hooks:
            logger.info("keryx-stream: this Hermes has no '%s' hook — that part of the "
                        "side-channel stays off.", hook_name)
            continue
        try:
            ctx.register_hook(hook_name, cb)
            probe.registered(hook_name)
        except Exception:
            logger.warning("keryx-stream: could not register hook '%s' — live "
                           "streaming will be inactive for it.", hook_name)
    if EditDiffs.available():
        probe.seen("edit_helpers")

    if _wants_thinking(config):
        valid_mw = _valid_names("hermes_cli.middleware", "VALID_MIDDLEWARE")
        register_mw = getattr(ctx, "register_middleware", None)
        if register_mw is None or (valid_mw is not None and "llm_request" not in valid_mw):
            logger.info("keryx-stream: this Hermes has no llm_request middleware — the "
                        "thinking dial for local brains stays off.")
        else:
            from .thinking import make_llm_request_middleware

            try:
                register_mw("llm_request", make_llm_request_middleware(config.thinking_kwargs))
                probe.registered("middleware:llm_request")
            except Exception:
                logger.warning("keryx-stream: could not register llm_request middleware", exc_info=True)

    _log_hints(forwarding=loop is None)

    if loop is not None and (_server_thread is None or not _server_thread.is_alive()):
        _server_thread = threading.Thread(
            target=_serve_loop, args=(loop,), name="keryx-stream-sse", daemon=True
        )
        _server_thread.start()
