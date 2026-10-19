"""keryx_tour: gui_tour's checks, the Link's mailbox, the routes, and Hermes's wait ladder."""
import asyncio
import json
import threading

import pytest
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig, tour
from keryx_stream.server import build_app

MASTER = {"Authorization": "Bearer t"}


def _client():
    return TestClient(TestServer(build_app(PluginConfig(token="t", panels=False, upstream_url=""))))


@pytest.fixture(autouse=True)
def _fresh_answers(monkeypatch):
    monkeypatch.setattr(tour, "answers", tour.Answers())


# ---- the checks --------------------------------------------------------------------------------

@pytest.mark.parametrize("args, message", [
    ({}, "action must be one of: targets, show, start, next, prev, stop."),
    ({"action": "wave"}, "action must be one of: targets, show, start, next, prev, stop."),
    ({"action": "show"}, "show needs a selector (and/or title/text for the popover)."),
    ({"action": "show", "selector": "x", "side": "up"}, "side must be one of: top, right, bottom, left."),
    ({"action": "start", "preset": "long"}, "preset must be one of: quick, full."),
    ({"action": "start", "preset": "quick", "steps": [{"title": "a"}]},
     "preset picks a built-in tour; pass steps or preset, not both."),
    ({"action": "start", "steps": []}, "start needs a non-empty steps array, or no steps for the built-in tour."),
    ({"action": "start", "steps": ["x"]}, "steps[0] must be an object."),
    ({"action": "start", "steps": [{"title": "a"}, {}]}, "steps[1] needs a selector and/or title/text."),
])
def test_bad_calls_get_gui_tours_own_words(args, message):
    assert tour.validate(args) == message


@pytest.mark.parametrize("args", [
    {"action": "targets"},
    {"action": "start"},
    {"action": "start", "preset": "quick"},
    {"action": "show", "selector": '[data-tour="composer"]', "title": "Ask here"},
    {"action": "start", "steps": [{"selector": "door-runs", "text": "Your crons"}]},
    {"action": "NEXT"},
])
def test_good_calls_pass(args):
    assert tour.validate(args) is None


def test_the_schema_is_gui_tours_without_the_preview_surface():
    props = tour.SCHEMA["parameters"]["properties"]
    assert tour.SCHEMA["name"] == "keryx_tour"
    assert "surface" not in props
    assert set(props) == {"action", "selector", "title", "text", "side", "steps", "preset", "step_index"}


# ---- who gets the tool ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform, source, offered", [
    ("", "tui", True),        # Keryx's direct door
    ("", "", True),           # identity not bound: keep the list stable rather than guess
    ("telegram", "", False),  # a messaging session has no Keryx screen
    ("matrix", "tui", False),
    ("", "desktop", False),   # Desktop has gui_tour
    ("", "cli", False),
])
def test_offered_only_to_keryx_sessions(platform, source, offered):
    env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_SOURCE": source}
    assert tour.offered_for(lambda name, default="": env.get(name, default)) is offered


# ---- the mailbox ----------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_answer_that_beats_its_waiter_is_kept():
    box = tour.Answers()
    box.put("s1", "call-1", {"success": True})
    assert await box.wait("s1", "call-1", 1) == {"success": True}
    assert await box.wait("s1", "call-1", 0.05) is None  # taken once


@pytest.mark.asyncio
async def test_a_waiter_gets_the_answer_when_it_comes():
    box = tour.Answers()
    waiting = asyncio.ensure_future(box.wait("s1", "call-1", 2))
    await asyncio.sleep(0)
    box.put("s1", "call-1", {"success": True, "action": "show"})
    assert await waiting == {"success": True, "action": "show"}


@pytest.mark.asyncio
async def test_answers_go_to_their_own_call():
    box = tour.Answers()
    box.put("s1", "call-2", {"n": 2})
    assert await box.wait("s1", "call-1", 0.05) is None
    assert await box.wait("s1", "call-2", 1) == {"n": 2}


@pytest.mark.asyncio
async def test_a_call_id_matches_whatever_the_phone_calls_the_session():
    """The phone names the session by its stored id; the agent's id can differ (a compaction
    mid-call, a gateway that keys them apart). The call id is unique on its own."""
    box = tour.Answers()
    box.put("phone-name", "call-1", {"n": 1})
    assert await box.wait("agent-name", "call-1", 1) == {"n": 1}
    waiting = asyncio.ensure_future(box.wait("agent-name", "call-2", 2))
    await asyncio.sleep(0)
    box.put("phone-name", "call-2", {"n": 2})
    assert await waiting == {"n": 2}


