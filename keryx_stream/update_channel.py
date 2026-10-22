"""Update channel read + switch for the Keryx app (``GET/PUT /keryx/update/channel``).

Stock Hermes keeps an update channel per install (``update.installs.<id>`` in
config.yaml, ``hermes_cli/update_channel.py``) and the dashboard's update check
follows it, but nothing over HTTP can show or change it: Desktop switches it
locally with ``hermes update --set-channel``. This exposes the same switch to the
phone through Hermes' own functions, never a reimplementation of them:

* ``stable``: published releases. An install with NO saved channel whose default
  is stable rides it forward-only: a checkout newer than the latest release waits
  for the next one and is never moved back. Saving ``stable`` explicitly is
  Hermes' consent to move back to the latest release.
* ``main`` (the app calls it Edge): every change as it lands.

GET writes nothing Hermes reads: its release lookups go through
``check_for_updates`` with a cache file of this plugin's own, never Hermes'
``source-checks`` file. A Hermes without ``update_channel.py`` (older than the
channel records) makes :func:`supported` False: the routes answer 404 and
``/keryx/health`` leaves ``update_channel`` out.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

CHANNELS = ("stable", "main")


class Unsupported(Exception):
    """This Hermes has no per-install update channel."""


class BadChannel(ValueError):
    """Not a channel the app may pick."""


class NotSettable(Exception):
    """This install's channel can't be set from here (the message says why)."""


def _hermes():
    """Hermes' own update-channel surface, imported lazily; None when this Hermes predates it."""
    try:
        from hermes_cli import update_channel as uc
        from hermes_cli.config import (
            detect_install_method,
            get_config_path,
            get_project_root,
            require_readable_config_before_write,
        )
        from hermes_cli.source_check import check_for_updates
        from hermes_cli.update_contract import (
            COMMIT_BUILD_UPDATE_MESSAGE,
            is_commit_build,
        )
        from utils import atomic_roundtrip_yaml_update
    except Exception:
        return None
    needed = ("resolve_update_channel", "channel_record", "default_channel", "rides_default_channel",
              "set_install_channel", "install_id", "_channel_write_lock", "_read_stamp", "_package_channel")
    if not all(hasattr(uc, name) for name in needed):
        return None
    return SimpleNamespace(
        uc=uc, detect_install_method=detect_install_method, get_config_path=get_config_path,
        get_project_root=get_project_root, read_config=require_readable_config_before_write,
        check_for_updates=check_for_updates, is_commit_build=is_commit_build,
        commit_build_message=COMMIT_BUILD_UPDATE_MESSAGE, yaml_update=atomic_roundtrip_yaml_update,
    )


def supported() -> bool:
    return _hermes() is not None


def _settable(h, root: Path) -> tuple[bool, str | None]:
    """Whether this install's channel can be set, and why not. The same refusals as
    ``set_install_channel``, plus channels being a source-install (git) thing only."""
    if h.is_commit_build(root):
        return False, h.commit_build_message
    stamp = h.uc._read_stamp(root)
    if h.uc._package_channel(stamp) or stamp.get("updateMechanism") == "external":
        owner = stamp.get("distribution") or "an external steward"
        return False, f"Update channels don't apply to this install; its updates are owned by {owner}."
    method = h.detect_install_method(root)
    if method != "git":
        return False, f"Update channels apply to source (git) installs; this one was installed with {method}."
    return True, None


def _cache_path(channel: str) -> Path:
    # Per user and channel, outside HERMES_HOME: Hermes' own check cache stays untouched.
    base = Path(tempfile.gettempdir()) / f"keryx-stream-update-{os.getuid() if hasattr(os, 'getuid') else 'u'}"
    base.mkdir(mode=0o700, exist_ok=True)
    return base / f"{channel}.json"


def _message(out: dict, check: dict) -> str | None:
    version = out["stable_version"]
    behind = out["behind"]
    if check.get("error"):
        return check.get("message") or "Couldn't reach the update source. Try again later."
    if out["channel"] == "stable":
        if out["ahead_of_stable"] and out["forward_only"]:
            return f"This build is newer than stable v{version}. You'll get the next stable release."
        if out["ahead_of_stable"]:
            return f"This build is newer than stable v{version}. Updating moves it back to that release."
        if behind == 0:
            return f"Stable v{version} · up to date." if version else "Up to date."
        if behind is not None:
            return f"Stable v{version} is available." if version else "A stable release is available."
        return None
    if behind == 0:
        return "Edge · up to date."
    if behind is not None and behind > 0:
        return f"Edge · {behind} new change{'s' if behind != 1 else ''}."
    if behind is not None:
        return "Edge · new changes are available."
    return None


def status(*, force: bool = False, offline: bool = False) -> dict:
    """The ``GET`` payload. ``offline`` skips the release lookups (channel facts only)."""
    h = _hermes()
    if h is None:
        raise Unsupported()
    root = Path(h.get_project_root()).resolve()
    config = h.read_config(h.get_config_path())
    channel = h.uc.resolve_update_channel(config, root)
    can_set, why = _settable(h, root)
    out = {
        "channel": channel,
        "default": h.uc.default_channel(root),
        "forward_only": bool(h.uc.rides_default_channel(h.uc.channel_record(config, root), channel, root)),
        "can_set": can_set,
        "stable_version": None,
        "behind": None,
        "ahead_of_stable": False,
        "message": why,
    }
    if offline or not can_set:
        return out
    stable = h.check_for_updates(channel="stable", cache_path=_cache_path("stable"), force=force)
    out["stable_version"] = stable.get("sourceVersion")
    # Newer than the release: pinned to HEAD when riding the default (aheadOfRelease),
    # or a move back once stable is saved explicitly (rollback).
    out["ahead_of_stable"] = bool(stable.get("aheadOfRelease") or stable.get("rollback"))
    current = stable if channel == "stable" else h.check_for_updates(
        channel=channel, cache_path=_cache_path(channel), force=force)
    behind = current.get("behind")
    # -1 = updates exist but the count is unknown (Hermes' UPDATE_AVAILABLE_NO_COUNT).
    out["behind"] = behind if isinstance(behind, int) and not isinstance(behind, bool) else None
    out["message"] = _message(out, current)
    return out


def _forget_record(h, root: Path) -> None:
    """Drop this install's saved channel, so it rides its default again (forward-only).
    Hermes' own lock and comment-preserving writer; ``value=None`` removes the key."""
    config_path = Path(h.get_config_path())
    sha16 = h.uc.install_id(root)
    with h.uc._channel_write_lock(config_path):
        existing = h.read_config(config_path)
        update = existing.get("update")
        installs = update.get("installs") if isinstance(update, dict) else None
        if isinstance(installs, dict) and sha16 in installs:
            h.yaml_update(config_path, f"update.installs.{sha16}", None)


def switch(channel: str, stay: bool) -> None:
    """The ``PUT``: save ``channel``; ``stable`` with ``stay`` rides the default instead."""
    h = _hermes()
    if h is None:
        raise Unsupported()
    if channel not in CHANNELS:
        raise BadChannel(channel)
    root = Path(h.get_project_root()).resolve()
    can_set, why = _settable(h, root)
    if not can_set:
        raise NotSettable(why)
    if channel == "stable" and stay:
        if h.uc.default_channel(root) != "stable":
            raise NotSettable(
                "This install follows main by default (a fork or a mirror has no releases of its own), "
                "so stable has to be chosen explicitly, which moves it back to the latest release.")
        _forget_record(h, root)
        return
    try:
        h.uc.set_install_channel(channel, root)
    except ValueError as exc:
        raise NotSettable(str(exc)) from exc
