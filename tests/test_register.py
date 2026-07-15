"""register(ctx) wiring, config loading, and hook → hub mirroring."""
import keryx_stream
from keryx_stream import PluginConfig, _make_hook_callbacks, load_config, register
from keryx_stream.hub import hub


def test_hook_callbacks_publish_expected_events(monkeypatch):
    published = []
    monkeypatch.setattr(
        hub, "publish_threadsafe", lambda *a: published.append(a)
    )
    cbs = _make_hook_callbacks(PluginConfig(default_platform="matrix"))

    cbs["on_stream_delta"](chat_id="!r:s", metadata={"platform": "matrix"}, delta="hi")
    cbs["on_stream_segment"](chat_id="!r:s", metadata=None)
    cbs["on_stream_end"](chat_id="!r:s", metadata=None, reason="done")

    assert published == [
        ("matrix", "!r:s", "delta", "hi"),
        ("matrix", "!r:s", "segment", None),
        ("matrix", "!r:s", "stop", None),
    ]


def test_delta_callback_ignores_empty_or_missing(monkeypatch):
    published = []
    monkeypatch.setattr(hub, "publish_threadsafe", lambda *a: published.append(a))
    cbs = _make_hook_callbacks(PluginConfig())
    cbs["on_stream_delta"](chat_id="!r:s", delta="")   # empty delta
    cbs["on_stream_delta"](chat_id=None, delta="hi")   # no chat
    assert published == []


def test_platform_falls_back_to_config_default_when_metadata_absent(monkeypatch):
    published = []
    monkeypatch.setattr(hub, "publish_threadsafe", lambda *a: published.append(a))
    cbs = _make_hook_callbacks(PluginConfig(default_platform="telegram"))
    cbs["on_stream_delta"](chat_id="123", metadata={}, delta="yo")
    assert published == [("telegram", "123", "delta", "yo")]


def test_load_config_reads_block_and_env(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "keryx_stream": {
                "enabled": True,
                "port": 9000,
                "default_platform": "matrix",
                "toolsets": {"locked": ["terminal"], "forbidden": ["web"]},
            }
        },
        raising=False,
    )
    monkeypatch.setenv("KERYX_STREAM_TOKEN", "sekret")
    cfg = load_config()
    assert cfg.port == 9000
    assert cfg.token == "sekret"
    assert cfg.toolsets_locked == ["terminal"]
    assert cfg.toolsets_forbidden == ["web"]


def test_register_wires_all_three_hooks_without_starting_server(monkeypatch):
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=True, token="t"))
    started = []
    monkeypatch.setattr(keryx_stream, "_serve_forever", lambda cfg: started.append(cfg))

    registered = []

    class FakeCtx:
        def register_hook(self, name, cb):
            registered.append(name)

    register(FakeCtx())
    assert sorted(registered) == ["on_stream_delta", "on_stream_end", "on_stream_segment"]


def test_register_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(keryx_stream, "load_config",
                        lambda: PluginConfig(enabled=False))
    called = []

    class FakeCtx:
        def register_hook(self, name, cb):
            called.append(name)

    register(FakeCtx())
    assert called == []
