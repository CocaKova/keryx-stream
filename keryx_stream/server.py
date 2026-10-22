"""The Keryx side-channel SSE server.

A small self-contained aiohttp app the plugin runs on its own thread/loop — it
adds NO routes to the gateway's api_server (plugins must not touch core), so it
is fully decoupled. Routes:

  GET  /keryx/stream?platform=<p>&chat_id=<id>  — transient SSE of one turn's
       token deltas (event: delta / segment / reasoning / stop / ping).
  POST /keryx/publish                           — bearer-authed ingest for
       non-hub processes (forward mode): same frames the hooks emit, published
       into this process's hub. Master key only.
  POST /keryx/pair/claim                        — trades the one-time code
       from ``hermes keryx pair`` for a device token (pairing.py).
  POST /keryx/tour/respond                      — the phone's answer to a
       ``keryx_tour`` call (tour.py).
  GET  /keryx/tour/wait?session_id=&call_id=    — the tool's long poll for that
       answer. Master key only.
  GET  /keryx/toolsets?platform=<p>             — toolset view for the platform.
  PUT  /keryx/toolsets/{name}                   — toggle one toolset.
  GET  /keryx/update/channel                    — the install's update channel
       (stable releases or every change on main) and where it stands
       (update_channel.py).
  PUT  /keryx/update/channel                    — switch it: {"channel", "stay"}.
  GET  /keryx/health                            — liveness + version + the
       DERIVED feature list (version.features) + config ``hints``, so the app
       can tell a missing panel from an old plugin and an operator sees which
       knobs to set.
  /keryx/*                                      — the app's panel routes
       (panels.py): reasoning dial, config, brains, kanban, skills, pets …
  anything else                                 — relayed to the native API
       server (proxy.py), so the app needs one URL, not two.

All routes except the health probes and the pairing claim require
``Authorization: Bearer <token>``: the master key (``API_SERVER_KEY``), or a
paired phone's device token everywhere but ``/keryx/publish``.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging

from . import pairing, tour
from . import toolsets as toolsets_mod
from . import update_channel as update_channel_mod
from .hub import drain_coalesced, hub
from .pairing import DeviceTokens
from .probe import probe
from .scope import make_scope_middleware
from .version import REQUIRES_HERMES, __version__, features

logger = logging.getLogger("keryx_stream.server")

# A release lookup (GitHub / the channel records, a remote branch tip) that takes
# longer than this answers with the channel facts alone and says it couldn't check.
UPDATE_CHECK_TIMEOUT_S = 30.0


def _panels_key():
    from aiohttp import web

    return web.AppKey("keryx_panel_features", list)


_PANELS_KEY = _panels_key()


def _unauthorized(web):
    return web.json_response(
        {"error": {"message": "Invalid or missing bearer token"}}, status=401
    )


def build_app(config) -> object:
    """Build the aiohttp Application for the given PluginConfig."""
    from aiohttp import web

    devices = DeviceTokens()

    def check_auth(request: web.Request, master_only: bool = False) -> web.Response | None:
        if not config.token:
            # A token that appeared after startup (.env edited, secret source
            # hydrated late) is picked up here; still none → refuse everything
            # rather than serve open.
            from .auth import resolve_token

            config.token, config.token_source = resolve_token()
        if not config.token:
            return _unauthorized(web)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return _unauthorized(web)
        presented = header[7:].strip()
        if hmac.compare_digest(presented, config.token):
            return None
        # A paired phone's own token (pairing.py): the app's routes, never the
        # forwarders' publish door.
        if not master_only and devices.verify(presented):
            return None
        return _unauthorized(web)

    async def handle_health(request):
        update_channel_ok = await asyncio.to_thread(update_channel_mod.supported)
        feats = await asyncio.to_thread(
            features, panel_features=request.app[_PANELS_KEY],
            proxy=bool(config.upstream_url), probe=probe, update_channel=update_channel_ok)
        hints = await asyncio.to_thread(probe.hints)
        return web.json_response({
            "ok": True, "plugin": "keryx-stream", "version": __version__,
            "requires_hermes": REQUIRES_HERMES, "features": feats, "hints": hints,
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

    def _no_channels():
        return web.json_response(
            {"error": "unsupported", "message": "This Hermes has no per-install update channel."},
            status=404)

    async def _channel_status() -> web.Response:
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(update_channel_mod.status), timeout=UPDATE_CHECK_TIMEOUT_S)
        except asyncio.TimeoutError:
            payload = await asyncio.to_thread(update_channel_mod.status, offline=True)
            if payload["can_set"]:
                payload["message"] = "Couldn't reach the update source in time. Try again."
        return web.json_response(payload, headers={"Cache-Control": "no-store"})

    async def handle_update_channel_get(request: web.Request) -> web.Response:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        if not await asyncio.to_thread(update_channel_mod.supported):
            return _no_channels()
        if request.query.get("force", "").lower() in ("1", "true", "yes"):
            try:
                payload = await asyncio.wait_for(
                    asyncio.to_thread(update_channel_mod.status, force=True),
                    timeout=UPDATE_CHECK_TIMEOUT_S)
                return web.json_response(payload, headers={"Cache-Control": "no-store"})
            except asyncio.TimeoutError:
                pass
        return await _channel_status()

    async def handle_update_channel_put(request: web.Request) -> web.Response:
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        if not await asyncio.to_thread(update_channel_mod.supported):
            return _no_channels()
        try:
            body = await request.json()
        except Exception:
            body = None
        channel = body.get("channel") if isinstance(body, dict) else None
        if channel not in update_channel_mod.CHANNELS:
            return web.json_response({"error": "bad_channel"}, status=400)
        try:
            await asyncio.to_thread(update_channel_mod.switch, channel, body.get("stay") is True)
        except update_channel_mod.NotSettable as exc:
            return web.json_response({"error": "not_settable", "message": str(exc)}, status=409)
        except update_channel_mod.BadChannel:
            return web.json_response({"error": "bad_channel"}, status=400)
        return await _channel_status()

    async def handle_publish(request: web.Request) -> web.Response:
        auth_err = check_auth(request, master_only=True)
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
        probe.note_event(event, text)
        hub.publish_threadsafe(platform, chat_id, event, text)
        return web.json_response({"ok": True})

    async def handle_pair_claim(request: web.Request) -> web.Response:
        # No bearer here: the one-time code from `hermes keryx pair` is the
        # credential, and pairing.claim enforces single use, expiry and the
        # wrong-try limit. Without a master key there is nothing to pair into.
        no_store = {"Cache-Control": "no-store"}
        if not config.token:
            from .auth import resolve_token

            config.token, config.token_source = resolve_token()
        if not config.token:
            return web.json_response(
                {"error": {"message": "This Link has no key yet (API_SERVER_KEY), so it can't pair."}},
                status=503, headers=no_store)
        try:
            body = await request.json()
        except Exception:
            body = None
        if not isinstance(body, dict):
            return web.json_response(
                {"error": {"message": "JSON body required"}}, status=400, headers=no_store)
        result = await asyncio.to_thread(
            pairing.claim, body.get("code"), body.get("name"), request.remote or "")
        return web.json_response(result.body, status=result.status, headers=no_store)

    async def handle_tour_respond(request: web.Request) -> web.Response:
        # The phone's answer to a keryx_tour call (tour.py): what its tour engine did with
        # the action it saw on tool.start. Any paired phone, or the master key.
        auth_err = check_auth(request)
        if auth_err is not None:
            return auth_err
        # One read returns whatever chunk has arrived, so read up to the cap and one byte over.
        raw = bytearray()
        while len(raw) <= tour.ANSWER_MAX_BYTES:
            chunk = await request.content.read(tour.ANSWER_MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        if len(raw) > tour.ANSWER_MAX_BYTES:
            return web.json_response({"error": {"message": "answer too large"}}, status=413)
        try:
            body = json.loads(bytes(raw).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = None
        session_id = str((body or {}).get("session_id") or "").strip() if isinstance(body, dict) else ""
        result = body.get("result") if isinstance(body, dict) else None
        if not session_id or not isinstance(result, dict):
            return web.json_response(
                {"error": {"message": "session_id and a result object are required"}}, status=400)
        tour.answers.put(session_id, str(body.get("call_id") or "").strip(), result)
        return web.json_response({"ok": True})

    async def handle_tour_wait(request: web.Request) -> web.Response:
        # The keryx_tour handler's long poll, from whichever process runs the agent. Master
        # key only: a phone answers tours, it never reads another's answers.
        auth_err = check_auth(request, master_only=True)
        if auth_err is not None:
            return auth_err
        session_id = request.query.get("session_id", "").strip()
        if not session_id:
            return web.json_response({"error": {"message": "session_id is required"}}, status=400)
        try:
            timeout = int(request.query.get("timeout", tour.PROBE_TIMEOUT_S))
        except ValueError:
            timeout = tour.PROBE_TIMEOUT_S
        timeout = max(1, min(timeout, tour.WAIT_MAX_S))
        result = await tour.answers.wait(session_id, request.query.get("call_id", "").strip(), timeout)
        if result is None:
            return web.Response(status=204, headers={"Cache-Control": "no-store"})
        return web.json_response(result, headers={"Cache-Control": "no-store"})

    app = web.Application(middlewares=[make_scope_middleware()])
    app[_PANELS_KEY] = []
    app.router.add_get("/keryx/health", handle_health)
    app.router.add_get("/keryx/stream", handle_stream)
    app.router.add_post("/keryx/publish", handle_publish)
    app.router.add_post("/keryx/pair/claim", handle_pair_claim)
    app.router.add_post("/keryx/tour/respond", handle_tour_respond)
    app.router.add_get("/keryx/tour/wait", handle_tour_wait)
    app.router.add_get("/keryx/toolsets", handle_toolsets_get)
    app.router.add_put("/keryx/toolsets/{name}", handle_toolset_put)
    app.router.add_get("/keryx/update/channel", handle_update_channel_get)
    app.router.add_put("/keryx/update/channel", handle_update_channel_put)
    if config.panels:
        # The panels lean on Hermes internals; the stream must not go down with
        # them if a Hermes release moves something they import.
        try:
            from .panels import register_panel_routes
            app[_PANELS_KEY] = list(register_panel_routes(app.router, check_auth) or [])
        except Exception:
            logger.warning("keryx-stream: panel routes unavailable on this Hermes — "
                           "streaming and toolsets still served", exc_info=True)
    if config.upstream_url:
        from .proxy import make_proxy_handler
        relay, cleanup = make_proxy_handler(config.upstream_url, check_auth)
        app.router.add_route("*", "/{tail:.*}", relay)
        app.on_cleanup.append(cleanup)
    return app
