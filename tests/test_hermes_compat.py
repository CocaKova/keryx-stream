"""Hermes refuses to load a plugin that imports a path it has retired
(`hermes plugins compat`). Run its own scanner over this package so a Hermes
refactor shows up here — nightly, against hermes-agent main — before it shows
up as a plugin that silently stopped loading on someone's gateway."""
import os
import sys
from pathlib import Path

import pytest

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

plugin_compat = pytest.importorskip("hermes_cli.plugin_compat")

PACKAGE = Path(__file__).resolve().parent.parent / "keryx_stream"


def test_no_retired_hermes_imports():
    hits = plugin_compat.scan_plugin(PACKAGE)
    assert not hits, "imports Hermes has retired: " + ", ".join(str(h) for h in hits)


def test_the_hooks_we_register_still_exist():
    plugins = pytest.importorskip("hermes_cli.plugins")
    from keryx_stream import PluginConfig, _make_hook_callbacks

    ours = set(_make_hook_callbacks(PluginConfig(), lambda *a: None))
    assert ours <= set(plugins.VALID_HOOKS), f"unknown to Hermes: {sorted(ours - set(plugins.VALID_HOOKS))}"
