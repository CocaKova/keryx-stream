"""Toolset toggle: platform validation + lock/forbidden guards.

The heavy read/write helpers are salvaged from the proven in-tree version and
delegate to hermes_cli; here we pin the plugin-owned logic — platform key
validation and the config-driven guard rails — with hermes_cli stubbed.
"""
import sys
import types

import pytest

from keryx_stream import toolsets


def test_platform_key_default_and_validation():
    assert toolsets.platform_key("", "matrix") == "matrix"
    assert toolsets.platform_key("Telegram", "matrix") == "telegram"
    with pytest.raises(ValueError):
        toolsets.platform_key("bad-platform!", "matrix")


@pytest.fixture
def stub_hermes(monkeypatch):
    """Stub the hermes_cli surfaces toolsets.set_enabled imports lazily."""
    tools_config = types.ModuleType("hermes_cli.tools_config")
    tools_config._get_effective_configurable_toolsets = lambda: [
        ("web", "Web", "web tools"),
        ("terminal", "Terminal", "shell"),
    ]
    tools_config._get_platform_tools = lambda cfg, plat, include_default_mcp_servers=False: []
    tools_config._save_platform_tools = lambda cfg, plat, tools: cfg.setdefault(
        "platform_toolsets", {}
    ).__setitem__(plat, sorted(tools))
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {}
    config.save_config = lambda cfg: None
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", tools_config)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    return tools_config


def test_set_enabled_rejects_unknown_toolset(stub_hermes):
    status, payload = toolsets.set_enabled("nope", True, "matrix", [], [])
    assert status == 400


def test_set_enabled_refuses_disabling_locked(stub_hermes):
    status, payload = toolsets.set_enabled("terminal", False, "matrix", ["terminal"], [])
    assert status == 403


def test_set_enabled_refuses_enabling_forbidden(stub_hermes):
    status, payload = toolsets.set_enabled("web", True, "matrix", [], ["web"])
    assert status == 403


def test_set_enabled_persists_allowed_toggle(stub_hermes):
    status, payload = toolsets.set_enabled("web", True, "matrix", [], [])
    assert status == 200
    assert payload == {"ok": True, "name": "web", "enabled": True, "platform": "matrix"}
