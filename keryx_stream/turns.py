"""Per-turn ordering for the side-channel: segment boundaries, the one ``stop``
per turn, the usage frame before it, and nothing after it.

Why this exists. Hermes hands every streaming observer hook to its OWN queue and
worker thread (agent/plugin_stream_hooks.py ``_start_dispatcher``: one
``plugin-stream-hook:<name>`` thread per registered callback), while the tool,
``post_api_request`` and ``post_llm_call`` hooks run on the agent's own path.
There is no ordering between those threads. The phone hangs up on ``stop`` —
anything published after it is lost — so ``stop`` must never overtake the last
``delta``, however far the delta worker lags.

The contract implemented here:

- ``on_stream_end`` never ends a turn (it fires once per API call, and a
  tool-calling call can carry text too).
- ``post_llm_call`` ends it (once per turn, after the tool loop,
  agent/turn_finalizer.py ``_apply_output_hooks``). The stop is HELD on a
  finisher thread until the text streamed for the turn's last iteration equals
  the final response, or until no delta has arrived for ``quiet_s`` (a
  transform / footer changed the text), or ``hold_s`` passes. It is published
  under the same lock the delta path publishes under, then the turn is closed:
  later deltas for it are dropped instead of leaking into the next turn.
- ``segment`` is inferred on the DELTA thread when the per-call ``iteration``
  counter moves on after a text run, so it is ordered with the deltas it
  separates by construction.
- ``post_llm_call`` does not fire for an interrupted turn or an empty final
  (turn_finalizer.py only calls ``_apply_output_hooks`` when
  ``final_response and not interrupted``). A watchdog closes such a turn with an
  empty ``stop`` once it has been idle ``idle_stop_s`` after its last stream end
  with no tool, subagent or compaction in flight.
- A compaction that rotates the session mid-turn keeps the turn id; events for
  the new session id are also published under the session the turn started on,
  so a subscriber keyed by the original session keeps receiving the turn.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("keryx_stream.turns")

_TURNS_MAX = 256
_CHILDREN_MAX = 256


@dataclass
class _Turn:
    sids: list[str]
    iter_text: list[str] = field(default_factory=list)  # text of the latest text iteration
    text_iter: Any = None          # iteration the open text run belongs to (None = no open run)
    last_delta_at: float = 0.0
    last_activity: float = 0.0
    ended_at: float = 0.0          # last on_stream_end
    tools_inflight: int = 0
    subs_inflight: int = 0
    compacting: bool = False
    usage: dict | None = None
    finishing: bool = False
    closed: bool = False


@dataclass
class _Child:
    parent_sid: str
    parent_turn: str
    child_id: str


class TurnTracker:
    def __init__(
        self,
        emit: Callable[[str, str, str | None], None],
        *,
        hold_s: float = 3.0,
        quiet_s: float = 0.3,
        idle_stop_s: float = 45.0,
        context_length: Callable[[str, str, str], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self._emit_one = emit
        self._cond = threading.Condition(threading.RLock())
        self._turns: OrderedDict[str, _Turn] = OrderedDict()
        self._children: OrderedDict[str, _Child] = OrderedDict()
        self.hold_s, self.quiet_s, self.idle_stop_s = hold_s, quiet_s, idle_stop_s
        self._context_length = context_length
        self._clock = clock
        self._background = background
        self._watchdog: threading.Thread | None = None

    # -- helpers (call with the lock held) ------------------------------------
    @staticmethod
    def _key(sid: str, turn_id: str) -> str:
        return turn_id or f"sid:{sid}"

    def _turn(self, sid: str, turn_id: str, *, create: bool = True) -> _Turn | None:
        key = self._key(sid, turn_id)
        turn = self._turns.get(key)
        if turn is not None and turn.closed and not turn_id and create:
            # No turn id to tell a late event from a new turn: a closed id-less
            # turn is over, whatever arrives next starts the next one.
            del self._turns[key]
            turn = None
        if turn is None:
            if not create:
                return None
            turn = self._turns[key] = _Turn(sids=[sid])
            while len(self._turns) > _TURNS_MAX:
                self._turns.popitem(last=False)
        elif sid and sid not in turn.sids:
            turn.sids.append(sid)  # session rotated mid-turn (compaction)
        turn.last_activity = self._clock()
        return turn

    def _emit(self, turn: _Turn, event: str, text: str | None) -> None:
        for sid in turn.sids:
            try:
                self._emit_one(sid, event, text)
            except Exception:
                logger.debug("keryx-stream: emit %s failed", event, exc_info=True)

    def _emit_tool(self, turn: _Turn, frame: dict) -> None:
        self._emit(turn, "tool", json.dumps(frame))

    # -- stream hooks ---------------------------------------------------------
    def start(self, sid: str, turn_id: str) -> None:
        with self._cond:
            turn = self._turn(sid, turn_id)
            if turn.closed:
                return
            self._emit(turn, "start", None)

    def delta(self, sid: str, turn_id: str, iteration: Any, kind: str, text: str) -> None:
        if not text:
            return
        with self._cond:
            turn = self._turn(sid, turn_id)
            if turn.closed:
                return
            moved = iteration is not None and turn.text_iter is not None and iteration != turn.text_iter
            if moved:
                # The previous API call's text run is over (a tool ran in between):
                # close it on THIS thread, so the boundary sits after its last delta.
                self._emit(turn, "segment", None)
                turn.text_iter = None
            if kind == "reasoning":
                self._emit(turn, "reasoning", text)
            else:
                if turn.text_iter is None:
                    turn.iter_text = []  # a new text run starts
                # No iteration in the payload (older Hermes): one run for the whole
                # turn, never "moved" — segments are then simply not inferred.
                turn.text_iter = iteration if iteration is not None else "run"
                turn.iter_text.append(text)
                self._emit(turn, "delta", text)
            turn.last_delta_at = self._clock()
            self._cond.notify_all()

    def interim(self, sid: str, turn_id: str, text: str) -> None:
        with self._cond:
            turn = self._turn(sid, turn_id)
            if not turn.closed:
                self._emit(turn, "interim", text)

    def stream_end(self, sid: str, turn_id: str) -> None:
        with self._cond:
            turn = self._turn(sid, turn_id)
            turn.ended_at = self._clock()
        self._arm_watchdog()

    def api_end(self, sid: str, turn_id: str, usage: dict | None) -> None:
        if not usage:
            return
        with self._cond:
            turn = self._turn(sid, turn_id)
            turn.usage = dict(usage)

    # -- tools / subagents / status -------------------------------------------
    def tool(self, sid: str, turn_id: str, frame: dict, *, phase: str) -> bool:
        """Publish a tool frame on the turn; returns False when the turn is over."""
        with self._cond:
            turn = self._turn(sid, turn_id)
            if phase == "start":
                turn.tools_inflight += 1
            elif phase == "end":
                turn.tools_inflight = max(0, turn.tools_inflight - 1)
            if turn.closed:
                return False
            self._emit_tool(turn, frame)
            return True

    def child_started(self, parent_sid: str, parent_turn: str, child_sid: str, child_id: str,
                      frame: dict) -> None:
        with self._cond:
            turn = self._turn(parent_sid, parent_turn)
            turn.subs_inflight += 1
            if child_sid:
                self._children[child_sid] = _Child(parent_sid, parent_turn, child_id)
                while len(self._children) > _CHILDREN_MAX:
                    self._children.popitem(last=False)
            if not turn.closed:
                self._emit_tool(turn, frame)

    def child_of(self, sid: str) -> _Child | None:
        with self._cond:
            return self._children.get(sid)

    def child_frame(self, child: _Child, frame: dict) -> None:
        with self._cond:
            turn = self._turn(child.parent_sid, child.parent_turn)
            if not turn.closed:
                self._emit_tool(turn, frame)

    def child_stopped(self, parent_sid: str, parent_turn: str, child_sid: str, frame: dict) -> None:
        with self._cond:
            turn = self._turn(parent_sid, parent_turn)
            turn.subs_inflight = max(0, turn.subs_inflight - 1)
            self._children.pop(child_sid, None)
            if not turn.closed:
                self._emit_tool(turn, frame)

    def status(self, sid: str, turn_id: str, frame: dict, *, compacting: bool | None = None) -> None:
        with self._cond:
            turn = self._turn(sid, turn_id)
            if compacting is not None:
                turn.compacting = compacting
            if not turn.closed:
                self._emit(turn, "status", json.dumps(frame))

    # -- turn end ---------------------------------------------------------------
    def turn_end(self, sid: str, turn_id: str, final_text: str | None) -> threading.Thread | None:
        """``post_llm_call``: hand the held stop to a finisher (the hook itself
        runs on Hermes' bounded hook path and must return promptly)."""
        with self._cond:
            turn = self._turn(sid, turn_id)
            if turn.closed or turn.finishing:
                return None
            turn.finishing = True
            usage = dict(turn.usage) if turn.usage else None
        args = (sid, turn_id, final_text if isinstance(final_text, str) else "", usage, self._clock())
        if not self._background:
            self._finish(*args)
            return None
        thread = threading.Thread(target=self._finish, args=args, name="keryx-stream-stop", daemon=True)
        thread.start()
        return thread

    def _usage_payload(self, usage: dict | None) -> str | None:
        if not usage:
            return None
        try:
            used = int(usage.get("prompt_tokens") or 0)
            model = str(usage.get("model") or "")
            cmax = 0
            if self._context_length is not None:
                cmax = int(self._context_length(model, str(usage.get("provider") or ""),
                                                str(usage.get("base_url") or "")) or 0)
            if used <= 0 or cmax <= 0:
                return None
            return json.dumps({"used": used, "max": cmax, "model": model})
        except Exception:
            logger.debug("keryx-stream: usage frame failed", exc_info=True)
            return None

    def _finish(self, sid: str, turn_id: str, final: str, usage: dict | None, ended: float) -> None:
        usage_payload = self._usage_payload(usage)  # may probe the endpoint: outside the lock
        target = final.strip()
        deadline = ended + self.hold_s
        with self._cond:
            turn = self._turn(sid, turn_id)
            while not turn.closed:
                now = self._clock()
                streamed = "".join(turn.iter_text).strip()
                if target and streamed == target:
                    break  # the whole final is on the wire
                if now - max(turn.last_delta_at, ended) >= self.quiet_s:
                    break  # transformed / footered / never streamed: the wire went quiet
                if now >= deadline:
                    break
                self._cond.wait(timeout=min(0.05, max(0.001, deadline - now)))
            self._close(turn, usage_payload, final)

    def _close(self, turn: _Turn, usage_payload: str | None, final: str | None) -> None:
        if turn.closed:
            return
        turn.closed = True
        # usage BEFORE stop: the subscriber hangs up at stop.
        if usage_payload:
            self._emit(turn, "usage", usage_payload)
        self._emit(turn, "stop", final)

    # -- watchdog -------------------------------------------------------------
    def sweep(self, now: float | None = None) -> int:
        """Close idle turns whose ``post_llm_call`` never came. Returns how many."""
        now = self._clock() if now is None else now
        closed = 0
        with self._cond:
            for turn in list(self._turns.values()):
                if (turn.closed or turn.finishing or not turn.ended_at
                        or turn.tools_inflight or turn.subs_inflight or turn.compacting):
                    continue
                if now - max(turn.last_activity, turn.ended_at) >= self.idle_stop_s:
                    self._close(turn, None, None)
                    closed += 1
        return closed

    def _arm_watchdog(self) -> None:
        if not self._background or (self._watchdog is not None and self._watchdog.is_alive()):
            return
        with self._cond:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog = threading.Thread(target=self._watch, name="keryx-stream-watchdog", daemon=True)
            self._watchdog.start()

    def _watch(self) -> None:
        idle = threading.Event()
        while True:
            idle.wait(1.0)
            try:
                self.sweep()
            except Exception:
                logger.debug("keryx-stream: watchdog sweep failed", exc_info=True)
