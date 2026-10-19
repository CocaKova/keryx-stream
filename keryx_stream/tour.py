"""``keryx_tour``: the agent points at things on the phone.

Hermes's own guided tour (``gui_tour``, tools/tour_tool.py) is offered only to Desktop sessions
and rides the Desktop renderer's blocking-request bridge. A Keryx session is a ``tui`` session,
so this plugin offers the same tour under its own name: the same actions, the same argument
checks and messages, and the same probe-then-full-deadline ladder
(tui_gateway/server.py ``_tour_request``). Ask "where do I set a cron?" from Keryx and the
agent can spotlight the real button.

How a call travels:

1. The agent calls ``keryx_tour``. ``pre_tool_call`` hands the handler its call id
   (:func:`note_call`, same thread).
2. The gateway's own ``tool.start`` event already carries the call id and the full arguments
   to every client watching the session. Keryx runs the action in its tour engine and answers
   ``POST /keryx/tour/respond`` with what happened (the same ``{success, …}`` object Desktop's
   renderer returns).
3. The handler, in whichever process runs the agent, long-polls the Link for that answer
   (``GET /keryx/tour/wait``, master key only) and hands it to the model.

The schema, the checks and their messages are adapted from Hermes (MIT, Nous Research),
tools/tour_tool.py at 6fec745d45 — see THIRD-PARTY.md.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable

from .markers import SECTION_MAX_CHARS

logger = logging.getLogger("keryx_stream.tour")

TOOL_NAME = "keryx_tour"
TOOLSET = "keryx"

ACTIONS = ("targets", "show", "start", "next", "prev", "stop")
SIDES = ("top", "right", "bottom", "left")
PRESETS = ("quick", "full")

# The phone answers in milliseconds; the full deadline covers a stop that first has to open
# the drawer or a space. Until a session's phone has answered at all, a deadline a working one
# can't miss (Hermes's own ladder: 10 s probe, then 45 s).
FULL_TIMEOUT_S = 45
PROBE_TIMEOUT_S = 10

# What the Link keeps for a call nobody has asked about yet (the answer can beat the waiter:
# the phone hears tool.start before the handler's poll reaches the Link).
ANSWER_TTL_S = 60.0
ANSWERS_MAX = 64
ANSWER_MAX_BYTES = 64 * 1024
WAIT_MAX_S = 50

UNAVAILABLE = json.dumps({
    "success": False,
    "error": ("No Keryx phone answered the tour request. The tour is drawn by the Keryx app, which "
              "has to be open on this chat, be 3.0 or newer, and see tool progress "
              "(display.tool_progress not 'off'). Do not retry the tour in this session."),
})


def _error(message: str) -> str:
    try:
        from tools.registry import tool_error

        return tool_error(message)
    except Exception:
        return json.dumps({"error": message})


def validate(args: dict) -> str | None:
    """gui_tour's checks (tools/tour_tool.py), same messages; None = fine. Whether a selector
    names anything is the phone's to say: only it knows what's on its screen."""
    verb = str(args.get("action") or "").strip().lower()
    if verb not in ACTIONS:
        return f"action must be one of: {', '.join(ACTIONS)}."
    side, preset, steps = args.get("side"), args.get("preset"), args.get("steps")
    if side is not None and side not in SIDES:
        return f"side must be one of: {', '.join(SIDES)}."
    if preset is not None and preset not in PRESETS:
        return f"preset must be one of: {', '.join(PRESETS)}."
    if preset is not None and steps is not None:
        return "preset picks a built-in tour; pass steps or preset, not both."
    if verb == "show" and not (args.get("selector") or args.get("title") or args.get("text")):
        return "show needs a selector (and/or title/text for the popover)."
    if verb == "start" and steps is not None:
        if not isinstance(steps, list) or not steps:
            return "start needs a non-empty steps array, or no steps for the built-in tour."
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                return f"steps[{i}] must be an object."
            if not (step.get("selector") or step.get("title") or step.get("text")):
                return f"steps[{i}] needs a selector and/or title/text."
    return None


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "selector": {"type": "string", "description": "Element to highlight; omit = centered narration."},
        "title": {"type": "string", "description": "Popover title."},
        "text": {"type": "string", "description": "Popover body."},
        "side": {"type": "string", "enum": list(SIDES), "description": "Popover side; omit to auto-place."},
    },
}

SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Show the user where something is in the Keryx phone app they are chatting from: dim the "
        "screen, highlight a control, attach a titled popover. A general look around the app is ONE call: "
        "action='start' with no steps runs Keryx's built-in tour (preset 'quick' or 'full', "
        "default full). For a custom tour, call action='targets' first; only the targets it "
        "lists exist on this phone right now. Then narrate with action='show' (one highlight "
        "per call, replaces the last; pair each with a chat message) or hand over with "
        "action='start' + steps (the user gets Next/Back; 'next'/'prev' also page it). 'stop' "
        "clears. Use for how-does-X-work / where-is-Y questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string", "enum": list(ACTIONS),
                "description": "start alone runs the built-in tour; targets first for custom steps.",
            },
            "selector": {"type": "string", "description": "show: selector from targets. Omit = centered narration."},
            "title": {"type": "string", "description": "show: popover title."},
            "text": {"type": "string", "description": "show: popover body."},
            "side": {"type": "string", "enum": list(SIDES), "description": "show: popover side; omit to auto-place."},
            "steps": {"type": "array", "items": _STEP_SCHEMA, "description": "start: ordered steps; omit for the built-in tour."},
            "preset": {
                "type": "string", "enum": list(PRESETS),
                "description": "start without steps: 'quick' (the essentials) or 'full'.",
            },
            "step_index": {"type": "integer", "description": "start: 0-indexed first step."},
        },
        "required": ["action"],
    },
}


# ---- the call id, from pre_tool_call to the handler -----------------------------------------

_call = threading.local()


def note_call(tool_name: str | None, tool_call_id: str | None) -> None:
    """``pre_tool_call`` runs on the thread that then runs the handler: leave the id there."""
    if tool_name == TOOL_NAME:
        _call.id = str(tool_call_id or "")


def _take_call_id() -> str:
    cid = getattr(_call, "id", "") or ""
    _call.id = ""
    return cid


# ---- who gets the tool -------------------------------------------------------------------------

def offered() -> bool:
    """Keryx's direct door runs ``tui`` sessions; a messaging session (Telegram, Matrix, …) binds
    its platform and has no Keryx screen to point at, and Desktop has ``gui_tour`` itself.
    Depends only on the session's identity, so a session's tool list never changes under it."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return False
    return offered_for(get_session_env)


