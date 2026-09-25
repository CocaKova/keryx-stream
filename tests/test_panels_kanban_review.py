"""The owner-verdict kanban routes: reply (and unblock), approve, request-changes.

The helpers run against a real temp board through hermes_cli.kanban_db; the
route tests go through the plugin's own aiohttp app so auth is exercised on
the exact handlers the app hits.
"""
import os
import sys
import warnings
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

kb = pytest.importorskip("hermes_cli.kanban_db")

from keryx_stream import PluginConfig  # noqa: E402
from keryx_stream import panels as ks  # noqa: E402
from keryx_stream.server import build_app  # noqa: E402

AUTH = {"Authorization": "Bearer t"}


def _connect():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ks._kanban_connect()[1]


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    conn = _connect()
    yield conn
    conn.close()


def _in_review(conn, title="ship it"):
    tid = ks.kanban_create(kb, conn, {"title": title, "assignee": "default"})["task_id"]
    assert kb.claim_task(conn, tid) is not None
    kb.request_review(conn, tid, summary="REVIEW: please look", force=True)
    assert kb.get_task(conn, tid).status == "review"
    return tid


def test_reply_comments_as_owner_and_unblocks(board, monkeypatch):
    tid = ks.kanban_create(kb, board, {"title": "needs input", "assignee": "default"})["task_id"]
    kb.claim_task(board, tid)
    assert kb.block_task(board, tid, reason="which branch?", kind="needs_input")
    out = ks.kanban_reply(kb, board, tid, {"body": "use main", "unblock": True})
    assert out["unblocked"] is True and out["status"] != "blocked"
    comment = kb.list_comments(board, tid)[-1]
    assert (comment.author, comment.body) == (ks.KANBAN_OWNER_DEFAULT, "use main")
    with pytest.raises(ValueError):
        ks.kanban_reply(kb, board, tid, {"body": "  "})
    assert ks.kanban_reply(kb, board, "t_nope", {"body": "x"}) is None


def test_owner_name_comes_from_config_not_the_request(board, monkeypatch):
    monkeypatch.setattr(ks, "_kanban_owner", lambda: "jonny")
    tid = ks.kanban_create(kb, board, {"title": "q", "assignee": "default"})["task_id"]
    ks.kanban_reply(kb, board, tid, {"body": "hi", "author": "hermes-system"})
    assert kb.list_comments(board, tid)[-1].author == "jonny"


def test_approve_completes_only_a_card_in_review(board):
    tid = _in_review(board)
    out = ks.kanban_approve(kb, board, tid, {"note": "lgtm"})
    assert out == {"task_id": tid, "completed": True, "status": "done"}
    assert kb.list_comments(board, tid)[-1].body == "APPROVED: lgtm"
    other = ks.kanban_create(kb, board, {"title": "not yet", "assignee": "default"})["task_id"]
    with pytest.raises(ValueError, match="only a card awaiting review"):
        ks.kanban_approve(kb, board, other, {})


def test_request_changes_reopens_a_parked_review(board):
    tid = _in_review(board)
    out = ks.kanban_request_changes(kb, board, tid, {"reason": "add tests"})
    assert out["status"] not in ("review", "done")
    assert kb.list_comments(board, tid)[-1].body.startswith("CHANGES REQUESTED: add tests")
    with pytest.raises(ValueError, match="reason is required"):
        ks.kanban_request_changes(kb, board, tid, {"reason": ""})


def test_board_marks_what_needs_the_owner(board):
    tid = _in_review(board, "review me")
    snap = ks.kanban_board_snapshot(kb, board)
    card = next(c for c in snap["tasks"]["review"] if c["id"] == tid)
    assert card["needs_you"] is True
    assert snap["needs_you"] >= 1


def _client():
    return TestClient(TestServer(build_app(PluginConfig(token="t", upstream_url=""))))


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["approve", "reply", "request-changes"])
async def test_review_routes_refuse_without_the_bearer(board, verb):
    tid = _in_review(board)
    async with _client() as client:
        path = f"/keryx/kanban/task/{tid}/{verb}"
        assert (await client.post(path, json={"note": "x"})).status == 401
        bad = {"Authorization": "Bearer wrong"}
        assert (await client.post(path, headers=bad, json={"note": "x"})).status == 401
    assert kb.get_task(board, tid).status == "review"  # nothing happened


@pytest.mark.asyncio
async def test_approve_route_with_the_bearer(board):
    tid = _in_review(board)
    async with _client() as client:
        health = await (await client.get("/keryx/health")).json()
        assert {"kanban", "kanban.review"} <= set(health["features"])
        resp = await client.post(f"/keryx/kanban/task/{tid}/approve", headers=AUTH, json={"note": "ok"})
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["status"] == "done"
        resp = await client.post("/keryx/kanban/task/t_missing/approve", headers=AUTH, json={})
        assert resp.status == 404
        again = await client.post(f"/keryx/kanban/task/{tid}/approve", headers=AUTH, json={})
        assert again.status == 400  # no longer in review
