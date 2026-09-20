"""SSE server: auth, validation, and end-to-end delta delivery."""
import asyncio
import json

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


@pytest.mark.asyncio
async def test_publish_requires_auth():
    async with _client() as client:
        resp = await client.post("/keryx/publish", json={})
        assert resp.status == 401


@pytest.mark.asyncio
async def test_publish_validates_body():
    async with _client() as client:
        # missing chat_id
        resp = await client.post("/keryx/publish", headers=AUTH,
                                 json={"platform": "cli", "event": "delta", "text": "x"})
        assert resp.status == 400
        # non-string text
        resp = await client.post("/keryx/publish", headers=AUTH,
                                 json={"platform": "cli", "chat_id": "s", "event": "delta", "text": 5})
        assert resp.status == 400


@pytest.mark.asyncio
async def test_publish_feeds_a_subscriber():
    async with _client() as client:
        resp = await client.get(
            "/keryx/stream?platform=cli&chat_id=sess9", headers=AUTH
        )
        assert resp.status == 200
        # A foreign process (forward mode) POSTs its hook events here:
        resp_pub = await client.post("/keryx/publish", headers=AUTH, json={
            "platform": "cli", "chat_id": "sess9", "event": "delta", "text": "forw",
        })
        assert resp_pub.status == 200
        hub.publish_threadsafe("cli", "sess9", "stop", None)

        body = await asyncio.wait_for(resp.read(), timeout=5.0)
        text = body.decode()
        assert "event: delta" in text and '"text": "forw"' in text
        assert "event: stop" in text


@pytest.mark.asyncio
async def test_publish_tool_frame_json_reaches_subscriber():
    async with _client() as client:
        resp = await client.get("/keryx/stream?platform=cli&chat_id=s1", headers=AUTH)
        assert resp.status == 200
        await client.post("/keryx/publish", headers=AUTH, json={
            "platform": "cli", "chat_id": "s1", "event": "tool",
            "text": json.dumps({"phase": "start", "name": "terminal", "preview": "echo"}),
        })
        hub.publish_threadsafe("cli", "s1", "stop", None)
        body = await asyncio.wait_for(resp.read(), timeout=5.0)
        # The tool payload rides the SSE envelope as {"text": "<json>"} — the
        # app JSON-parses text for tool events, same shape as the gateway patch.
        data_line = next(line for line in body.decode().splitlines()
                         if line.startswith("data:") and "phase" in line)
        frame = json.loads(json.loads(data_line[len("data: "):])["text"])
        assert frame == {"phase": "start", "name": "terminal", "preview": "echo"}


@pytest.mark.asyncio
async def test_requests_run_inside_the_profile_secret_scope(monkeypatch):
    """A multiplexed gateway refuses credential reads outside a profile scope;
    the toolsets probe does such reads, so every request must enter one."""
    import sys
    import types

    events = []
    fake = types.ModuleType("agent.secret_scope")
    fake.is_multiplex_active = lambda: True
    fake.build_profile_secret_scope = lambda home: {"K": "v"}
    fake.set_secret_scope = lambda secrets: events.append(("set", dict(secrets))) or "tok"
    fake.reset_secret_scope = lambda token: events.append(("reset", token))
    agent_pkg = types.ModuleType("agent")
    agent_pkg.secret_scope = fake
    constants = types.ModuleType("hermes_constants")
    constants.get_hermes_home = lambda: "/h"
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake)
    monkeypatch.setitem(sys.modules, "hermes_constants", constants)

    async with _client() as client:
        assert (await client.get("/keryx/health")).status == 200
    assert events == [("set", {"K": "v"}), ("reset", "tok")]