def offered_for(get_session_env: Callable[..., str]) -> bool:
    if get_session_env("HERMES_SESSION_PLATFORM", ""):
        return False
    return get_session_env("HERMES_SESSION_SOURCE", "").strip().lower() in ("tui", "")


# ---- the Link's mailbox (runs on the server's loop) ------------------------------------------

class Answers:
    """The phone's answers until the handler's poll takes them. A call id is unique on its own,
    so it alone matches an answer to its call (the phone's name for the session needn't be the
    agent's); the session matters only to a waiter that never learned its call id. Only touched
    from the Link's event loop."""

    def __init__(self, ttl_s: float = ANSWER_TTL_S, cap: int = ANSWERS_MAX) -> None:
        self._ttl = ttl_s
        self._cap = cap
        self._serial = 0
        # key → (stamp, session id, answer); the key is the call id, or a serial when there's none.
        self._held: OrderedDict[str, tuple[float, str, dict]] = OrderedDict()
        self._waiters: dict[str, list[asyncio.Future]] = {}

    @staticmethod
    def _call_key(call_id: str) -> str:
        return f"call:{call_id}"

    @staticmethod
    def _session_key(session_id: str) -> str:
        return f"session:{session_id}"

    def _prune(self) -> None:
        now = time.monotonic()
        for key in [k for k, (at, _, _) in self._held.items() if now - at > self._ttl]:
            del self._held[key]
        while len(self._held) > self._cap:
            self._held.popitem(last=False)

    def put(self, session_id: str, call_id: str, result: dict) -> None:
        # A waiter for exactly this call, else one in this session that didn't know its call id.
        keys = ([self._call_key(call_id)] if call_id else []) + [self._session_key(session_id)]
        for wkey in keys:
            for fut in self._waiters.get(wkey, []):
                if not fut.done():
                    fut.set_result(result)
                    return
        if call_id:
            key = self._call_key(call_id)
        else:
            self._serial += 1
            key = f"anon:{self._serial}"
        self._held[key] = (time.monotonic(), session_id, result)
        self._held.move_to_end(key)
        self._prune()

    def _take(self, session_id: str, call_id: str) -> dict | None:
        self._prune()
        if call_id:
            hit = self._held.pop(self._call_key(call_id), None)
            return hit[2] if hit else None
        # No call id: the oldest answer this session left.
        for key, (_, sid, _) in list(self._held.items()):
            if sid == session_id:
                return self._held.pop(key)[2]
        return None

    async def wait(self, session_id: str, call_id: str, timeout_s: float) -> dict | None:
        held = self._take(session_id, call_id)
        if held is not None:
            return held
        key = self._call_key(call_id) if call_id else self._session_key(session_id)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            waiters = self._waiters.get(key, [])
            if fut in waiters:
                waiters.remove(fut)
            if not waiters:
                self._waiters.pop(key, None)


answers = Answers()


