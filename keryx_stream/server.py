"""The Keryx side-channel SSE server.

A small self-contained aiohttp app the plugin runs on its own thread/loop — it
adds NO routes to the gateway's api_server (plugins must not touch core), so it
is fully decoupled. Routes:

  GET  /keryx/stream?platform=<p>&chat_id=<id>  — transient SSE of one turn's
       token deltas (event: delta / segment / reasoning / stop / ping).
  POST /keryx/publish                           — bearer-authed ingest for
       non-hub processes (forward mode): same frames the hooks emit, published
       into this process's hub.
  GET  /keryx/toolsets?platform=<p>             — toolset view for the platform.
  PUT  /keryx/toolsets/{name}                   — toggle one toolset.
  GET  /keryx/health                            — liveness + version + the
       feature list, so the app can tell a missing panel from an old plugin.
  /keryx/*                                      — the app's panel routes
       (panels.py): reasoning dial, config, brains, kanban, skills, pets …
  anything else                                 — relayed to the native API
       server (proxy.py), so the app needs one URL, not two.

All routes except the health probes require ``Authorization: Bearer <token>``.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging

from . import toolsets as toolsets_mod
from .hub import drain_coalesced, hub
from .scope import make_scope_middleware
from .version import FEATURES, __version__

logger = logging.getLogger("keryx_stream.server")


# Served by this module itself; everything else in FEATURES comes from panels.py.
_CORE_FEATURES = {"stream", "stream.chat_key", "publish", "toolsets"}


def _unauthorized(web):
    return web.json_response(
        {"error": {"message": "Invalid or missing bearer token"}}, status=401
    )


def build_app(config) -> object:
    """Build the aiohttp Application for the given PluginConfig."""
    from aiohttp import web

    def check_auth(request: web.Request) -> web.Response | None:
        if not config.token:
            # No token configured → refuse everything rather than serve open.
            return _unauthorized(web)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return _unauthorized(web)
        presented = header[7:].strip()
        if not hmac.compare_digest(presented, config.token):
            return _unauthorized(web)
        return None

    async def handle_health(request):
        features = [f for f in FEATURES if f in _CORE_FEATURES or request.app.get("keryx_panels")]
        features += ["proxy"] if config.upstream_url else []
        return web.json_response({
            "ok": True, "plugin": "keryx-stream", "version": __version__, "features": features,
        })

    async def handle_stream(request: web.Request) -> web.StreamResponse:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        platform = request.query.get("platform", config.default_platform)
        chat_id = request.query.get("chat_id", "").strip()
        if not chat_id:
            return web.json_response(
                {"error": {"message": "chat_id is required"}}, status=400
            )

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        sub = hub.subscribe(platform, chat_id)
        try:
            while True:
                try:
                    first = await asyncio.wait_for(sub.queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    await resp.write(b"event: ping\ndata: {}\n\n")
                    continue
                frames, stop = drain_coalesced(sub.queue, first)
                for event, text in frames:
                    payload = json.dumps({"text": text} if text is not None else {})
                    await resp.write(
                        f"event: {event}\ndata: {payload}\n\n".encode()
                    )
                if stop:
                    break  # transient channel: one turn per subscription
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            hub.unsubscribe(platform, chat_id, sub)
        try:
            await resp.write_eof()
        except Exception:
            pass
        return resp

    async def handle_toolsets_get(request: web.Request) -> web.Response:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        try:
            platform = toolsets_mod.platform_key(
                request.query.get("platform", ""), config.default_platform
            )
        except ValueError as exc:
            return web.json_response({"error": {"message": str(exc)}}, status=400)
        snap = await asyncio.to_thread(
            toolsets_mod.snapshot, platform, config.toolsets_locked, config.toolsets_forbidden
        )
        return web.json_response(snap)

    async def handle_toolset_put(request: web.Request) -> web.Response:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        name = request.match_info["name"]
        try:
            platform = toolsets_mod.platform_key(
                request.query.get("platform", ""), config.default_platform
            )
        except ValueError as exc:
            return web.json_response({"error": {"message": str(exc)}}, status=400)
        try:
            body = await request.json()
        except Exception:
            body = {}
        enabled = bool(body.get("enabled", True))
        status, payload = await asyncio.to_thread(
            toolsets_mod.set_enabled,
            name,
            enabled,
            platform,
            config.toolsets_locked,
            config.toolsets_forbidden,
        )
        return web.json_response(payload, status=status)

    async def handle_publish(request: web.Request) -> web.Response:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": {"message": "JSON body required"}}, status=400
            )
        platform = str(body.get("platform", "")).strip().lower()
        chat_id = str(body.get("chat_id", "")).strip()
        event = str(body.get("event", "")).strip()
        if not platform or not chat_id or not event:
            return web.json_response(
                {"error": {"message": "platform, chat_id and event are required"}}, status=400
            )
        text = body.get("text")
        if text is not None and not isinstance(text, str):
            return web.json_response(
                {"error": {"message": "text must be a string or null"}}, status=400
            )
        hub.publish_threadsafe(platform, chat_id, event, text)
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[make_scope_middleware()])
    app.router.add_get("/keryx/health", handle_health)
    app.router.add_get("/keryx/stream", handle_stream)
    app.router.add_post("/keryx/publish", handle_publish)
    app.router.add_get("/keryx/toolsets", handle_toolsets_get)
    app.router.add_put("/keryx/toolsets/{name}", handle_toolset_put)
    if config.panels:
        # The panels lean on Hermes internals; the stream must not go down with
        # them if a Hermes release moves something they import.
        try:
            from .panels import register_panel_routes
            register_panel_routes(app.router, check_auth)
            app["keryx_panels"] = True
        except Exception:
            logger.warning("keryx-stream: panel routes unavailable on this Hermes — "
                           "streaming and toolsets still served", exc_info=True)
    if config.upstream_url:
        from .proxy import make_proxy_handler
        relay, cleanup = make_proxy_handler(config.upstream_url, check_auth)
        app.router.add_route("*", "/{tail:.*}", relay)
        app.on_cleanup.append(cleanup)
    return app