@pytest.mark.asyncio
async def test_without_a_call_id_only_the_session_matches():
    box = tour.Answers()
    box.put("s2", "call-1", {"n": "other session"})
    assert await box.wait("s1", "", 0.05) is None
    assert await box.wait("s2", "", 1) == {"n": "other session"}


@pytest.mark.asyncio
async def test_a_waiter_without_a_call_id_takes_the_sessions_oldest():
    box = tour.Answers()
    box.put("s1", "a", {"n": 1})
    box.put("s1", "b", {"n": 2})
    assert await box.wait("s1", "", 1) == {"n": 1}
    waiting = asyncio.ensure_future(box.wait("s1", "", 2))
    await asyncio.sleep(0)
    assert await waiting == {"n": 2}


@pytest.mark.asyncio
async def test_old_and_surplus_answers_are_dropped():
    box = tour.Answers(ttl_s=0.01, cap=2)
    box.put("s1", "old", {"n": 0})
    await asyncio.sleep(0.03)
    assert await box.wait("s1", "old", 0.01) is None
    box = tour.Answers(cap=2)
    for i in range(3):
        box.put("s1", f"c{i}", {"n": i})
    assert await box.wait("s1", "c0", 0.01) is None
    assert await box.wait("s1", "c2", 0.1) == {"n": 2}


# ---- the routes -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_respond_then_wait_round_trip():
    async with _client() as client:
        r = await client.post("/keryx/tour/respond", headers=MASTER,
                              json={"session_id": "s1", "call_id": "c1", "result": {"success": True, "action": "start"}})
        assert r.status == 200
        r = await client.get("/keryx/tour/wait?session_id=s1&call_id=c1&timeout=2", headers=MASTER)
        assert r.status == 200
        assert await r.json() == {"success": True, "action": "start"}
        r = await client.get("/keryx/tour/wait?session_id=s1&call_id=c1&timeout=1", headers=MASTER)
        assert r.status == 204


@pytest.mark.asyncio
async def test_a_paired_phone_can_answer_but_never_read_answers():
    from keryx_stream import pairing

    code = pairing.mint(600)
    claim = pairing.claim(code, "Pixel", "127.0.0.1")
    phone = {"Authorization": f"Bearer {claim.body['token']}"}
    async with _client() as client:
        r = await client.post("/keryx/tour/respond", headers=phone,
                              json={"session_id": "s1", "call_id": "c1", "result": {"success": True}})
        assert r.status == 200
        r = await client.get("/keryx/tour/wait?session_id=s1&call_id=c1", headers=phone)
        assert r.status == 401


@pytest.mark.asyncio
async def test_respond_refuses_strangers_and_bad_bodies():
    async with _client() as client:
        r = await client.post("/keryx/tour/respond", json={"session_id": "s1", "result": {}})
        assert r.status == 401
        r = await client.post("/keryx/tour/respond", headers=MASTER, json={"session_id": "s1", "result": "yes"})
        assert r.status == 400
        r = await client.post("/keryx/tour/respond", headers=MASTER, json={"result": {}})
        assert r.status == 400
        r = await client.post("/keryx/tour/respond", headers=MASTER, data=b"not json")
        assert r.status == 400
        big = {"session_id": "s1", "result": {"text": "x" * (tour.ANSWER_MAX_BYTES + 1)}}
        r = await client.post("/keryx/tour/respond", headers=MASTER, json=big)
        assert r.status == 413


@pytest.mark.asyncio
async def test_health_advertises_the_tour():
    async with _client() as client:
        r = await client.get("/keryx/health")
        assert "tour" in (await r.json())["features"]


# ---- the handler: Hermes's ladder ----------------------------------------------------------------

class _FakeBridge(tour.TourBridge):
    def __init__(self, replies):
        super().__init__("http://link", lambda: "t")
        self.replies = list(replies)
        self.timeouts = []

    def _poll(self, session_id, call_id, timeout_s):
        self.timeouts.append(timeout_s)
        return self.replies.pop(0) if self.replies else None


