"""Turn ordering: the one stop per turn, held behind the last delta.

Hermes runs every streaming observer on its own worker thread
(agent/plugin_stream_hooks.py) while post_llm_call runs on the agent's path, so
in production post_llm_call can arrive while the delta worker still has tokens
queued. These tests reproduce that race with a real lagging delta thread.
"""
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

from keryx_stream import PluginConfig, _make_hook_callbacks
from keryx_stream.turns import TurnTracker


class Wire:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = []

    def __call__(self, sid, event, text):
        with self.lock:
            self.events.append((sid, event, text))

    def names(self, sid=None):
        with self.lock:
            return [e for s, e, _ in self.events if sid is None or s == sid]


def _delta_worker(tracker, sid, turn, items, lag):
    """Stands in for Hermes' plugin-stream-hook:on_stream_delta thread."""
    q = queue.Queue()
    for it in items:
        q.put(it)

    def run():
        while not q.empty():
            iteration, text = q.get()
            time.sleep(lag)
            tracker.delta(sid, turn, iteration, "text", text)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_stop_waits_for_lagging_deltas_until_the_text_matches():
    wire = Wire()
    tracker = TurnTracker(wire, hold_s=3.0, quiet_s=0.3)
    worker = _delta_worker(tracker, "s1", "t1", [(1, "Hel"), (1, "lo"), (1, " world")], lag=0.05)
    # post_llm_call lands while every delta is still queued on the other thread.
    finisher = tracker.turn_end("s1", "t1", "Hello world")
    worker.join(2)
    finisher.join(4)
    assert wire.names() == ["delta", "delta", "delta", "stop"]
    assert wire.events[-1] == ("s1", "stop", "Hello world")


def test_transformed_final_falls_back_to_quiet_wire_and_still_trails_every_delta():
    """A footer / transform_llm_output means the streamed text never equals the
    final; the stop then waits for the wire to go quiet, not a fixed guess."""
    wire = Wire()
    tracker = TurnTracker(wire, hold_s=3.0, quiet_s=0.3)
    worker = _delta_worker(tracker, "s1", "t1", [(1, "a"), (1, "b"), (1, "c")], lag=0.1)
    finisher = tracker.turn_end("s1", "t1", "abc\n\n---\n2 files changed")
    worker.join(2)
    finisher.join(4)
    assert wire.names() == ["delta", "delta", "delta", "stop"]


def test_deltas_after_the_stop_are_dropped_not_leaked_into_the_next_turn():
    wire = Wire()
    tracker = TurnTracker(wire, hold_s=0.2, quiet_s=0.1)
    finisher = tracker.turn_end("s1", "t1", "late")
    finisher.join(2)
    tracker.delta("s1", "t1", 1, "text", "late")  # arrived after the hold expired
    tracker.start("s1", "t1")
    assert wire.names() == ["stop"]
    # the next turn on the same session streams normally
    tracker.delta("s1", "t2", 1, "text", "fresh")
    assert wire.names() == ["stop", "delta"]


def test_exactly_one_stop_per_turn():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0)
    tracker.delta("s1", "t1", 1, "text", "x")
    tracker.turn_end("s1", "t1", "x")
    tracker.turn_end("s1", "t1", "x")
    tracker.sweep(now=time.monotonic() + 3600)
    assert wire.names().count("stop") == 1


def test_usage_goes_out_before_stop():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0,
                          context_length=lambda model, provider, base: 182000)
    tracker.delta("s1", "t1", 1, "text", "ok")
    tracker.api_end("s1", "t1", {"prompt_tokens": 12345, "model": "m", "provider": "custom",
                                 "base_url": "http://x"})
    tracker.turn_end("s1", "t1", "ok")
    assert wire.names() == ["delta", "usage", "stop"]
    assert json.loads(wire.events[1][2]) == {"used": 12345, "max": 182000, "model": "m"}


def test_no_usage_frame_when_the_window_is_unknown():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0, context_length=lambda *a: 0)
    tracker.api_end("s1", "t1", {"prompt_tokens": 5, "model": "m"})
    tracker.turn_end("s1", "t1", "")
    assert wire.names() == ["stop"]


def test_segment_is_inferred_on_the_delta_thread_between_text_runs():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0)
    tracker.delta("s1", "t1", 1, "text", "first")
    tracker.delta("s1", "t1", 2, "reasoning", "hmm")   # next call's thinking
    tracker.delta("s1", "t1", 2, "text", "second")
    tracker.delta("s1", "t1", 3, "text", "third")
    assert wire.names() == ["delta", "segment", "reasoning", "delta", "segment", "delta"]


