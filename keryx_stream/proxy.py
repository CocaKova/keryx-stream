"""One URL for the app: everything that isn't ``/keryx/*`` is relayed to the
gateway's native API server.

The Keryx app is configured with a single gateway URL and sends it both the
plugin's ``/keryx/*`` routes and stock ones (``/health``, ``/v1/runs``,
``/v1/models``, ``/api/jobs`` …). A plugin can't add routes to the native
server, so instead the plugin's port fronts it: point the app at this port and
both halves answer.

The relay never widens access. Every relayed request except the bare
``/health`` liveness probe must carry the plugin's bearer token first; only
then is it forwarded, presenting the native server's own key
(``API_SERVER_KEY``) so a separate ``KERYX_STREAM_TOKEN`` still works. A native
server bound to loopback with no key therefore stays unreachable to anyone who
doesn't hold the plugin token.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("keryx_stream.proxy")

# Hop-by-hop headers (RFC 9110 §7.6.1) plus the ones the relay sets itself.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
_OPEN_PATHS = {"/health"}
_CHUNK = 16 * 1024


def make_proxy_handler(upstream_url: str, check_auth):
    """aiohttp catch-all handler relaying to [upstream_url]; owns one client
    session, closed by the returned ``cleanup`` coroutine."""
    import aiohttp
    from aiohttp import web

    base = upstream_url.rstrip("/")
    state: dict = {"session": None}

    def _session() -> aiohttp.ClientSession:
        if state["session"] is None or state["session"].closed:
            # No total timeout: /v1/runs/{id}/events is an SSE stream that
            # legitimately stays open for the length of a run.
            state["session"] = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
                auto_decompress=False,
            )
        return state["session"]

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path not in _OPEN_PATHS:
            auth_err = check_auth(request)
            if auth_err is not None:
                return auth_err
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        upstream_key = os.environ.get("API_SERVER_KEY", "").strip()
        if upstream_key and "Authorization" in request.headers:
            headers["Authorization"] = f"Bearer {upstream_key}"
        body = await request.read() if request.can_read_body else None
        try:
            async with _session().request(
                request.method, base + request.rel_url.raw_path_qs,
                headers=headers, data=body, allow_redirects=False,
            ) as upstream:
                resp = web.StreamResponse(status=upstream.status, headers={
                    k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
                })
                await resp.prepare(request)
                async for chunk in upstream.content.iter_chunked(_CHUNK):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except aiohttp.ClientConnectorError:
            return web.json_response({"error": {
                "message": f"keryx-stream: the Hermes API server is not reachable at {base}. "
                           "Enable it (API_SERVER_ENABLED=true) or set keryx_stream.upstream_url.",
                "code": "upstream_unreachable",
            }}, status=502)
        except (ConnectionResetError, aiohttp.ClientError) as exc:
            logger.debug("keryx-stream: relay to %s dropped: %s", base, exc)
            return web.json_response(
                {"error": {"message": "upstream connection dropped", "code": "upstream_dropped"}},
                status=502,
            )

    async def cleanup(_app) -> None:
        if state["session"] is not None and not state["session"].closed:
            await state["session"].close()

    return handler, cleanup