# ---- the handler: Hermes's ladder over a long poll ---------------------------------------------

class TourBridge:
    """Waits on the Link for the phone's answer. A session's first call gets the probe deadline;
    unanswered, the session is marked so and later calls fail at once (each would otherwise
    stack a full wait per turn); answered, the full deadline from then on."""

    _STATES_MAX = 256

    def __init__(self, base_url: str, token: Callable[[], str]) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._lock = threading.Lock()
        self._state: OrderedDict[str, str] = OrderedDict()

    def _mark(self, session_id: str, state: str) -> None:
        with self._lock:
            self._state[session_id] = state
            self._state.move_to_end(session_id)
            while len(self._state) > self._STATES_MAX:
                self._state.popitem(last=False)

    def ask(self, session_id: str, call_id: str) -> str:
        with self._lock:
            state = self._state.get(session_id)
        if state == "unanswered":
            return UNAVAILABLE
        timeout = FULL_TIMEOUT_S if state == "answered" else PROBE_TIMEOUT_S
        answer = self._poll(session_id, call_id, timeout)
        if answer is not None:
            self._mark(session_id, "answered")
            return json.dumps(answer, ensure_ascii=False)
        if state != "answered":
            self._mark(session_id, "unanswered")
        return UNAVAILABLE

    def _poll(self, session_id: str, call_id: str, timeout_s: int) -> dict | None:
        query = urllib.parse.urlencode({"session_id": session_id, "call_id": call_id, "timeout": timeout_s})
        req = urllib.request.Request(
            f"{self._base}/keryx/tour/wait?{query}",
            headers={"Authorization": f"Bearer {self._token()}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s + 5) as resp:  # noqa: S310 - the Link's own URL
                if resp.status != 200:
                    return None
                body = json.loads(resp.read().decode("utf-8"))
                return body if isinstance(body, dict) else None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            logger.debug("keryx-stream: tour wait failed", exc_info=True)
            return None


def make_handler(bridge: TourBridge):
    def handle(args: dict, session_id: str = "", **_) -> str:
        args = args if isinstance(args, dict) else {}
        problem = validate(args)
        call_id = _take_call_id()
        if problem:
            return _error(problem)
        if not session_id:
            return _error("keryx_tour needs a session: it answers on the phone watching it.")
        return bridge.ask(str(session_id), call_id)

    return handle


# ---- telling the agent the tool is there --------------------------------------------------------

# Hermes's tool search defers every plugin tool: the model sees keryx_tour as one cut-off line in a
# listing and has to tool_describe it before it can call it. Asked "show me where the cron jobs
# are" from Keryx, an agent that never looked answered in words (and from memory). gui_tour is never
# deferred on Desktop, and a plugin can't ask for that, so the habit is taught where the tool is
# offered: Keryx's tui sessions. Rendered once per new session, like the markers.
SECTION_ID = "keryx.tour"
SECTION_TEXT = (
    "Keryx, the phone app this chat may come from, can point at its own screens. When the user "
    "asks where something is in the app or how to get to it, show them rather than describe it: "
    "use the keryx_tour tool. If it is only listed among deferred tools, tool_describe keryx_tour, "
    "then call it through tool_call. action='targets' lists what is on this phone right now; "
    "action='show' highlights one target with a title and a sentence (say the same in chat); "
    "action='start' with no steps runs the app's own tour. If no phone answers, say so, and describe "
    "the way only as far as you actually know it."
)


def section(info) -> str:
    """The tour note for a new session: Keryx's direct door runs ``tui`` sessions, where the tool is
    offered; anywhere else it would point at a tool the agent doesn't have."""
    platform = str((info or {}).get("platform") or "").strip().lower()
    return SECTION_TEXT if platform == "tui" else ""


def register_tool(ctx, base_url: str, token: Callable[[], str]) -> bool:
    """Offer ``keryx_tour`` to Keryx's sessions, and the prompt note that tells the agent to use it.
    False when this Hermes can't take plugin tools."""
    register = getattr(ctx, "register_tool", None)
    if register is None:
        return False
    try:
        register(
            name=TOOL_NAME, toolset=TOOLSET, schema=SCHEMA,
            handler=make_handler(TourBridge(base_url, token)), check_fn=offered,
            description=SCHEMA["description"], emoji="🧭",
        )
    except Exception:
        logger.warning("keryx-stream: could not register the keryx_tour tool", exc_info=True)
        return False
    add_section = getattr(ctx, "register_system_prompt_section", None)
    if add_section is not None:
        try:
            add_section(SECTION_ID, section, position="after_memory", max_chars=SECTION_MAX_CHARS)
        except Exception:
            logger.warning("keryx-stream: could not register the '%s' prompt section", SECTION_ID,
                           exc_info=True)
    return True
