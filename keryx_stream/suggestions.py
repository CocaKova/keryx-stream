"""Starter suggestions for an empty chat (Keryx 2.17.3).

``GET /keryx/suggestions?profile=<name>`` answers four short messages the user
might send next, drawn from what THIS install's user actually talks about: the
profile's recent conversation titles and its ``memories/USER.md``. They are
written by the auxiliary model Hermes already uses for session titles, so they
cost one small call per profile, and never per screen open: the answer is
cached on disk for a few hours and refreshed in the background. A caller that
arrives while nothing is cached gets ``pending: true`` and asks again later;
the app keeps its own generic starters meanwhile.

Everything is read from the profile's own home, so a bot's suggestions come
from that bot's conversations, and nothing here is specific to one user.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger("keryx_stream.suggestions")

CACHE_TTL_S = 6 * 3600
RETRY_AFTER_FAILURE_S = 15 * 60
CACHE_FILE = "keryx_suggestions.json"
MAX_PROMPTS = 4
MAX_PROMPT_CHARS = 80
MAX_TITLES = 40
MAX_USER_NOTES_CHARS = 1500

# Sessions that are machinery, not the user talking: their titles say nothing about
# what the user cares about (and a cron job's title would just be suggested back).
_MACHINE_SOURCES = ("cron", "subagent", "kanban", "api_server", "oneshot", "hermes_browser")

_SYSTEM_PROMPT = (
    "You write conversation starters for a personal AI agent's chat app. From the user's "
    "recent conversation titles and their profile notes, write exactly four short messages "
    "the user is likely to want to send next. Make them specific to the topics and ongoing "
    "work that recur, not generic chatbot questions. Write each in the first person, as the "
    "user talking to the agent, under 60 characters, with no numbering or quotes. Return only "
    'JSON: {"prompts": ["...", "...", "...", "..."]}'
)


def recent_titles(home: Path, limit: int = MAX_TITLES) -> list[str]:
    """Titles of the profile's recent human conversations, newest first, deduplicated."""
    db = home / "state.db"
    if not db.exists():
        return []
    marks = ",".join("?" for _ in _MACHINE_SOURCES)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            f"SELECT title FROM sessions WHERE title IS NOT NULL AND title != '' "
            f"AND COALESCE(hidden, 0) = 0 AND COALESCE(archived, 0) = 0 "
            f"AND COALESCE(source, '') NOT IN ({marks}) "
            f"ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?",
            (*_MACHINE_SOURCES, limit * 2),
        ).fetchall()
    finally:
        conn.close()
    out: list[str] = []
    for (title,) in rows:
        t = re.sub(r"\s+#\d+$", "", str(title).strip())  # "Topic #3" continuations are one topic
        if t and not t.startswith("Bot Chat") and t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def user_notes(home: Path) -> str:
    path = home / "memories" / "USER.md"
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()[:MAX_USER_NOTES_CHARS]
    except OSError:
        return ""


def build_messages(titles: list[str], notes: str) -> list[dict]:
    parts = []
    if titles:
        parts.append("Recent conversation titles (newest first):\n" + "\n".join(f"- {t}" for t in titles))
    if notes:
        parts.append("About the user:\n" + notes)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


def parse_prompts(text: str) -> list[str]:
    """The prompts in a model reply: the JSON contract first, one-per-line as the fallback."""
    raw = (text or "").strip()
    candidates: list = []
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and isinstance(data.get("prompts"), list):
                candidates = data["prompts"]
        except ValueError:
            pass
    if not candidates:
        candidates = [ln for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith(("{", "}", "```"))]
    out: list[str] = []
    for c in candidates:
        if not isinstance(c, str):
            continue
        p = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", c).strip().strip('"').strip()
        if p and len(p) <= MAX_PROMPT_CHARS and p not in out:
            out.append(p)
        if len(out) >= MAX_PROMPTS:
            break
    return out


def _call_aux(messages: list[dict]) -> str:
    from agent.auxiliary_client import call_llm

    response = call_llm(
        # The title model: already configured, small, and the right weight for four lines.
        task="title_generation",
        messages=messages,
        max_tokens=400,
        temperature=None,
        timeout=60,
        reasoning_config={"enabled": False},
    )
    return response.choices[0].message.content or ""


def generate(home: Path, call: Callable[[list[dict]], str] = _call_aux) -> list[str]:
    titles = recent_titles(home)
    notes = user_notes(home)
    if not titles and not notes:
        return []  # nothing known about this user yet: the app's generic starters fit better
    return parse_prompts(call(build_messages(titles, notes)))


class SuggestionCache:
    """One answer per profile home: served from disk, regenerated in the background,
    single-flight so a burst of empty chats is one model call."""

    def __init__(self, generator: Callable[[Path], list[str]] = generate, now: Callable[[], float] = time.time):
        self._generate = generator
        self._now = now
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._failed_at: dict[str, float] = {}

    @staticmethod
    def _path(home: Path) -> Path:
        return home / "cache" / CACHE_FILE

    def _read(self, home: Path) -> dict | None:
        try:
            data = json.loads(self._path(home).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("prompts"), list) else None
        except (OSError, ValueError):
            return None

    def _write(self, home: Path, prompts: list[str]) -> None:
        path = self._path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"prompts": prompts, "generated_at": self._now()}), encoding="utf-8")
        tmp.replace(path)

    def _run(self, home: Path) -> None:
        key = str(home)
        try:
            prompts = self._generate(home)
            self._write(home, prompts)
            self._failed_at.pop(key, None)
        except Exception:
            logger.warning("keryx-stream: suggestions for %s failed", home, exc_info=True)
            self._failed_at[key] = self._now()
        finally:
            with self._lock:
                self._running.discard(key)

    def _start(self, home: Path) -> bool:
        key = str(home)
        with self._lock:
            if key in self._running:
                return True
            if self._now() - self._failed_at.get(key, 0.0) < RETRY_AFTER_FAILURE_S:
                return False
            self._running.add(key)
        # The request's profile secret scope lives in contextvars; a bare thread would drop it.
        ctx = contextvars.copy_context()
        threading.Thread(target=ctx.run, args=(self._run, home), name="keryx-suggestions", daemon=True).start()
        return True

    def get(self, home: Path, refresh: bool = False) -> dict:
        cached = self._read(home)
        fresh = cached is not None and self._now() - float(cached.get("generated_at") or 0) < CACHE_TTL_S
        pending = False
        if refresh or not fresh:
            pending = self._start(home)
        return {
            "prompts": (cached or {}).get("prompts", []),
            "generated_at": (cached or {}).get("generated_at"),
            # Only a caller with nothing to show yet needs to come back.
            "pending": pending and cached is None,
        }


_CACHE = SuggestionCache()


def profile_home(name: str) -> Path:
    name = (name or "").strip()
    if not name:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(name))  # validates the name: no path escapes


def suggestions_route(request, body) -> tuple[int, dict]:
    home = profile_home(str(request.query.get("profile", "")))
    if not home.exists():
        return 404, {"error": {"message": "no such profile"}}
    refresh = str(request.query.get("refresh", "")).lower() in ("1", "true")
    return 200, _CACHE.get(home, refresh=refresh)
