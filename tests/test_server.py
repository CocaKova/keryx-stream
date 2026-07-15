"""SSE server: auth, validation, and end-to-end delta delivery."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig
from keryx_stream.hub import hub
from keryx_stream.server import build_app

AUTH = {"Authorization": "Bearer t"}


def _client():
    app = build_app(PluginConfig(token="t", default_platform="matrix"))
    return TestClient(TestServer(app))


@pytest.mark.asyncio
async def test_health_is_open():
    async with _client() as client:
        resp = await client.get("/keryx/health")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True


@pytest.mark.asyncio
async def test_stream_requires_auth():
    async with _client() as client:
        resp = await client.get("/keryx/stream?chat_id=x")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_toolsets_requires_auth():
    async with _client() as client:
        resp = await client.get("/keryx/toolsets")
        assert resp.status == 401


@pytest.mark.asyncio
async def test_stream_requires_chat_id():
    async with _client() as client:
        resp = await client.get("/keryx/stream", headers=AUTH)
        assert resp.status == 400


@pytest.mark.asyncio
async def test_stream_delivers_deltas_then_closes_on_stop():
    async with _client() as client:
        resp = await client.get(
            "/keryx/stream?platform=matrix&chat_id=!room", headers=AUTH
        )
        assert resp.status == 200
        # Subscribed now (handler yielded at queue.get). Drive the stream.
        hub.publish_threadsafe("matrix", "!room", "delta", "hel")
        hub.publish_threadsafe("matrix", "!room", "delta", "lo")
        hub.publish_threadsafe("matrix", "!room", "stop", None)

        body = await asyncio.wait_for(resp.read(), timeout=5.0)
        text = body.decode()
        assert "event: delta" in text
        assert '"text": "hello"' in text  # coalesced into one frame
        assert "event: stop" in text
