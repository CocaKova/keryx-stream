"""The llm_request middleware that maps Hermes' reasoning setting onto a local
brain's chat-template thinking switch."""
import copy

import pytest

from keryx_stream.thinking import make_llm_request_middleware, map_request


def _req(effort, **extra):
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    if effort is not None:
        base["reasoning_effort"] = effort
    base.update(extra)
    return base


def test_no_reasoning_field_means_no_rewrite():
    assert map_request(_req(None), "qwen3.8-flash-next") is None


def test_generic_local_brain_gets_enable_thinking():
    out = map_request(_req("medium"), "qwen3.8-flash-next")
    assert out["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert out["reasoning_effort"] == "medium"  # Hermes' own field left alone
    off = map_request(_req("none"), "qwen3.8-flash-next")
    assert off["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_existing_extra_body_and_template_kwargs_are_preserved():
    req = _req("low", extra_body={"top_k": 20, "chat_template_kwargs": {"foo": 1}})
    before = copy.deepcopy(req)
    out = map_request(req, "qwen3.8-flash-next")
    assert out["extra_body"] == {"top_k": 20, "chat_template_kwargs": {"foo": 1, "enable_thinking": True}}
    assert req == before  # pure: the input request is untouched


@pytest.mark.parametrize("effort, rung", [
    ("low", "low"), ("medium", "low"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max"),
])
def test_dsv41_is_clamped_onto_its_template_ladder(effort, rung):
    out = map_request(_req(effort), "deepseek-v4.1-flash")
    ctk = out["extra_body"]["chat_template_kwargs"]
    assert ctk == {"enable_thinking": True, "reasoning_effort": rung}
    assert out["reasoning_effort"] == rung  # never a rung the template 400s on


@pytest.mark.parametrize("effort, rung", [("low", "low"), ("medium", "low"), ("high", "high"), ("xhigh", "max")])
def test_glm53_ladder(effort, rung):
    out = map_request(_req(effort), "GLM-5.3-Flash")
    assert out["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == rung


def test_graded_family_thinking_off_sends_no_rung():
    out = map_request(_req("none"), "deepseek-v4.1")
    assert out["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert out["reasoning_effort"] == "none"


def test_mistral_native_never_gets_template_kwargs():
    req = _req("medium", extra_body={"chat_template_kwargs": {"enable_thinking": True}})
    out = map_request(req, "mistralai/Magistral-Small")
    assert "extra_body" not in out
    assert out["reasoning_effort"] == "high"
    assert map_request(_req("none"), "mistral-small")["reasoning_effort"] == "none"


def test_middleware_only_touches_local_providers():
    mw = make_llm_request_middleware()
    assert mw(request=_req("high"), provider="openrouter", model="x") is None
    out = mw(request=_req("high"), provider="custom", model="qwen3.8-flash-next")
    assert out["request"]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert out["source"] == "keryx-stream"
    assert mw(request=_req("high"), provider="custom:silas-brain", model="q")["request"]


def test_kill_switches(monkeypatch):
    assert make_llm_request_middleware(False)(request=_req("high"), provider="custom", model="q") is None
    monkeypatch.setenv("KERYX_THINKING_KWARGS", "off")
    assert make_llm_request_middleware()(request=_req("high"), provider="custom", model="q") is None


def test_middleware_through_hermes_request_chain(monkeypatch):
    """Run the callback through Hermes' own request-middleware chain
    (hermes_cli/middleware.py apply_llm_request_middleware), with only the
    registry lookups stubbed so no plugin discovery runs in the test."""
    mw_mod = pytest.importorskip("hermes_cli.middleware")
    plugins = pytest.importorskip("hermes_cli.plugins")
    cb = make_llm_request_middleware()
    monkeypatch.setattr(plugins, "has_middleware", lambda kind: kind == "llm_request")
    monkeypatch.setattr(plugins, "invoke_middleware",
                        lambda kind, **kw: [r for r in [cb(**kw)] if r is not None])
    result = mw_mod.apply_llm_request_middleware(
        _req("medium"), provider="custom", model="deepseek-v4.1", session_id="s",
    )
    assert result.changed
    assert result.payload["extra_body"]["chat_template_kwargs"]["reasoning_effort"] == "low"
    assert result.payload["reasoning_effort"] == "low"
    assert result.trace == [{"source": "keryx-stream", "reason": "thinking chat_template_kwargs"}]
