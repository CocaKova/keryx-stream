"""Marker prompt sections: what register() hands Hermes, where it renders, and
that the taught syntax is the syntax the app parses."""
import re

import keryx_stream
from keryx_stream import PluginConfig, load_config, register
from keryx_stream.markers import (
    CORE_ID,
    CORE_TEXT,
    HANDS_ID,
    HANDS_TEXT,
    SECTION_MAX_CHARS,
    normalize_platforms,
    register_sections,
    section_content,
)
from keryx_stream.probe import Probe

# Hermes' aggregate budget for every plugin's sections (plugins_dispatch.py);
# ours must leave room for other plugins, not just fit.
_HERMES_TOTAL_BUDGET = 8000


class _SectionCtx:
    def __init__(self):
        self.sections = {}

    def register_hook(self, name, cb):
        pass

    def register_system_prompt_section(self, id, content, *, position="after_memory", max_chars=4000):
        assert position == "after_memory" and max_chars <= 4000
        self.sections[id] = content


def test_sections_fit_hermes_budgets():
    assert len(CORE_TEXT) <= SECTION_MAX_CHARS and len(HANDS_TEXT) <= SECTION_MAX_CHARS
    assert len(CORE_TEXT) + len(HANDS_TEXT) < _HERMES_TOTAL_BUDGET * 0.6
    for sid in (CORE_ID, HANDS_ID):  # Hermes' id rule
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", sid)


def test_text_is_generic():
    for text in (CORE_TEXT, HANDS_TEXT):
        assert not re.search(r"\b(Jonny|Sy|SILAS)\b", text)
        assert "/home/" not in text


def test_taught_shapes_parse_like_the_app():
    """Examples in the prompt must match the app's regexes (MessageParser.kt) —
    a shape the parser rejects renders as literal text on the phone."""
    cite_source = re.compile(r"⟦cite\s*(\d+)\s*\|([^|]*)\|([^|]*)\|([^⟧]*)⟧")
    assert cite_source.search(CORE_TEXT)
    assert re.search(r"⟦c\d+⟧", CORE_TEXT)
    assert re.search(r"⟦keryx:ask\|[^⟧\n]*⟧", CORE_TEXT)
    assert re.search(r"⟦keryx:do\|[^⟧\n]*⟧", HANDS_TEXT)
    assert "MEDIA:/absolute/path" in CORE_TEXT
    # Every phone-action kind PhoneAction.Kind knows is taught.
    for kind in ("url", "dial", "sms", "email", "calendar", "alarm", "timer", "navigate",
                 "search", "play", "open", "copy", "torch", "share"):
        assert re.search(rf"(^|· ){kind}\|", HANDS_TEXT, re.MULTILINE), kind
    assert "⟦keryx:voice⟧" in HANDS_TEXT
    # The override of the api_server "plain text, no markdown" hint is explicit.
    assert "overrides" in CORE_TEXT and "markdown" in CORE_TEXT


def test_section_renders_only_on_configured_platforms():
    render = section_content("hello", frozenset({"api_server", "matrix"}))
    assert render({"platform": "api_server"}) == "hello"
    assert render({"platform": "MATRIX"}) == "hello"
    assert render({"platform": "tui"}) == ""
    assert render({"platform": "kanban"}) == ""
    assert render({}) == ""


def test_normalize_platforms():
    assert normalize_platforms(None) == frozenset({"api_server", "matrix"})
    assert normalize_platforms([]) == frozenset({"api_server", "matrix"})
    assert normalize_platforms("TUI") == frozenset({"tui"})
    assert normalize_platforms(["api_server", " tui "]) == frozenset({"api_server", "tui"})


def test_register_sections_on_old_hermes_is_a_noop():
    class OldCtx:
        pass

    assert register_sections(OldCtx(), frozenset({"matrix"})) == []


def test_register_sections_survives_a_refusal():
    class Refusing(_SectionCtx):
        def register_system_prompt_section(self, id, content, **kw):
            if id == CORE_ID:
                raise ValueError("already registered")
            super().register_system_prompt_section(id, content, **kw)

    ctx = Refusing()
    assert register_sections(ctx, frozenset({"matrix"})) == [HANDS_ID]


def _register_with(monkeypatch, config):
    monkeypatch.setattr(keryx_stream, "load_config", lambda: config)
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: object())
    monkeypatch.setattr(keryx_stream, "_server_thread",
                        type("T", (), {"is_alive": lambda self: True})())
    probe = Probe(config_loader=dict)
    monkeypatch.setattr(keryx_stream, "probe", probe)
    ctx = _SectionCtx()
    register(ctx)
    return ctx, probe


def test_register_wires_sections_and_reports_the_feature(monkeypatch):
    ctx, probe = _register_with(monkeypatch, PluginConfig(enabled=True, token="t"))
    assert set(ctx.sections) == {CORE_ID, HANDS_ID}
    assert ctx.sections[CORE_ID]({"platform": "api_server"}) == CORE_TEXT
    assert ctx.sections[HANDS_ID]({"platform": "tui"}) == ""
    assert "prompt.markers" in probe.stream_features()


def test_register_skips_sections_when_switched_off(monkeypatch):
    ctx, probe = _register_with(monkeypatch, PluginConfig(enabled=True, token="t", markers=False))
    assert ctx.sections == {}
    assert "prompt.markers" not in probe.stream_features()


def test_load_config_markers_block(monkeypatch):
    import sys
    import types

    def with_block(block):
        fake = types.ModuleType("hermes_cli.config")
        fake.load_config = lambda: {"keryx_stream": block}  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "hermes_cli.config", fake)
        return load_config()

    monkeypatch.setenv("KERYX_STREAM_TOKEN", "t")
    cfg = with_block({})
    assert cfg.markers is True and cfg.markers_platforms == frozenset({"api_server", "matrix"})
    cfg = with_block({"markers": {"platforms": ["api_server", "tui"]}})
    assert cfg.markers is True and cfg.markers_platforms == frozenset({"api_server", "tui"})
    assert with_block({"markers": False}).markers is False
    assert with_block({"markers": {"enabled": "off"}}).markers is False