def test_a_silent_phone_is_probed_once_then_left_alone():
    bridge = _FakeBridge([None])
    assert json.loads(bridge.ask("s1", "c1"))["success"] is False
    assert bridge.timeouts == [tour.PROBE_TIMEOUT_S]
    # Marked: no second wait in this session.
    assert bridge.ask("s1", "c2") == tour.UNAVAILABLE
    assert bridge.timeouts == [tour.PROBE_TIMEOUT_S]


def test_a_phone_that_answered_gets_the_full_deadline():
    bridge = _FakeBridge([{"success": True}, None, {"success": True}])
    assert json.loads(bridge.ask("s1", "c1")) == {"success": True}
    # A later miss doesn't write the session off: the phone has proven it answers.
    assert bridge.ask("s1", "c2") == tour.UNAVAILABLE
    assert json.loads(bridge.ask("s1", "c3")) == {"success": True}
    assert bridge.timeouts == [tour.PROBE_TIMEOUT_S, tour.FULL_TIMEOUT_S, tour.FULL_TIMEOUT_S]


def test_the_handler_checks_first_and_uses_its_own_call_id():
    seen = []

    class Bridge(_FakeBridge):
        def _poll(self, session_id, call_id, timeout_s):
            seen.append((session_id, call_id))
            return {"success": True}

    handle = tour.make_handler(Bridge([]))
    assert "action must be one of" in json.loads(handle({"action": "dance"}, session_id="s1"))["error"]
    tour.note_call("keryx_tour", "call-7")
    tour.note_call("terminal", "call-8")  # another tool's call never lands here
    assert json.loads(handle({"action": "targets"}, session_id="s1")) == {"success": True}
    assert seen == [("s1", "call-7")]
    # Taken once: the next call on this thread has no stale id.
    handle({"action": "targets"}, session_id="s1")
    assert seen[-1] == ("s1", "")


def test_a_call_id_stays_on_its_own_thread():
    tour.note_call("keryx_tour", "main")
    got = []
    t = threading.Thread(target=lambda: got.append(tour._take_call_id()))
    t.start()
    t.join()
    assert got == [""]
    assert tour._take_call_id() == "main"


@pytest.mark.asyncio
async def test_the_bridge_reads_a_real_link():
    """The real poll against the real routes, the way the dashboard's agent reaches the gateway's Link."""
    server = TestServer(build_app(PluginConfig(token="t", panels=False, upstream_url="")))
    async with TestClient(server) as client:
        bridge = tour.TourBridge(str(server.make_url("")), lambda: "t")
        asking = asyncio.ensure_future(asyncio.to_thread(bridge.ask, "s9", "c9"))
        await asyncio.sleep(0.2)
        r = await client.post("/keryx/tour/respond", headers=MASTER,
                              json={"session_id": "s9", "call_id": "c9", "result": {"success": True, "steps": 4}})
        assert r.status == 200
        assert json.loads(await asking) == {"success": True, "steps": 4}


# ---- telling the agent ----------------------------------------------------------------------------

class _Ctx:
    def __init__(self, sections=True):
        self.tools, self.sections = {}, {}
        if not sections:
            self.register_system_prompt_section = None

    def register_tool(self, name, **kw):
        self.tools[name] = kw

    def register_system_prompt_section(self, section_id, content, **kw):
        self.sections[section_id] = (content, kw)


def test_a_deferred_listing_still_says_what_the_tool_is_for():
    # Tool search lists a deferred tool by name and the first ~60 characters of its description.
    assert tour.SCHEMA["description"].startswith("Show the user where something is in the Keryx")


def test_the_tool_comes_with_a_note_for_keryx_sessions_only():
    ctx = _Ctx()
    assert tour.register_tool(ctx, "http://127.0.0.1:8646", lambda: "k")
    assert "keryx_tour" in ctx.tools
    render, kw = ctx.sections[tour.SECTION_ID]
    assert kw["position"] == "after_memory"
    assert len(tour.SECTION_TEXT) <= kw["max_chars"]
    assert render({"platform": "tui"}) == tour.SECTION_TEXT
    for platform in ("matrix", "telegram", "api_server", "cli", "desktop", ""):
        assert render({"platform": platform}) == ""
    assert "keryx_tour" in tour.SECTION_TEXT and "tool_describe" in tour.SECTION_TEXT


def test_a_hermes_without_prompt_sections_still_gets_the_tool():
    ctx = _Ctx(sections=False)
    assert tour.register_tool(ctx, "http://127.0.0.1:8646", lambda: "k")
    assert "keryx_tour" in ctx.tools and not ctx.sections
