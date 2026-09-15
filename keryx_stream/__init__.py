"""keryx-stream — a standalone Hermes plugin: live token streaming + tool
mirroring for the Keryx Android client, over the plugin surface only.

It subscribes to Hermes' shipped streaming and tool observer hooks
(``on_stream_start`` / ``on_stream_delta`` / ``on_stream_end`` /
``on_interim_message`` and ``pre_tool_call`` / ``post_tool_call`` — every one
carries ``session_id``, so a turn is keyed by session, on any surface) and
mirrors each turn's tokens and tool activity to its own SSE side-channel that
the app subscribes to — so Keryx renders a turn live while the chat itself
stays on its transport (Matrix multi-device sync, history, push). It also
serves a toolset view/toggle for the app.

Two run modes, picked at register time:

- **Hub mode** — this process is the first to bind the SSE port (the gateway,
  normally). Hook events publish to the in-process hub and subscribers read
  them over ``GET /keryx/stream``.
- **Forward mode** — the port is already bound by another keryx-stream
  instance (e.g. a ``hermes chat`` one-shot starting while the gateway is
  up). Hook events are POSTed to that instance's ``/keryx/publish`` route, so
  a session driven from OUTSIDE the gateway process still streams live to
  whatever subscriber is attached to the hub owner.

No core files are patched: streaming comes from the public hooks, the SSE
server runs on the plugin's own thread, and toolsets are read/written through
the same ``hermes_cli`` helpers core uses. Install into ``~/.hermes/plugins/``
(or via the pip entry point in pyproject.toml).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .hub import hub

logger = logging.getLogger("keryx_stream")

# Side-channel frame size caps — mirror the gateway-side monolithic patch so a
# subscriber sees the same payload shapes regardless of which process produced
# the turn.
_TOOL_PREVIEW_MAX = 240
_TOOL_RESULT_MAX = 2400
_TOOL_RESULT_TAIL = 800


@dataclass
class PluginConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8646
    default_platform: str = "matrix"
    token: str = ""  # secret — from env, never config.yaml
    forward_url: str = ""  # hub owner's publish endpoint; derived from port when empty
    toolsets_locked: list[str] = field(default_factory=list)
    toolsets_forbidden: list[str] = field(default_factory=list)


def load_config() -> PluginConfig:
    """Build config from the ``keryx_stream`` block in config.yaml (non-secret)
    plus the bearer token from the environment (secret)."""
    cfg = {}
    try:
        from hermes_cli.config import load_config as _load
        cfg = (_load() or {}).get("keryx_stream", {}) or {}
    except Exception:
        logger.debug("keryx-stream: could not load config.yaml block", exc_info=True)
    toolsets = cfg.get("toolsets", {}) or {}
    token = (
        os.environ.get("KERYX_STREAM_TOKEN")
        or os.environ.get("API_SERVER_KEY")
        or ""
    ).strip()
    return PluginConfig(
        enabled=bool(cfg.get("enabled", True)),
        host=str(cfg.get("host", "0.0.0.0")),
        port=int(cfg.get("port", 8646)),
        default_platform=str(cfg.get("default_platform", "matrix")).strip().lower(),
        token=token,
        forward_url=str(cfg.get("forward_url", "")).strip(),
        toolsets_locked=list(toolsets.get("locked", []) or []),
        toolsets_forbidden=list(toolsets.get("forbidden", []) or []),
    )


def _clip(value, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_middle(value, limit: int = _TOOL_RESULT_MAX, tail: int = _TOOL_RESULT_TAIL) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    head = limit - tail - 20
    return text[:head] + "\n…[truncated]…\n" + text[-tail:]


def _make_hook_callbacks(config: PluginConfig, publish: Callable[..., None]):
    """Return the shipped-hook callbacks bound to this config and a publish
    function (hub or forwarder). Exposed for tests."""

    def _key_of(surface: str | None, session_id: str | None):
        """(platform, chat_id) for a hook payload, or None when unusable.

        Every shipped hook carries ``session_id`` (agent.stream_delivery and
        agent.inline_tool_executors both stamp it), so a turn is keyed by its
        session id; ``surface`` (cli/gateway platform) namespaces it.
        """
        sid = str(session_id or "").strip()
        if not sid:
            return None
        platform = str(surface or config.default_platform).strip().lower() or config.default_platform
        return platform, sid

    def on_start(*, session_id=None, surface=None, **_):
        key = _key_of(surface, session_id)
        if key:
            publish(*key, "start", None)

    def on_delta(*, session_id=None, surface=None, delta="", kind="text", **_):
        key = _key_of(surface, session_id)
        if key and delta:
            event = "reasoning" if kind == "reasoning" else "delta"
            publish(key[0], key[1], event, delta)

    def on_interim(*, session_id=None, surface=None, text="", already_streamed=False, **_):
        # Text the subscriber already saw as deltas would duplicate on the wire.
        key = _key_of(surface, session_id)
        if key and text and not already_streamed:
            publish(key[0], key[1], "interim", text)

    def on_end(*, session_id=None, surface=None, final_text=None, **_):
        key = _key_of(surface, session_id)
        if key:
            publish(key[0], key[1], "stop", final_text if isinstance(final_text, str) else None)

    def _tool_frame(*, phase: str, tool_name: str | None = None, status: str | None = None,
                    duration_ms: int = 0, args: Any = None, result: Any = None,
                    error_message: str | None = None) -> dict[str, Any]:
        frame: dict[str, Any] = {"phase": phase, "name": str(tool_name or "tool")}
        if phase == "start":
            frame["preview"] = _clip(args, _TOOL_PREVIEW_MAX)
        else:
            frame["ok"] = (status or "ok") == "ok"
            frame["ms"] = int(duration_ms or 0)
            if error_message:
                frame["error"] = _clip(error_message, 200)
            clipped = _clip_middle(result)
            if clipped:
                frame["result"] = clipped
                frame["result_len"] = len(str(result or ""))
        return frame

    def on_pre_tool(*, session_id=None, surface=None, tool_name=None, args=None, **_):
        key = _key_of(surface, session_id)
        if key:
            publish(key[0], key[1], "tool", _tool_frame(phase="start", tool_name=tool_name, args=args))

    def on_post_tool(*, session_id=None, surface=None, tool_name=None, result=None,
                     status=None, duration_ms=0, error_message=None, **_):
        key = _key_of(surface, session_id)
        if key:
            publish(key[0], key[1], "tool", _tool_frame(
                phase="end", tool_name=tool_name, result=result, status=status,
                duration_ms=duration_ms, error_message=error_message))

    return {
        "on_stream_start": on_start,
        "on_stream_delta": on_delta,
        "on_interim_message": on_interim,
        "on_stream_end": on_end,
        "pre_tool_call": on_pre_tool,
        "post_tool_call": on_post_tool,
    }


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
    owns the side-channel) or HTTP forwarder (someone else does)."""
    if loop is not None:
        return hub.publish_threadsafe
    from .forwarder import Forwarder
    forwarder = Forwarder(
        config.forward_url or f"http://127.0.0.1:{config.port}/keryx/publish",
        config.token,
    )
    return forwarder.publish


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
            "keryx-stream: no bearer token (set KERYX_STREAM_TOKEN or API_SERVER_KEY) — "
            "the SSE side-channel will refuse all requests until one is set."
        )

    loop = _start_sse_server(config)
    publish = _publisher(config, loop)
    if loop is None and not config.token:
        logger.warning("keryx-stream: forward mode without a bearer token — "
                       "the hub owner will refuse every forwarded event.")

    for hook_name, cb in _make_hook_callbacks(config, publish).items():
        try:
            ctx.register_hook(hook_name, cb)
        except Exception:
            logger.warning("keryx-stream: could not register hook '%s' — live "
                           "streaming will be inactive for it.", hook_name)

    if loop is not None and (_server_thread is None or not _server_thread.is_alive()):
        _server_thread = threading.Thread(
            target=_serve_loop, args=(loop,), name="keryx-stream-sse", daemon=True
        )
        _server_thread.start()
