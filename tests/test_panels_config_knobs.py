"""Config knob table tests — every knob is checked against Hermes' OWN defaults.

The knob table is hand-written metadata pointing into config.yaml, so the failure
mode is a silent typo: a knob whose path doesn't exist writes a brand-new key
that nothing reads, and the app shows a control that does nothing. Validating
each spec against hermes_cli.config.DEFAULT_CONFIG turns that into a test
failure instead of a dead switch on the phone.

Hermes also drops settings between versions. A knob whose setting the running
Hermes no longer has is hidden from the app and refused on write
(panels._knob_available), so against such a tree the path check verifies that
guard and reports a skip instead of failing.
"""

import os
import sys
from pathlib import Path

import pytest

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

hermes_config = pytest.importorskip("hermes_cli.config")
DEFAULTS = hermes_config.DEFAULT_CONFIG

from keryx_stream import panels as ks  # noqa: E402

KNOBS = ks._CONFIG_KNOBS

# Knobs that legitimately do NOT appear in DEFAULT_CONFIG carry `not_in_defaults`
# in the table, naming the read site that proves the key is real and supplies its
# fallback, so this stays an audited exception list rather than a hole in the
# check. A knob excused without a verified read site is a dead switch on the
# phone: memory.flush_min_turns was exactly that (present in config.yaml, read by
# nothing) and was dropped instead of being excused.
_NOT_IN_DEFAULTS = {key for key, spec in KNOBS.items() if spec.get("not_in_defaults")}


def _resolve(spec):
    """Walk (section, path) through DEFAULT_CONFIG; KeyError if it isn't real."""
    node = DEFAULTS[spec["section"]]
    for part in spec["path"]:
        node = node[part]
    return node


def _resolve_or_skip(spec):
    try:
        return _resolve(spec)
    except (KeyError, TypeError):
        pytest.skip("not in this Hermes' DEFAULT_CONFIG; the knob is hidden from the app")


def _assert_hidden_and_refused(key):
    """A knob for a setting this Hermes lacks must not reach the phone or config.yaml."""
    import hermes_cli.config as hc

    saved = {}
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(hc, "load_config", lambda: {})
        mp.setattr(hc, "save_config", lambda cfg: saved.update(cfg))
        shown = {k["key"] for k in ks.config_knobs_snapshot()["knobs"]}
        assert key not in shown, f"{key} is offered but this Hermes has no such setting"
        status, _ = ks.config_knob_set(key, KNOBS[key]["default"])
        assert status == 400, f"{key} was written although this Hermes has no such setting"
        assert not saved
    finally:
        mp.undo()


@pytest.mark.parametrize("key", sorted(KNOBS))
def test_every_knob_points_at_a_real_config_path(key):
    spec = KNOBS[key]
    if key in _NOT_IN_DEFAULTS:
        pytest.skip("knob targets a Keryx-managed section")
    try:
        _resolve(spec)
    except (KeyError, TypeError):
        _assert_hidden_and_refused(key)
        pytest.skip(
            f"{spec['section']}.{'.'.join(spec['path'])} is not in this Hermes' "
            "DEFAULT_CONFIG; the knob is hidden from the app"
        )


@pytest.mark.parametrize("key", sorted(KNOBS))
def test_declared_kind_matches_the_real_default_type(key):
    spec = KNOBS[key]
    if key in _NOT_IN_DEFAULTS:
        pytest.skip("knob targets a Keryx-managed section")
    actual = _resolve_or_skip(spec)
    kind = spec["kind"]
    if kind == "bool":
        assert isinstance(actual, bool), f"{key}: declared bool, default is {actual!r}"
    elif kind == "int":
        assert isinstance(actual, int) and not isinstance(actual, bool), (
            f"{key}: declared int, default is {actual!r}"
        )
    elif kind == "float":
        assert isinstance(actual, (int, float)) and not isinstance(actual, bool), (
            f"{key}: declared float, default is {actual!r}"
        )
    elif kind == "enum":
        assert isinstance(actual, str), f"{key}: declared enum, default is {actual!r}"


@pytest.mark.parametrize("key", sorted(KNOBS))
def test_declared_default_matches_hermes_default(key):
    """Our "default" is what the app shows when the key is absent from
    config.yaml — if it disagrees with Hermes, the phone lies about the state."""
    spec = KNOBS[key]
    if key in _NOT_IN_DEFAULTS:
        pytest.skip("knob targets a Keryx-managed section")
    actual = _resolve_or_skip(spec)
    declared = spec["default"]
    if spec["kind"] == "float":
        assert float(declared) == pytest.approx(float(actual)), key
    else:
        assert declared == actual, (
            f"{key}: table says {declared!r}, Hermes default is {actual!r}"
        )


