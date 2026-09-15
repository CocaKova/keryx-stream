"""register(ctx) wiring, config loading, and hook → publish mirroring."""
import keryx_stream
from keryx_stream import PluginConfig, _make_hook_callbacks, load_config, register


def test_stream_callbacks_publish_expected_events(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="matrix"), lambda *a: published.append(a))

    cbs["on_stream_start"](session_id="s1", surface="cli")
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="hi")
    cbs["on_stream_delta"](session_id="s1", surface="cli", delta="hm", kind="reasoning")
    cbs["on_interim_message"](session_id="s1", surface="cli", text="part done", already_streamed=False)
    cbs["on_stream_end"](session_id="s1", surface="cli", final_text="done")

    assert published == [
        ("cli", "s1", "start", None),
        ("cli", "s1", "delta", "hi"),
        ("cli", "s1", "reasoning", "hm"),
        ("cli", "s1", "interim", "part done"),
        ("cli", "s1", "stop", "done"),
    ]


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
    assert frame == {"phase": "start", "name": "terminal",
                     "preview": str({"command": "echo probe-ok"})}
    platform, chat_id, event, frame = published[1]
    assert (platform, chat_id, event) == ("cli", "s1", "tool")
    assert frame == {"phase": "end", "name": "terminal", "ok": True, "ms": 63,
                     "result": '{"output": "probe-ok"}', "result_len": 22}


def test_post_tool_error_carries_error_and_ok_false(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    cbs["post_tool_call"](session_id="s1", surface="cli", tool_name="terminal",
                          result="boom", status="error", duration_ms=5,
                          error_message="exit code 1")
    _, _, _, frame = published[0]
    assert frame["ok"] is False
    assert frame["error"] == "exit code 1"


def test_delta_callback_ignores_empty_or_missing(monkeypatch):
    published = []
    cbs = _make_hook_callbacks(PluginConfig(), lambda *a: published.append(a))
    cbs["on_stream_delta"](session_id="s1", delta="")     # empty delta
    cbs["on_stream_delta"](delta="hi")                    # no session id
    cbs["on_stream_end"](session_id="s1", final_text=None)  # surface falls back to default
    assert published == [(PluginConfig().default_platform, "s1", "stop", None)]


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
    big = "x" * 5000
    cbs["post_tool_call"](session_id="s1", tool_name="web_extract", result=big, status="ok")
    _, _, _, frame = published[0]
    assert len(frame["result"]) < 3000
    assert frame["result_len"] == 5000
    assert "truncated" in frame["result"]


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
    assert sorted(registered) == [
        "on_interim_message", "on_stream_delta", "on_stream_end", "on_stream_start",
        "post_tool_call", "pre_tool_call",
    ]


def test_register_forwards_when_port_taken(monkeypatch):
    """Bind failure → forward mode: no server thread, publish goes to Forwarder."""
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=True, token="t"))
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: None)
    created = []

    class FakeForwarder:
        def __init__(self, url, token):
            created.append((url, token))
            self.publish = lambda *a: None

    import keryx_stream.forwarder as fwd
    monkeypatch.setattr(fwd, "Forwarder", FakeForwarder)

    registered = []

    class FakeCtx:
        def register_hook(self, name, cb):
            registered.append(name)

    register(FakeCtx())
    assert len(registered) == 6
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
