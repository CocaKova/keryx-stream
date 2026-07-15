"""Toolset read + toggle for the Keryx app.

Core ships a read-only ``GET /v1/toolsets`` (api_server platform). Keryx needs
(a) the view for the platform its runs actually execute on and (b) a toggle, so
this exposes ``GET/PUT /keryx/toolsets`` over the same internal helpers core
uses. It writes ``platform_toolsets.<platform>`` through
``hermes_cli.tools_config`` — the exact path the desktop picker uses.

``locked`` / ``forbidden`` guard lists come from the plugin's ``config.yaml``
block (``keryx_stream.toolsets.locked`` / ``.forbidden``) — NOT env vars — so
this stays within the "non-secret config lives in config.yaml" rule.
"""
from __future__ import annotations

import re
from typing import List, Tuple

_PLATFORM_KEY_OK = re.compile(r"^[a-z0-9_]+$")


def platform_key(raw: str, default: str) -> str:
    platform = (raw or "").strip().lower() or default
    if not _PLATFORM_KEY_OK.match(platform):
        raise ValueError(f"invalid platform '{raw}'")
    return platform


def snapshot(platform: str, locked: List[str], forbidden: List[str]) -> dict:
    """Payload for ``GET /keryx/toolsets`` — same entry shape as ``/v1/toolsets``
    plus ``locked``, keyed to the requested platform's enablement."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _toolset_has_keys,
    )
    from toolsets import resolve_toolset

    locked_set = set(locked)
    forbidden_set = set(forbidden)
    config = load_config()
    enabled = set(
        _get_platform_tools(config, platform, include_default_mcp_servers=False)
    )
    data = []
    for name, label, desc in _get_effective_configurable_toolsets():
        try:
            tools = sorted(set(resolve_toolset(name)))
        except Exception:
            tools = []
        data.append(
            {
                "name": name,
                "label": label,
                "description": desc,
                "enabled": name in enabled,
                "configured": _toolset_has_keys(name, config),
                "locked": name in locked_set or name in forbidden_set,
                "tools": tools,
            }
        )
    return {"platform": platform, "canToggle": True, "data": data}


def set_enabled(
    name: str,
    enabled: bool,
    platform: str,
    locked: List[str],
    forbidden: List[str],
) -> Tuple[int, dict]:
    """``PUT /keryx/toolsets/{name}`` — persist one toolset's enablement for a
    platform. Refuses locked/forbidden changes so the app never makes an edit a
    guard would revert behind the user's back."""
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _save_platform_tools,
    )

    valid = {key for key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        return 400, {"error": {"message": f"unknown toolset '{name}'"}}
    if not enabled and name in set(locked):
        return 403, {"error": {"message": f"'{name}' is locked on and cannot be disabled here"}}
    if enabled and name in set(forbidden):
        return 403, {"error": {"message": f"'{name}' is locked off and cannot be enabled here"}}

    config = load_config()
    current = set(
        _get_platform_tools(config, platform, include_default_mcp_servers=False)
    )
    if enabled:
        current.add(name)
    else:
        current.discard(name)

    # _save_platform_tools drops the `no_mcp` sentinel by design (the desktop
    # picker treats saving as consent to re-enable MCP servers). A single-toolset
    # phone toggle is no such consent — losing the sentinel would resurrect every
    # default MCP server on this platform. Put it back.
    raw_before = config.get("platform_toolsets", {}).get(platform) or []
    had_no_mcp = "no_mcp" in raw_before
    _save_platform_tools(config, platform, current)
    if had_no_mcp and "no_mcp" not in config["platform_toolsets"][platform]:
        from hermes_cli.config import save_config

        config["platform_toolsets"][platform] = sorted(
            set(config["platform_toolsets"][platform]) | {"no_mcp"}
        )
        save_config(config)
    return 200, {"ok": True, "name": name, "enabled": enabled, "platform": platform}
