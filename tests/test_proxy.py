"""The relay: one URL for the app, without widening access to the native server."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig, _upstream_url
from keryx_stream.server import build_app

AUTH = {"Authorization": "Bearer plugin-token"}


async def _upstream():
    """A stand-in native API server that records what reached it."""
    seen = []

    async def health(request):
        return web.json_response({"status": "ok"})

    async def models(request):
        seen.append(request.headers.get("Authorization"))
        return web.json_response({"data": ["m"], "q": request.query.get("x")})

    async def events(request):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for i in range(3):
            await resp.write(f"data: {i}\n\n".encode())
        return resp

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_get("/v1/runs/r1/events", events)
    server = TestServer(app)
    await server.start_server()
    return server, seen


def _plugin(upstream_url, panels=False):
    return TestClient(TestServer(build_app(PluginConfig(
        token="plugin-token", upstream_url=upstream_url, panels=panels))))


@pytest.mark.asyncio
async def test_native_routes_answer_through_the_plugin_port(monkeypatch):
    monkeypatch.setenv("API_SERVER_KEY", "native-key")
    upstream, seen = await _upstream()
    try:
        async with _plugin(str(upstream.make_url("")).rstrip("/")) as client:
            resp = await client.get("/v1/models?x=1", headers=AUTH)
            assert resp.status == 200
            assert await resp.json() == {"data": ["m"], "q": "1"}
            # the native server is shown ITS key, not the plugin's
            assert seen == ["Bearer native-key"]
            assert (await client.get("/health")).status == 200  # Test link, no token
    finally:
        await upstream.close()


@pytest.mark.asyncio
async def test_relay_never_widens_access():
    upstream, seen = await _upstream()
    try:
        async with _plugin(str(upstream.make_url("")).rstrip("/")) as client:
            assert (await client.get("/v1/models")).status == 401
            assert (await client.get("/v1/models", headers={"Authorization": "Bearer nope"})).status == 401
            assert seen == []  # nothing unauthenticated reached the native server
    finally:
        await upstream.close()


@pytest.mark.asyncio
async def test_relay_streams_sse():
    upstream, _ = await _upstream()
    try:
        async with _plugin(str(upstream.make_url("")).rstrip("/")) as client:
            resp = await client.get("/v1/runs/r1/events", headers=AUTH)
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            assert await resp.text() == "data: 0\n\ndata: 1\n\ndata: 2\n\n"
    finally:
        await upstream.close()


@pytest.mark.asyncio
async def test_unreachable_upstream_says_so():
    async with _plugin("http://127.0.0.1:9") as client:
        resp = await client.get("/v1/models", headers=AUTH)
        assert resp.status == 502
        assert (await resp.json())["error"]["code"] == "upstream_unreachable"


@pytest.mark.asyncio
async def test_relay_off_leaves_unknown_paths_404():
    async with _plugin("") as client:
        assert (await client.get("/v1/models", headers=AUTH)).status == 404


@pytest.mark.asyncio
async def test_health_advertises_version_and_features():
    async with _plugin("http://127.0.0.1:9") as client:
        body = await (await client.get("/keryx/health")).json()
        assert body["version"].count(".") == 2
        assert {"stream", "stream.chat_key", "capabilities", "proxy"} <= set(body["features"])
    async with _plugin("") as client:
        assert "proxy" not in (await (await client.get("/keryx/health")).json())["features"]


def test_upstream_url_follows_the_api_server_port(monkeypatch):
    monkeypatch.setenv("API_SERVER_PORT", "9001")
    assert _upstream_url({}) == "http://127.0.0.1:9001"
    assert _upstream_url({"upstream_url": ""}) == ""
    assert _upstream_url({"upstream_url": False}) == ""
    assert _upstream_url({"upstream_url": "http://10.0.0.2:8642"}) == "http://10.0.0.2:8642"
