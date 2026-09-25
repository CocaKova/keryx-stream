"""register(ctx) wiring, config loading, and hook → publish mirroring."""
import json

import keryx_stream
from keryx_stream import PluginConfig, _make_hook_callbacks, load_config, register

_ALL_HOOKS = [
    "on_interim_message", "on_stream_delta", "on_stream_end", "on_stream_start",
    "post_api_request", "post_auxiliary_call", "post_llm_call", "post_tool_call",
    "pre_auxiliary_call", "pre_gateway_dispatch", "pre_tool_call",
    "subagent_start", "subagent_stop",
]


def _cbs(publish, **cfg):
    """Hook callbacks with the stop finisher run inline (deterministic tests)."""
    return _make_hook_callbacks(PluginConfig(**cfg), publish, background=False)


def test_stream_callbacks_publish_expected_events(monkeypatch):
    published = []
    cbs = _cbs(lambda *a: published.append(a), default_platform="matrix", stop_quiet_ms=0)

    cbs["on_stream_start"](session_id="s1", surface="cli", turn_id="t1", iteration=1)
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="hi", turn_id="t1", iteration=1)
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="hm", kind="reasoning",
                           turn_id="t1", iteration=1)
    cbs["on_interim_message"](session_id="s1", surface="cli", text="part done",
                              already_streamed=False, turn_id="t1")
    cbs["on_stream_end"](session_id="s1", surface="cli", final_text="hi", finished=True, turn_id="t1")
    # on_stream_end is per API call — the turn is NOT over yet.
    assert [e for _, _, e, _ in published] == ["start", "delta", "reasoning", "interim"]

    cbs["post_llm_call"](session_id="s1", turn_id="t1", assistant_response="hi")
    assert published == [
        ("cli", "s1", "start", None),
        ("cli", "s1", "delta", "hi"),
        ("cli", "s1", "reasoning", "hm"),
        ("cli", "s1", "interim", "part done"),
        ("cli", "s1", "stop", "hi"),
    ]


def test_text_before_a_tool_call_is_a_segment_not_a_stop(monkeypatch):
    """A tool-calling API call can carry text ("Let me read that file.") — the
    0.3 heuristic ended the turn there. The boundary is now inferred from the
    iteration counter on the delta thread; the stop comes from post_llm_call."""
    published = []
    cbs = _cbs(lambda *a: published.append(a), default_platform="cli", stop_quiet_ms=0)

    cbs["on_stream_start"](session_id="s1", surface="cli", turn_id="t1", iteration=1)
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="Reading it.", turn_id="t1", iteration=1)
    cbs["on_stream_end"](session_id="s1", surface="cli", final_text="Reading it.", finished=True,
                         turn_id="t1")
    cbs["pre_tool_call"](session_id="s1", tool_name="read_file", args={"path": "a"}, turn_id="t1",
                         tool_call_id="c1")
    cbs["post_tool_call"](session_id="s1", tool_name="read_file", result="out", status="ok",
                          duration_ms=10, turn_id="t1", tool_call_id="c1")
    cbs["on_stream_start"](session_id="s1", surface="cli", turn_id="t1", iteration=2)
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="answer", turn_id="t1", iteration=2)
    cbs["on_stream_end"](session_id="s1", surface="cli", final_text="answer", finished=True, turn_id="t1")
    cbs["post_llm_call"](session_id="s1", turn_id="t1", assistant_response="answer")

    assert [(e, t if e != "tool" else json.loads(t)["phase"]) for _, _, e, t in published] == [
        ("start", None), ("delta", "Reading it."),
        ("tool", "start"), ("tool", "end"),
        ("start", None), ("segment", None), ("delta", "answer"),
        ("stop", "answer"),
    ]


def test_error_end_does_not_stop_the_turn_but_the_watchdog_does(monkeypatch):
    published = []
    cbs = _cbs(lambda *a: published.append(a), default_platform="cli", idle_stop_s=5)
    cbs["on_stream_start"](session_id="s1", surface="cli", turn_id="t1")
    cbs["on_stream_end"](session_id="s1", surface="cli", final_text="",
                         finished=False, error="HTTP 500", turn_id="t1")
    assert [e for _, _, e, _ in published] == ["start"]  # a retry may follow
    tracker = cbs.tracker
    assert tracker.sweep(now=tracker._clock() + 1) == 0      # not idle long enough
    assert tracker.sweep(now=tracker._clock() + 60) == 1     # interrupted / empty turn
    assert published[-1] == ("cli", "s1", "stop", None)


