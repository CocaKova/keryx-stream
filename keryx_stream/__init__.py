"""keryx-stream — a standalone Hermes plugin: live token streaming + toolset
control for the Keryx Android client, over the plugin surface only.

It subscribes to the gateway's generic streaming observer hooks
(``on_stream_delta`` / ``on_stream_segment`` / ``on_stream_end``) and mirrors
each turn's tokens to its own SSE side-channel that the app subscribes to — so
Keryx renders tokens live while the chat itself stays on Matrix (multi-device
sync, history, push). It also serves a toolset view/toggle for the app.

No core files are patched: streaming comes from the public hooks, the SSE server
runs on the plugin's own thread, and toolsets are read/written through the same
``hermes_cli`` helpers core uses. Install into ``~/.hermes/plugins/`` (or via the
pip entry point in pyproject.toml).

Requires a hermes-agent that provides the stream observer hooks
(NousResearch/hermes-agent#65077).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from dataclasses import dataclass, field
from typing import List

from .hub import hub

logger = logging.getLogger("keryx_stream")

_STREAM_HOOKS = ("on_stream_delta", "on_stream_segment", "on_stream_end")


@dataclass
class PluginConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8646
    default_platform: str = "matrix"
    token: str = ""  # secret — from env, never config.yaml
    toolsets_locked: List[str] = field(default_factory=list)
    toolsets_forbidden: List[str] = field(default_factory=list)


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
        toolsets_locked=list(toolsets.get("locked", []) or []),
        toolsets_forbidden=list(toolsets.get("forbidden", []) or []),
    )


def _platform_of(metadata, default: str) -> str:
    if isinstance(metadata, dict):
        p = metadata.get("platform")
        if p:
            return str(p).strip().lower()
    return default


def _make_hook_callbacks(config: PluginConfig):
    """Return the three hook callbacks bound to this config. Exposed for tests."""

    def on_delta(*, chat_id=None, metadata=None, delta="", **_):
        if chat_id and delta:
            hub.publish_threadsafe(
                _platform_of(metadata, config.default_platform), str(chat_id), "delta", delta
            )

    def on_segment(*, chat_id=None, metadata=None, **_):
        if chat_id:
            hub.publish_threadsafe(
                _platform_of(metadata, config.default_platform), str(chat_id), "segment", None
            )

    def on_end(*, chat_id=None, metadata=None, **_):
        if chat_id:
            hub.publish_threadsafe(
                _platform_of(metadata, config.default_platform), str(chat_id), "stop", None
            )

    return {
        "on_stream_delta": on_delta,
        "on_stream_segment": on_segment,
        "on_stream_end": on_end,
    }


def _serve_forever(config: PluginConfig) -> None:
    """Run the SSE server on a private event loop in this thread. Best-effort:
    a bind failure (another process already serving, e.g. a CLI invocation that
    also loaded the plugin) is logged, not raised."""
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
        logger.debug("keryx-stream: not serving on %s:%s (%s)", config.host, config.port, exc)
        return
    logger.info("keryx-stream: SSE side-channel listening on %s:%s", config.host, config.port)
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()


_server_thread: "threading.Thread | None" = None


def register(ctx) -> None:
    """Plugin entry point (called by Hermes' PluginManager at discovery)."""
    config = load_config()
    if not config.enabled:
        logger.info("keryx-stream: disabled via config.yaml (keryx_stream.enabled=false)")
        return
    if not config.token:
        logger.warning(
            "keryx-stream: no bearer token (set KERYX_STREAM_TOKEN or API_SERVER_KEY) — "
            "the SSE side-channel will refuse all requests until one is set."
        )

    # Mirror the live token stream to the hub via the generic observer hooks.
    for hook_name, cb in _make_hook_callbacks(config).items():
        try:
            ctx.register_hook(hook_name, cb)
        except Exception:
            logger.warning(
                "keryx-stream: hook '%s' unavailable — this hermes-agent predates the "
                "stream observer hooks (see NousResearch/hermes-agent#65077); live "
                "streaming will be inactive.",
                hook_name,
            )

    # Serve the side-channel from our own thread so we never add a core route.
    global _server_thread
    if _server_thread is None or not _server_thread.is_alive():
        _server_thread = threading.Thread(
            target=_serve_forever, args=(config,), name="keryx-stream-sse", daemon=True
        )
        _server_thread.start()
