"""Starter suggestions: what feeds the model, what comes back, and how often it runs."""
import json
import sqlite3
import time

from keryx_stream import suggestions as sg


def _home(tmp_path, rows=(), notes=None):
    home = tmp_path / "home"
    home.mkdir()
    conn = sqlite3.connect(home / "state.db")
    conn.execute(
        "CREATE TABLE sessions (id TEXT, title TEXT, source TEXT, hidden INT, archived INT,"
        " last_activity_at REAL, started_at REAL)"
    )
    for i, (title, source, hidden, archived) in enumerate(rows):
        conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", (str(i), title, source, hidden, archived, i, i))
    conn.commit()
    conn.close()
    if notes is not None:
        (home / "memories").mkdir()
        (home / "memories" / "USER.md").write_text(notes)
    return home


def test_titles_are_the_users_conversations_newest_first(tmp_path):
    home = _home(tmp_path, [
        ("Garden plan", "tui", 0, 0),
        ("Nightly digest", "cron", 0, 0),
        ("Bot Chat", "tui", 1, 0),
        ("Old idea", "tui", 0, 1),
        ("Garden plan #2", "tui", 0, 0),
        ("Fix the router", "telegram", 0, 0),
    ])
    assert sg.recent_titles(home) == ["Fix the router", "Garden plan"]


def test_prompts_parse_from_json_or_lines_and_are_capped():
    assert sg.parse_prompts('```json\n{"prompts": ["A", "B", "A", "C", "D", "E"]}\n```') == ["A", "B", "C", "D"]
    assert sg.parse_prompts("1. Check the garden\n- Plan the week") == ["Check the garden", "Plan the week"]
    assert sg.parse_prompts('{"prompts": ["' + "x" * 200 + '", "ok"]}') == ["ok"]


def test_nothing_known_means_no_model_call(tmp_path):
    home = _home(tmp_path)
    called = []
    assert sg.generate(home, call=lambda m: called.append(m) or "") == []
    assert called == []


def test_the_model_sees_titles_and_notes(tmp_path):
    home = _home(tmp_path, [("Garden plan", "tui", 0, 0)], notes="Likes tomatoes.")
    seen = {}

    def call(messages):
        seen["user"] = messages[-1]["content"]
        return '{"prompts": ["How are the tomatoes?"]}'

    assert sg.generate(home, call=call) == ["How are the tomatoes?"]
    assert "Garden plan" in seen["user"] and "Likes tomatoes." in seen["user"]


def test_cache_serves_fresh_answers_without_regenerating(tmp_path):
    home = _home(tmp_path)
    (home / "cache").mkdir()
    (home / "cache" / sg.CACHE_FILE).write_text(json.dumps({"prompts": ["Hi"], "generated_at": time.time()}))
    calls = []
    cache = sg.SuggestionCache(generator=lambda h: calls.append(h) or ["new"])
    assert cache.get(home) == {"prompts": ["Hi"], "generated_at": json.loads(
        (home / "cache" / sg.CACHE_FILE).read_text())["generated_at"], "pending": False}
    assert calls == []


def test_a_cold_profile_is_pending_then_answered(tmp_path):
    home = _home(tmp_path)
    cache = sg.SuggestionCache(generator=lambda h: ["Plan my day"])
    first = cache.get(home)
    assert first["prompts"] == [] and first["pending"] is True
    for _ in range(100):
        if cache.get(home)["prompts"]:
            break
        time.sleep(0.02)
    assert cache.get(home)["prompts"] == ["Plan my day"]


def test_a_failed_generation_is_not_retried_at_once(tmp_path):
    home = _home(tmp_path)
    calls = []

    def boom(h):
        calls.append(h)
        raise RuntimeError("model down")

    cache = sg.SuggestionCache(generator=boom)
    cache.get(home)
    for _ in range(100):
        if not cache._running:
            break
        time.sleep(0.02)
    assert cache.get(home)["pending"] is False
    assert len(calls) == 1