def test_tool_callbacks_publish_tool_frames(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="matrix"), lambda *a: published.append(a))

    cbs["pre_tool_call"](session_id="s1", surface="cli", tool_name="terminal",
                         args={"command": "echo probe-ok"})
    cbs["post_tool_call"](session_id="s1", surface="cli", tool_name="terminal",
                          result='{"output": "probe-ok"}', status="ok", duration_ms=63)

    assert len(published) == 2
    platform, chat_id, event, frame = published[0]
    assert (platform, chat_id, event) == ("cli", "s1", "tool")
    # Tool frames ride the wire as a JSON string (same shape as the gateway patch).
    assert json.loads(frame) == {"phase": "start", "name": "terminal",
                                 "preview": str({"command": "echo probe-ok"})}
    platform, chat_id, event, frame = published[1]
    assert (platform, chat_id, event) == ("cli", "s1", "tool")
    assert json.loads(frame) == {"phase": "end", "name": "terminal", "ok": True, "ms": 63,
                                 "result": '{"output": "probe-ok"}', "result_len": 22}


def test_post_tool_error_carries_error_and_ok_false(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    cbs["post_tool_call"](session_id="s1", surface="cli", tool_name="terminal",
                          result="boom", status="error", duration_ms=5,
                          error_message="exit code 1")
    frame = json.loads(published[0][3])
    assert frame["ok"] is False
    assert frame["error"] == "exit code 1"


def test_delta_callback_ignores_empty_or_missing(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    cbs["on_stream_delta"](session_id="s1", delta="")     # empty delta
    cbs["on_stream_delta"](delta="hi")                    # no session id
    cbs["on_stream_start"](session_id="s1")               # surface falls back to default
    assert published == [(PluginConfig().default_platform, "s1", "start", None)]


def test_surface_falls_back_to_config_default_when_absent(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="matrix"), lambda *a: published.append(a))
    cbs["on_stream_delta"](session_id="s1", surface=None, delta="yo")
    assert published == [("matrix", "s1", "delta", "yo")]


def test_interim_already_streamed_is_suppressed(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    cbs["on_interim_message"](session_id="s1", text="seen it", already_streamed=True)
    assert published == []


def test_tool_result_is_clipped_middle(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    big = "a" * 3000 + "b" * 2000
    cbs["post_tool_call"](session_id="s1", tool_name="web_extract", result=big, status="ok")
    frame = json.loads(published[0][3])
    assert len(frame["result"]) < 2500
    assert frame["result_len"] == 5000
    # Both ends survive (a transform_tool_result verdict lives in the tail) and
    # the elision says how much is missing — same shape as the in-tree patch.
    assert frame["result"].startswith("a") and frame["result"].endswith("b")
    assert "2,600 chars elided" in frame["result"]


def test_load_config_reads_block_and_env(monkeypatch):
    """config.yaml block + env token. A fake hermes_cli.config keeps the test
    standalone (CI has no hermes-agent installed; load_config degrades to {})."""
    import sys
    import types

    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {  # type: ignore[attr-defined]
        "keryx_stream": {
            "enabled": True,
            "port": 9000,
            "forward_url": "http://10.0.0.5:8646/keryx/publish",
            "default_platform": "matrix",
            "toolsets": {"locked": ["terminal"], "forbidden": ["web"]},
        }
    }
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)
    monkeypatch.setenv("KERYX_STREAM_TOKEN", "sekret")
    cfg = load_config()
    assert cfg.port == 9000
    assert cfg.token == "sekret"
    assert cfg.forward_url == "http://10.0.0.5:8646/keryx/publish"
    assert cfg.toolsets_locked == ["terminal"]
    assert cfg.toolsets_forbidden == ["web"]


def test_register_wires_all_hooks_and_starts_server(monkeypatch):
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=True, token="t"))
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: object())
    monkeypatch.setattr(keryx_stream, "_server_thread",
                        type("T", (), {"is_alive": lambda self: True})())

    registered = []

    class FakeCtx:
        def register_hook(self, name, cb):
            registered.append(name)

    register(FakeCtx())
    assert sorted(registered) == sorted(_ALL_HOOKS)