def test_no_segment_without_an_iteration_counter():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0)
    tracker.delta("s1", "t1", None, "text", "a")
    tracker.delta("s1", "t1", None, "text", "b")
    assert wire.names() == ["delta", "delta"]


def test_watchdog_waits_for_inflight_tools_and_subagents():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, idle_stop_s=10)
    tracker.stream_end("s1", "t1")
    tracker.tool("s1", "t1", {"phase": "start", "name": "terminal"}, phase="start")
    assert tracker.sweep(now=time.monotonic() + 60) == 0   # a long tool is not idleness
    tracker.tool("s1", "t1", {"phase": "end", "name": "terminal"}, phase="end")
    assert tracker.sweep(now=time.monotonic() + 60) == 1


def test_compaction_rotation_keeps_publishing_under_the_first_session():
    wire = Wire()
    tracker = TurnTracker(wire, background=False, quiet_s=0)
    tracker.delta("old", "t1", 1, "text", "a")
    tracker.delta("new", "t1", 2, "text", "b")  # same turn, rotated session id
    tracker.turn_end("new", "t1", "b")
    assert wire.names("old") == ["delta", "segment", "delta", "stop"]
    assert wire.names("new") == ["segment", "delta", "stop"]


def test_subagent_frames_land_on_the_parent_turn():
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="cli"),
                               lambda *a: published.append(a), background=False)
    cbs["on_stream_start"](session_id="parent", surface="cli", turn_id="pt")
    cbs["subagent_start"](parent_session_id="parent", parent_turn_id="pt", child_session_id="kid",
                          child_subagent_id="sa-1", child_role="researcher", child_goal="find it")
    cbs["pre_tool_call"](session_id="kid", tool_name="web_search", args={"q": "x"}, turn_id="kt")
    cbs["subagent_stop"](parent_session_id="parent", parent_turn_id="pt", child_session_id="kid",
                         child_role="researcher", child_summary="found it", child_status="completed",
                         tool_call_history=[{"tool": "web_search"}], duration_ms=1500)
    frames = [json.loads(t) for p, s, e, t in published if s == "parent" and e == "tool"]
    assert [(f["phase"], f["kind"]) for f in frames] == [("sub", "start"), ("sub", "tool"), ("sub", "complete")]
    assert frames[0]["child"] == frames[1]["child"] == frames[2]["child"] == "sa-1"
    assert frames[0]["goal"] == "find it" and frames[0]["session"] == "kid"
    assert frames[1]["name"] == "web_search"
    assert frames[2]["summary"] == "found it" and frames[2]["duration_seconds"] == 1.5
    assert frames[2]["tool_count"] == 1


def test_compaction_status_from_the_auxiliary_hooks():
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="cli"),
                               lambda *a: published.append(a), background=False)
    cbs["pre_auxiliary_call"](aux_task="title_generation", session_id="s1", turn_id="t1")
    cbs["pre_auxiliary_call"](aux_task="compression", session_id="s1", turn_id="t1",
                              approx_input_tokens=123456)
    cbs["post_auxiliary_call"](aux_task="compression", session_id="s1", turn_id="t1", error=None)
    statuses = [json.loads(t) for _, _, e, t in published if e == "status"]
    assert statuses == [
        {"kind": "compacting", "text": "📦 Compacting context: ~123,456 tokens", "tokens": 123456},
        {"kind": "ready"},
    ]


HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")


def test_edit_diff_frame_follows_the_end_frame(tmp_path):
    if str(HERMES_ROOT) not in sys.path:
        sys.path.insert(0, str(HERMES_ROOT))
    pytest.importorskip("agent.display")
    target = tmp_path / "f.txt"
    target.write_text("one\ntwo\n")
    published = []
    cbs = _make_hook_callbacks(PluginConfig(default_platform="cli"),
                               lambda *a: published.append(a), background=False)
    args = {"path": str(target), "content": "one\nTWO\nthree\n"}
    cbs["pre_tool_call"](session_id="s1", tool_name="write_file", args=args, tool_call_id="c1", turn_id="t")
    target.write_text(args["content"])
    cbs["post_tool_call"](session_id="s1", tool_name="write_file", args=args, tool_call_id="c1",
                          result=json.dumps({"success": True, "path": str(target)}), status="ok", turn_id="t")
    phases = [json.loads(t)["phase"] for _, _, e, t in published if e == "tool"]
    assert phases == ["start", "end", "diff"]
    diff = json.loads(published[-1][3])
    assert (diff["added"], diff["removed"]) == (2, 1)
