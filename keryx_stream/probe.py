"""Feature detection and operator hints.

Three sources, never a guess:

- **registered** — the hooks / middleware ``register()`` handed to Hermes.
  Hermes stores an unknown hook name with only a warning
  (hermes_cli/plugins.py ``_track_callback``), so ``register()`` checks each
  name against the host's ``VALID_HOOKS`` first and skips the ones it lacks;
  this set is what the running Hermes actually provides.
- **seen** — payload traits observed on real hook calls (``post_tool_call``
  carried a ``tool_call_id``, stream deltas carried an ``iteration``). A field a
  Hermes release dropped simply never shows up here.
- **fed** — events that actually went out on the side-channel, including those
  another process pushed through ``POST /keryx/publish`` (a forward-mode
  ``hermes chat``, or an operator's own shim). ``status`` and ``subagents`` are
  advertised only once something fed them.

Config-derived traits (``reasoning``) are read live, so flipping
``plugins.stream_reasoning_deltas`` shows up without a restart.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

logger = logging.getLogger("keryx_stream.probe")


def _load_config() -> dict:
    try:
        from hermes_cli.config import load_config

        return load_config() or {}
    except Exception:
        logger.debug("keryx-stream: config unreadable for feature probe", exc_info=True)
        return {}


def _get(cfg: dict, *path: str, default: Any = None) -> Any:
    node: Any = cfg
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Probe:
    """What this process has seen of the running Hermes."""

    def __init__(self, config_loader=_load_config) -> None:
        self._lock = threading.Lock()
        self._registered: set[str] = set()
        self._seen: set[str] = set()
        self._fed: set[str] = set()
        self._config_loader = config_loader

    # -- inputs ---------------------------------------------------------------
    def registered(self, name: str) -> None:
        with self._lock:
            self._registered.add(name)

    def seen(self, trait: str) -> None:
        if trait in self._seen:  # hot path: no lock for the common repeat
            return
        with self._lock:
            self._seen.add(trait)

    def note_event(self, event: str, text: Any = None) -> None:
        """Record one published side-channel event (in-process or forwarded)."""
        key = str(event or "")
        if key == "tool" and isinstance(text, str) and '"phase"' in text:
            try:
                phase = str(json.loads(text).get("phase") or "")
            except Exception:
                phase = ""
            key = {"sub": "tool.sub", "diff": "tool.diff"}.get(phase, "tool")
        if key in self._fed:
            return
        with self._lock:
            self._fed.add(key)

    def reset(self) -> None:
        with self._lock:
            self._registered.clear()
            self._seen.clear()
            self._fed.clear()

    # -- outputs --------------------------------------------------------------
    def has(self, name: str) -> bool:
        return name in self._registered

    def stream_features(self) -> list[str]:
        reg, seen, fed = self._registered, self._seen, self._fed
        out: list[str] = []

        def add(name: str, cond: bool) -> None:
            if cond and name not in out:
                out.append(name)

        cfg = self._config_loader()
        add("reasoning",
            ("on_stream_delta" in reg and _truthy(_get(cfg, "plugins", "stream_reasoning_deltas", default=False)))
            or "reasoning" in fed)
        add("interim", "on_interim_message" in reg or "interim" in fed)
        add("tools", {"pre_tool_call", "post_tool_call"} <= reg or "tool" in fed)
        add("tools.diff", "tool.diff" in fed
            or ({"pre_tool_call", "post_tool_call"} <= reg and "tool_call_id" in seen and "edit_helpers" in seen))
        # Boundaries are inferred from the per-API-call ``iteration`` counter the
        # stream payloads carry; without it a segment could only be guessed.
        add("segment", "segment" in fed or "iteration" in seen)
        add("stop.turn", "post_llm_call" in reg)
        add("usage", "usage" in fed)
        add("status", "status" in fed)
        add("subagents", "tool.sub" in fed)
        add("thinking", "middleware:llm_request" in reg)
        return out

    def hints(self, cfg: dict | None = None) -> list[dict[str, Any]]:
        """Config knobs the operator should set for full parity, with why."""
        cfg = self._config_loader() if cfg is None else cfg
        return config_hints(cfg)


def _matrix_configured(cfg: dict) -> bool:
    matrix = _get(cfg, "platforms", "matrix")
    if isinstance(matrix, dict) and matrix.get("enabled", True) is not False and matrix:
        return True
    return bool(os.environ.get("MATRIX_HOMESERVER", "").strip())


def config_hints(cfg: dict) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    if not _truthy(_get(cfg, "plugins", "stream_reasoning_deltas", default=False)):
        hints.append({
            "key": "plugins.stream_reasoning_deltas", "value": True,
            "why": "Hermes only hands reasoning tokens to plugins when this is on; "
                   "without it the app sees no live thinking.",
        })
    if not _truthy(_get(cfg, "compression", "progress_notices", default=False)):
        hints.append({
            "key": "compression.progress_notices", "value": True,
            "why": "chat surfaces otherwise swallow routine compaction progress, so a turn "
                   "that is compacting reads as a hang between the status frames.",
        })
    if _matrix_configured(cfg) and _get(cfg, "display", "platforms", "matrix", "streaming") is not False:
        hints.append({
            "key": "display.platforms.matrix.streaming", "value": False,
            "why": "the side-channel already carries the live tokens; Matrix edit-streaming "
                   "would repeat them as m.replace edits and bloat the homeserver.",
        })
    return hints


# Process-wide probe: hook callbacks and the server both report here.
probe = Probe()