def test_register_forwards_when_port_taken(monkeypatch):
    """Bind failure → forward mode: no server thread, publish goes to Forwarder."""
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=True, token="t"))
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: None)
    created = []

    class FakeForwarder:
        def __init__(self, url, token, resolve=None):
            created.append((url, token))
            self.publish = lambda *a: None

    import keryx_stream.forwarder as fwd
    monkeypatch.setattr(fwd, "Forwarder", FakeForwarder)

    registered = []

    class FakeCtx:
        def register_hook(self, name, cb):
            registered.append(name)

    register(FakeCtx())
    assert len(registered) == len(_ALL_HOOKS)
    assert created and created[0][0].endswith("/keryx/publish")


def test_register_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=False))
    called = []

    class FakeCtx:
        def register_hook(self, name, cb):
            called.append(name)

    register(FakeCtx())
    assert called == []


class _Origin:
    def __init__(self, platform, chat_id):
        self.platform, self.chat_id = platform, chat_id


class _Entry:
    def __init__(self, origin):
        self.origin = origin


class _Store:
    def __init__(self, table):
        self.table, self.lookups = table, 0

    def lookup_by_session_id(self, session_id):
        self.lookups += 1
        return self.table.get(session_id)


def test_gateway_turn_also_publishes_under_its_chat_key():
    """The app on a chat transport subscribes by room id — it never learns the
    session id — so a gateway turn must reach that key too."""
    published = []
    cbs = _cbs(lambda *a: published.append(a), default_platform="matrix")
    store = _Store({"s1": _Entry(_Origin("matrix", "!room:hs"))})
    assert cbs["pre_gateway_dispatch"](event=object(), gateway=None, session_store=store) is None

    cbs["on_stream_start"](session_id="s1", surface="matrix")
    cbs["on_stream_delta"](session_id="s1", surface="matrix", delta="a")
    cbs["on_stream_delta"](session_id="s1", surface="matrix", delta="b")
    cbs["on_stream_end"](session_id="s1", surface="matrix", final_text="ab")
    cbs["post_llm_call"](session_id="s1", assistant_response="ab")

    assert [p for p in published if p[1] == "!room:hs"] == [
        ("matrix", "!room:hs", "start", None),
        ("matrix", "!room:hs", "delta", "a"),
        ("matrix", "!room:hs", "delta", "b"),
        ("matrix", "!room:hs", "stop", "ab"),
    ]
    assert ("matrix", "s1", "delta", "a") in published  # session key still served
    assert store.lookups == 1  # never on the token path


def test_no_store_or_unknown_session_publishes_session_key_only():
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="cli"), lambda *a: published.append(a))
    cbs["on_stream_start"](session_id="s9", surface="cli")
    cbs["pre_gateway_dispatch"](session_store=_Store({}))
    cbs["on_stream_delta"](session_id="s9", surface="cli", delta="x")
    assert published == [("cli", "s9", "start", None), ("cli", "s9", "delta", "x")]


def test_tool_frames_follow_the_sessions_surface():
    """The tool hooks carry session_id but no surface. Frames must land on the
    key the session's tokens use, not on the default platform's."""
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="matrix"), lambda *a: published.append(a))
    cbs["on_stream_start"](session_id="s1", surface="cli")
    cbs["pre_tool_call"](session_id="s1", tool_name="terminal", args={"command": "echo"})
    cbs["post_tool_call"](session_id="s1", tool_name="terminal", result="out", duration_ms=1)
    assert [(p, sid, ev) for p, sid, ev, _ in published] == [
        ("cli", "s1", "start"), ("cli", "s1", "tool"), ("cli", "s1", "tool"),
    ]
    # a session never seen streaming still falls back to the default platform
    cbs["pre_tool_call"](session_id="s2", tool_name="terminal", args={})
    assert published[-1][:2] == ("matrix", "s2")
