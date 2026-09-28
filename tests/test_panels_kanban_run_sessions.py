"""Run → worker session linking for the task detail (``runs[].session_id``).

Standalone: a fake kb, fake runs and a throwaway state.db with just the columns
the lookup reads, so it runs without a hermes-agent tree. The real-board path
(``kanban_task_detail`` carrying the key) is covered in test_panels_kanban_routes.
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from keryx_stream import panels as ks

T0 = 1_790_000_000


def _state_db(path: Path, sessions):
    """sessions: (id, source, title, started_at, first user prompt)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
                 "role TEXT, content TEXT)")
    for sid, source, title, started, prompt in sessions:
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, source, title, started))
        if prompt is not None:
            conn.execute("INSERT INTO messages (session_id, role, content) VALUES (?, 'user', ?)", (sid, prompt))
    conn.commit()
    conn.close()


def _run(rid, start, end, profile="default"):
    return SimpleNamespace(id=rid, profile=profile, started_at=start, ended_at=end)


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    """profile name → its home dir; resolution goes through _profile_state_db."""
    dirs = {}

    def home(name):
        d = tmp_path / name
        d.mkdir(exist_ok=True)
        dirs[name] = d
        return d

    monkeypatch.setattr(ks, "_profile_state_db",
                        lambda p: (dirs[str(p or "default")] / "state.db") if str(p or "default") in dirs else None)
    return home


def _kb(log: Path | None = None):
    return SimpleNamespace(worker_log_path=lambda tid: log or Path("/nonexistent/keryx-test.log"))


def test_each_run_gets_the_session_that_started_in_its_window(homes):
    _state_db(homes("default") / "state.db", [
        ("s1", "kanban", "Ship it", T0 + 3, "work kanban task t_1"),
        ("s2", "kanban", "Ship it #2", T0 + 103, "work kanban task t_1"),
        ("other", "kanban", "Something else", T0 + 4, "work kanban task t_9"),
        ("chat", "tui", "Ship it", T0 + 5, "hi"),  # not a worker
    ])
    task = SimpleNamespace(id="t_1", title="Ship it")
    runs = [_run(1, T0, T0 + 60), _run(2, T0 + 100, None)]
    assert ks._run_session_ids(_kb(), task, runs) == ["s1", "s2"]


def test_one_session_per_run_when_windows_overlap(homes):
    # A review hand-off opens a second run in the same second as the first.
    _state_db(homes("default") / "state.db", [("s1", "kanban", "Card", T0 + 2, None)])
    task = SimpleNamespace(id="t_1", title="Card")
    out = ks._run_session_ids(_kb(), task, [_run(1, T0, T0), _run(2, T0, T0 + 9)])
    assert sorted(out, key=str) == [None, "s1"]


def test_same_titled_card_is_not_mistaken_for_ours(homes):
    _state_db(homes("default") / "state.db", [
        ("theirs", "kanban", "Review", T0 + 1, "work kanban task t_other"),
        ("ours", "kanban", "Review #2", T0 + 4, "work kanban task t_1"),
    ])
    task = SimpleNamespace(id="t_1", title="Review")
    assert ks._run_session_ids(_kb(), task, [_run(1, T0, T0 + 30)]) == ["ours"]


def test_fallback_title_and_long_title_cap(homes):
    long = "x " * 80  # whitespace collapses, then capped with an ellipsis
    capped = ks._worker_session_title(long)
    assert len(capped) <= 96 and capped.endswith("…")
    _state_db(homes("default") / "state.db", [
        ("a", "kanban", capped, T0 + 1, None),
        ("b", "kanban", "Kanban task t_1 #2", T0 + 51, None),
    ])
    task = SimpleNamespace(id="t_1", title=long)
    assert ks._run_session_ids(_kb(), task, [_run(1, T0, T0 + 10), _run(2, T0 + 50, T0 + 60)]) == ["a", "b"]


def test_runs_read_their_own_profiles_db(homes):
    _state_db(homes("default") / "state.db", [("d", "kanban", "Card", T0 + 1, None)])
    _state_db(homes("theo") / "state.db", [("t", "kanban", "Card", T0 + 101, None)])
    task = SimpleNamespace(id="t_1", title="Card")
    runs = [_run(1, T0, T0 + 10), _run(2, T0 + 100, T0 + 110, profile="theo"),
            _run(3, T0 + 200, T0 + 210, profile="unknown")]
    assert ks._run_session_ids(_kb(), task, runs) == ["d", "t", None]


def test_worker_log_fills_in_only_when_counts_line_up(homes, tmp_path):
    homes("default")  # no state.db at all
    log = tmp_path / "t_1.log"
    log.write_text("Query: work kanban task t_1\n...\nSession:        20260927_181008_94e04c\n"
                   "Title:          Card\n[kanban-worker-exit] rc=0\n")
    task = SimpleNamespace(id="t_1", title="Card")
    assert ks._run_session_ids(_kb(log), task, [_run(1, T0, T0 + 10)]) == ["20260927_181008_94e04c"]
    # Two runs, one Session line (a crash left none): position means nothing.
    assert ks._run_session_ids(_kb(log), task, [_run(1, T0, T0 + 10), _run(2, T0 + 20, T0 + 30)]) == [None, None]


def test_never_raises(homes, tmp_path):
    bad = homes("default") / "state.db"
    bad.write_text("not a database")
    task = SimpleNamespace(id="t_1", title="Card")
    boom = SimpleNamespace(worker_log_path=lambda tid: (_ for _ in ()).throw(RuntimeError("x")))
    assert ks._run_session_ids(boom, task, [_run(1, T0, T0 + 10)]) == [None]
    assert ks._run_session_ids(boom, task, []) == []
    # A run the board never started has no window.
    assert ks._run_session_ids(_kb(), task, [_run(1, None, None)]) == [None]