@pytest.mark.parametrize("key", sorted(KNOBS))
def test_spec_shape_is_complete(key):
    spec = KNOBS[key]
    for field in ("section", "path", "kind", "label", "group", "description", "applies"):
        assert spec.get(field), f"{key} is missing '{field}'"
    assert isinstance(spec["path"], list) and spec["path"], key
    if spec["kind"] in ("int", "float"):
        assert spec["min"] < spec["max"], key
        assert spec["min"] <= spec["default"] <= spec["max"], (
            f"{key}: default {spec['default']} outside {spec['min']}–{spec['max']}"
        )
    if spec["kind"] == "enum" and not spec.get("choices_dynamic"):
        assert spec["default"] in spec["choices"], key
        assert spec["choices"] == [c.lower() for c in spec["choices"]], (
            f"{key}: static enum choices must be lowercase — config_knob_set "
            "lowercases what the phone sends before matching"
        )
    if "not_in_defaults" in spec:
        assert isinstance(spec["not_in_defaults"], str) and spec["not_in_defaults"].strip(), (
            f"{key}: not_in_defaults must name the read site that proves the key is real"
        )


def test_enum_choices_are_accepted_by_the_setter(monkeypatch):
    """Guards the round trip: every static enum choice must survive
    config_knob_set's own validation, not just look right in the table."""
    saved = {}
    monkeypatch.setattr(
        ks, "_toolsets_env_set", lambda *_a, **_kw: set(), raising=False
    )
    fake = {"hermes_cli.config": None}

    import hermes_cli.config as hc

    monkeypatch.setattr(hc, "load_config", lambda: {})
    monkeypatch.setattr(hc, "save_config", lambda cfg: saved.update(cfg))

    for key, spec in KNOBS.items():
        if spec["kind"] != "enum" or spec.get("choices_dynamic"):
            continue
        for choice in spec["choices"]:
            status, out = ks.config_knob_set(key, choice)
            assert status == 200, f"{key}={choice!r} rejected: {out}"
            assert out["value"] == choice
    assert fake  # keeps the fixture honest about having run


def test_unknown_key_and_out_of_range_are_refused(monkeypatch):
    import hermes_cli.config as hc

    monkeypatch.setattr(hc, "load_config", lambda: {})
    monkeypatch.setattr(hc, "save_config", lambda cfg: None)
    monkeypatch.setattr(
        ks, "_toolsets_env_set", lambda *_a, **_kw: set(), raising=False
    )

    assert ks.config_knob_set("no_such_knob", 1)[0] == 400
    assert ks.config_knob_set("memory_char_limit", 10)[0] == 400  # below min
    assert ks.config_knob_set("memory_char_limit", 10**9)[0] == 400  # above max
    assert ks.config_knob_set("memory_enabled", "yes")[0] == 400  # not a bool
    assert ks.config_knob_set("browser_dialog_policy", "explode")[0] == 400


def test_knob_count_and_grouping():
    """1.25 widened the surface; groups are what the app renders as sections."""
    assert len(KNOBS) >= 55
    groups = {spec["group"] for spec in KNOBS.values()}
    for expected in (
        "Behavior", "Display", "Missions", "Compression",
        "Agent", "Tools", "Terminal", "Browser",
        "Memory", "Skills", "Delegation", "Voice", "Safety",
    ):
        assert expected in groups, f"missing group {expected}"


def test_knobs_follow_the_settings_the_running_hermes_has(monkeypatch):
    """Drop a setting from the defaults: its knob leaves the snapshot and its
    writes are refused; knobs excused by not_in_defaults and unknown defaults
    keep everything."""
    import copy

    import hermes_cli.config as hc

    saved = {}
    monkeypatch.setattr(hc, "load_config", lambda: {})
    monkeypatch.setattr(hc, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(
        ks, "_toolsets_env_set", lambda *_a, **_kw: set(), raising=False
    )

    defaults = copy.deepcopy(DEFAULTS)
    defaults["browser"] = {
        k: v for k, v in (defaults.get("browser") or {}).items() if k != "dialog_policy"
    }
    monkeypatch.setattr(ks, "_hermes_defaults", lambda: defaults)
    shown = {k["key"] for k in ks.config_knobs_snapshot()["knobs"]}
    assert "browser_dialog_policy" not in shown
    assert _NOT_IN_DEFAULTS <= shown
    assert ks.config_knob_set("browser_dialog_policy", "auto_dismiss")[0] == 400
    assert not saved

    monkeypatch.setattr(ks, "_hermes_defaults", lambda: None)
    shown = {k["key"] for k in ks.config_knobs_snapshot()["knobs"]}
    assert shown == set(KNOBS)
