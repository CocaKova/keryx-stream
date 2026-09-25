"""Side-channel frame builders: the ``tool`` / ``status`` / ``usage`` payloads.

Shapes match the in-tree Keryx side-channel byte for byte, so the app renders a
turn the same whichever server produced it:

  tool   {"phase":"start", "name", "preview"}
         {"phase":"end", "name", "ok", "ms", "result"?, "result_len"?, "error"?}
         {"phase":"diff", "name", "added", "removed", "diff", "truncated"}
         {"phase":"sub", "kind":"start|tool|complete", "child", "name", "preview", ...}
  status {"kind":"compacting|lifecycle|warning|ready", "text"?, "tokens"?}
  usage  {"used", "max", "model"}

Everything here is pure except the edit-diff pair, which borrows the agent's own
display helpers (``agent.display.capture_local_edit_snapshot`` /
``render_edit_diff_with_delta``) so the app shows the diff the CLI would print.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("keryx_stream.frames")

TOOL_PREVIEW_MAX = 240
# Every completion carries its result, clipped from the MIDDLE: the head says what
# the payload is, the tail is where a ``transform_tool_result`` plugin appends its
# verdict — cutting the tail would cut exactly the part a diagnosis lives in.
TOOL_RESULT_MAX = 2400
TOOL_RESULT_TAIL = 800
DIFF_MAX = 1800
_EDIT_SNAPSHOT_MAX = 32


def clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clip_middle(value: Any, limit: int = TOOL_RESULT_MAX, tail: int = TOOL_RESULT_TAIL) -> str:
    """Clip to ``limit`` keeping both ends; the elision line says how much is
    missing so a clipped body never reads as the whole."""
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").strip()
    if len(text) <= limit:
        return text
    tail = max(0, min(tail, limit // 2))
    head = limit - tail
    elided = len(text) - head - tail
    return f"{text[:head].rstrip()}\n⋯ {elided:,} chars elided ⋯\n{text[len(text) - tail:].lstrip()}"


def tool_start_frame(tool_name: Any, args: Any) -> dict[str, Any]:
    return {"phase": "start", "name": str(tool_name or "tool"), "preview": clip(args, TOOL_PREVIEW_MAX)}


def tool_end_frame(tool_name: Any, *, result: Any = None, status: Any = None,
                   duration_ms: Any = 0, error_message: Any = None) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "phase": "end", "name": str(tool_name or "tool"),
        "ok": (status or "ok") == "ok", "ms": int(duration_ms or 0),
    }
    if error_message:
        frame["error"] = clip(error_message, 200)
    clipped = clip_middle(result)
    if clipped:
        frame["result"] = clipped
        frame["result_len"] = len(str(result or ""))
    return frame


# --- subagents ---------------------------------------------------------------
# Hermes reports delegation through ``subagent_start`` / ``subagent_stop``
# (tools/delegate_tool.py, tools/delegate_tool_results.py). Their payloads name
# the child by session and subagent id; the child's own tool calls fire the
# ordinary tool hooks under the CHILD's session id and are re-attributed to the
# parent here, so the parent's wing shows what the child is doing.

def subagent_start_frame(*, child_id: str, role: Any, goal: Any, child_session: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "phase": "sub", "kind": "start", "child": child_id,
        "name": str(role or ""), "preview": clip(goal, TOOL_PREVIEW_MAX),
    }
    if goal:
        frame["goal"] = clip(goal, 200)
    if child_session:
        frame["session"] = str(child_session)
    return frame


def subagent_tool_frame(*, child_id: str, tool_name: Any, args: Any, child_session: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "phase": "sub", "kind": "tool", "child": child_id,
        "name": str(tool_name or "tool"), "preview": clip(args, TOOL_PREVIEW_MAX),
    }
    if child_session:
        frame["session"] = str(child_session)
    return frame


def subagent_stop_frame(*, child_id: str, role: Any, status: Any, summary: Any,
                        duration_ms: Any, tool_calls: Any, child_session: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "phase": "sub", "kind": "complete", "child": child_id,
        "name": str(role or ""), "preview": clip(summary, TOOL_PREVIEW_MAX),
    }
    if status:
        frame["status"] = clip(status, 200)
    if summary:
        # For a background fan-out the summary IS the work — the only place the
        # child's result exists on the phone.
        frame["summary"] = clip(summary, 600)
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool) and duration_ms:
        frame["duration_seconds"] = float(duration_ms) / 1000.0
    if isinstance(tool_calls, (list, tuple)):
        frame["tool_count"] = len(tool_calls)
    if child_session:
        frame["session"] = str(child_session)
    return frame


# --- inline edit diffs -------------------------------------------------------
# post_tool_call carries the tool's RESULT, which for an edit tool is a success
# envelope, not a diff; the diff only exists by comparing the file against what
# it was before the call. pre_tool_call snapshots, post_tool_call renders, both
# keyed by the ``tool_call_id`` the tool hooks carry.

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class EditDiffs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: OrderedDict[str, Any] = OrderedDict()

    @staticmethod
    def available() -> bool:
        try:
            from agent.display import (  # noqa: F401
                capture_local_edit_snapshot,
                render_edit_diff_with_delta,
            )

            return True
        except Exception:
            return False

    def capture(self, tool_call_id: str, tool_name: str, args: Any, task_id: Any = None) -> None:
        if not tool_call_id:
            return
        try:
            from agent.display import capture_local_edit_snapshot

            kwargs = {"task_id": task_id} if task_id else {}
            snapshot = capture_local_edit_snapshot(tool_name, args if isinstance(args, dict) else {}, **kwargs)
        except TypeError:
            try:
                from agent.display import capture_local_edit_snapshot

                snapshot = capture_local_edit_snapshot(tool_name, args if isinstance(args, dict) else {})
            except Exception:
                logger.debug("edit snapshot failed", exc_info=True)
                return
        except Exception:
            logger.debug("edit snapshot failed", exc_info=True)
            return
        if snapshot is None:
            return
        with self._lock:
            self._snapshots[str(tool_call_id)] = snapshot
            # Leak-stop for calls that start and never complete (interrupt, crash).
            while len(self._snapshots) > _EDIT_SNAPSHOT_MAX:
                self._snapshots.popitem(last=False)

    def frame(self, tool_call_id: str, tool_name: str, args: Any, result: Any) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.pop(str(tool_call_id), None) if tool_call_id else None
        if snapshot is None:
            return None
        try:
            from agent.display import render_edit_diff_with_delta

            rendered: list[str] = []
            ok = render_edit_diff_with_delta(
                tool_name,
                result if isinstance(result, str) else json.dumps(result, default=str),
                function_args=args if isinstance(args, dict) else None,
                snapshot=snapshot,
                print_fn=rendered.append,
            )
        except Exception:
            logger.debug("edit diff render failed", exc_info=True)
            return None
        if not ok or not rendered:
            return None
        diff = "\n".join(rendered)
        added, removed = diff_counts(diff)
        return {
            "phase": "diff", "name": str(tool_name or "tool"),
            "added": added, "removed": removed,
            "diff": clip(diff, DIFF_MAX), "truncated": len(diff) > DIFF_MAX,
        }


def diff_counts(diff: str) -> tuple[int, int]:
    """(+added, -removed) the way the app classifies lines. The rendered lines are
    ANSI-coloured, so strip first; a ``+++``/``---`` header is not a change."""
    add = rem = 0
    for line in diff.splitlines():
        bare = _ANSI.sub("", line).lstrip()
        if bare.startswith("+++") or bare.startswith("---"):
            continue
        if bare.startswith("+"):
            add += 1
        elif bare.startswith("-"):
            rem += 1
    return add, rem


# --- status ------------------------------------------------------------------

_TOKENS_RE = re.compile(r"~\s*([\d,]+)\s*tokens")


def status_frame(kind: str, message: Any = "") -> dict[str, Any]:
    frame: dict[str, Any] = {"kind": kind}
    text = clip(message, 400)
    if text:
        frame["text"] = text
        m = _TOKENS_RE.search(text)
        if m:
            try:
                frame["tokens"] = int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return frame


def compaction_text(approx_tokens: Any) -> str:
    try:
        n = int(approx_tokens or 0)
    except (TypeError, ValueError):
        n = 0
    return f"📦 Compacting context: ~{n:,} tokens" if n > 0 else "📦 Compacting context"
