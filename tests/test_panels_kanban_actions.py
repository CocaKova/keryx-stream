"""The owner's card moves: POST /keryx/kanban/task/{id}/action.

Each verb runs against a real temp board through hermes_cli.kanban_db, so a
refusal here is the same refusal `hermes kanban <verb>` would give.
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


@pytest.fixture()
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        conn = ks._kanban_connect()[1]
    yield conn
    conn.close()


def _card(conn, title="work", assignee="default"):
    return ks.kanban_create(kb, conn, {"title": title, "assignee": assignee})["task_id"]


def _blocked(conn):
    tid = _card(conn)
    kb.claim_task(conn, tid)
    assert kb.block_task(conn, tid, reason="which branch?", kind="needs_input")
    return tid


def _act(conn, tid, **payload):
    return ks.kanban_action(kb, conn, tid, payload)


def test_unblock_needs_no_reply_and_leaves_no_comment(board):
    tid = _blocked(board)
    before = len(kb.list_comments(board, tid))
    out = _act(board, tid, action="unblock")
    assert out["from"] == "blocked" and out["status"] not in ("blocked", "scheduled")
    assert len(kb.list_comments(board, tid)) == before


def test_a_note_lands_first_as_an_owner_comment(board):
    tid = _blocked(board)
    _act(board, tid, action="unblock", note="go ahead")
    last = kb.list_comments(board, tid)[-1]
    assert (last.author, last.body) == (ks.KANBAN_OWNER_DEFAULT, "UNBLOCK: go ahead")


def test_unblock_refuses_a_card_that_is_not_blocked(board):
    tid = _card(board)
    with pytest.raises(ValueError, match="only a blocked"):
        _act(board, tid, action="unblock")


def test_reclaim_returns_a_running_card(board):
    tid = _card(board)
    kb.claim_task(board, tid)
    assert kb.get_task(board, tid).status == "running"
    out = _act(board, tid, action="reclaim")
    assert out["from"] == "running" and out["status"] != "running"
    with pytest.raises(ValueError, match="nothing to reclaim"):
        _act(board, tid, action="reclaim")


def test_reassign_a_running_card_reclaims_it_first(board):
    tid = _card(board)
    kb.claim_task(board, tid)
    out = _act(board, tid, action="reassign", assignee="theo")
    assert out["assignee"] == "theo" and out["status"] != "running"
    with pytest.raises(ValueError, match="assignee is required"):
        _act(board, tid, action="reassign")


def test_block_needs_a_reason(board):
    tid = _card(board)
    with pytest.raises(ValueError, match="reason is required"):
        _act(board, tid, action="block")
    out = _act(board, tid, action="block", note="wait for the deploy")
    assert out["status"] in ("blocked", "triage")


def test_complete_then_archive(board):
    tid = _card(board)
    assert _act(board, tid, action="complete", note="did it by hand")["status"] == "done"
    with pytest.raises(ValueError, match="already done"):
        _act(board, tid, action="complete")
    assert _act(board, tid, action="archive")["status"] == "archived"


def test_promote_is_refused_while_a_parent_is_open(board):
    parent = _card(board, "first")
    child = _card(board, "second")
    kb.link_tasks(board, parent, child)
    assert kb.get_task(board, child).status == "todo"
    with pytest.raises(ValueError, match="parent"):
        _act(board, child, action="promote")


def test_promote_starts_a_card_parked_in_triage(board):
    tid = ks.kanban_create(kb, board, {"title": "parked", "assignee": "default", "triage": True})["task_id"]
    assert kb.get_task(board, tid).status == "triage"
    out = _act(board, tid, action="promote")
    assert out["from"] == "triage" and out["status"] in ("todo", "ready")


def test_unknown_verb_and_unknown_card(board):
    tid = _card(board)
    with pytest.raises(ValueError, match="unknown action"):
        _act(board, tid, action="delete")
    assert _act(board, "t_nope", action="archive") is None


def _client():
    return TestClient(TestServer(build_app(PluginConfig(token="t", upstream_url=""))))


@pytest.mark.asyncio
async def test_action_route_needs_the_bearer_and_says_why_it_refused(board):
    tid = _blocked(board)
    async with _client() as client:
        health = await (await client.get("/keryx/health")).json()
        assert "kanban.actions" in health["features"]
        path = f"/keryx/kanban/task/{tid}/action"
        assert (await client.post(path, json={"action": "unblock"})).status == 401
        assert kb.get_task(board, tid).status == "blocked"  # nothing happened
        resp = await client.post(path, headers=AUTH, json={"action": "unblock"})
        assert resp.status == 200, await resp.text()
        again = await client.post(path, headers=AUTH, json={"action": "unblock"})
        assert again.status == 400
        assert "only a blocked" in await again.text()
        missing = await client.post("/keryx/kanban/task/t_missing/action", headers=AUTH, json={"action": "archive"})
        assert missing.status == 404
