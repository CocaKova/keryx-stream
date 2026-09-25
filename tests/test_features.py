"""Feature derivation: /keryx/health lists only what the running Hermes provides
or what was actually fed — never a hard-coded list."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig
from keryx_stream.probe import Probe, config_hints, probe
from keryx_stream.server import build_app
from keryx_stream.version import CORE_FEATURES, REQUIRES_HERMES, features

AUTH = {"Authorization": "Bearer t"}


def _probe(cfg=None):
    return Probe(config_loader=lambda: cfg or {})


def test_nothing_registered_means_core_features_only():
    assert features(probe=_probe()) == list(CORE_FEATURES)


def test_reasoning_needs_the_opt_in_knob_and_the_delta_hook():
    p = _probe({"plugins": {"stream_reasoning_deltas": False}})
    p.registered("on_stream_delta")
    assert "reasoning" not in p.stream_features()
    p = _probe({"plugins": {"stream_reasoning_deltas": True}})
    assert "reasoning" not in p.stream_features()  # knob on, hook not registered
    p.registered("on_stream_delta")
    assert "reasoning" in p.stream_features()


def test_payload_derived_features_appear_once_seen():
    p = _probe()
    p.registered("pre_tool_call")
    p.registered("post_tool_call")
    assert "tools" in p.stream_features()
    assert "segment" not in p.stream_features()
    assert "tools.diff" not in p.stream_features()
    p.seen("iteration")          # stream payloads carry the per-call counter
    p.seen("tool_call_id")       # tool hooks carry the id the diff pairs on
    p.seen("edit_helpers")       # agent.display exposes the diff renderers
    assert {"segment", "tools.diff"} <= set(p.stream_features())


def test_turn_stop_and_thinking_follow_registration():
    p = _probe()
    assert not {"stop.turn", "thinking"} & set(p.stream_features())
    p.registered("post_llm_call")
    p.registered("middleware:llm_request")
    assert {"stop.turn", "thinking"} <= set(p.stream_features())


def test_status_usage_and_subagents_only_when_fed():
    p = _probe()
    for name in ("pre_auxiliary_call", "post_auxiliary_call", "subagent_start", "post_api_request"):
        p.registered(name)
    assert not {"status", "usage", "subagents"} & set(p.stream_features())
    p.note_event("status", json.dumps({"kind": "compacting"}))
    p.note_event("usage", json.dumps({"used": 1, "max": 2}))
    p.note_event("tool", json.dumps({"phase": "sub", "kind": "start"}))
    assert {"status", "usage", "subagents"} <= set(p.stream_features())


def test_features_merge_core_stream_panels_and_proxy():
    p = _probe()
    p.registered("post_llm_call")
    out = features(panel_features=["kanban", "kanban.review"], proxy=True, probe=p)
    assert out[: len(CORE_FEATURES)] == list(CORE_FEATURES)
    assert out[-1] == "proxy" and {"stop.turn", "kanban", "kanban.review"} <= set(out)


def test_hints_name_the_missing_knobs():
    keys = {h["key"] for h in config_hints({})}
    assert keys == {"plugins.stream_reasoning_deltas", "compression.progress_notices"}
    matrix = {"platforms": {"matrix": {"enabled": True}}}
    assert "display.platforms.matrix.streaming" in {h["key"] for h in config_hints(matrix)}
    done = {
        "plugins": {"stream_reasoning_deltas": True},
        "compression": {"progress_notices": True},
        "platforms": {"matrix": {"enabled": True}},
        "display": {"platforms": {"matrix": {"streaming": False}}},
    }
    assert config_hints(done) == []
    assert all(h["why"] for h in config_hints({}))


@pytest.mark.asyncio
async def test_health_reports_derived_features_hints_and_floor():
    probe.reset()
    app = build_app(PluginConfig(token="t", panels=False, upstream_url=""))
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/keryx/health")).json()
        assert body["requires_hermes"] == REQUIRES_HERMES
        assert body["features"] == list(CORE_FEATURES)
        assert isinstance(body["hints"], list)
        # Something feeds a status frame through the publish route (a forward-mode
        # process, or an operator's shim) — only now is status advertised.
        resp = await client.post("/keryx/publish", headers=AUTH, json={
            "platform": "cli", "chat_id": "s", "event": "status",
            "text": json.dumps({"kind": "compacting", "text": "📦"}),
        })
        assert resp.status == 200
        body = await (await client.get("/keryx/health")).json()
        assert "status" in body["features"]
    probe.reset()
