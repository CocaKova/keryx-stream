"""Bearer-token resolution that works in every Hermes process, not just the gateway.

The gateway loads ``<HERMES_HOME>/.env`` into ``os.environ`` at startup, so an
environment-only lookup works there. Other processes that load this plugin do
not always get that:

- cron's restart-safe worker is spawned with a scrubbed environment when the
  gateway multiplexes profiles (``cron/scheduler.py`` builds it with
  ``build_subprocess_env(scrub_secrets=multiplex_active)``), and inside the
  worker ``load_hermes_dotenv`` deliberately skips the ``os.environ`` load
  while multiplexing (``hermes_cli/env_loader.py``) — credentials live only in
  the profile secret scope there;
- kanban workers and other sanitized children see the same scrubbed env.

In those processes ``os.environ`` has no ``API_SERVER_KEY``, the plugin fell
into forward mode without a token, and the hub owner refused every forwarded
event. Resolution therefore walks, in order:

1. the process environment (explicit ``KERYX_STREAM_TOKEN`` / ``API_SERVER_KEY``,
   e.g. systemd ``Environment=``);
2. the ``.env`` of the process's own Hermes home — the file the gateway loaded,
   so the forwarder presents the key the hub owner holds;
3. the active profile secret scope (``agent.secret_scope.get_secret``);
4. the ``.env`` of the currently routed profile home.

Nothing is ever written back to ``os.environ``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("keryx_stream.auth")

TOKEN_VARS = ("KERYX_STREAM_TOKEN", "API_SERVER_KEY")


def _env_file_values(home) -> dict:
    if not home:
        return {}
    try:
        from agent.secret_scope import load_env_file  # Hermes' own .env tokenizer

        return load_env_file(Path(home) / ".env") or {}
    except Exception:
        logger.debug("keryx-stream: .env read failed for %s", home, exc_info=True)
        return {}


def _process_home():
    try:
        from hermes_constants import get_process_hermes_home

        return get_process_hermes_home()
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw) if raw else Path.home() / ".hermes"


def _current_home():
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home()
    except Exception:
        return None


def _scoped(name: str) -> str:
    try:
        from agent.secret_scope import get_secret

        return str(get_secret(name, "") or "")
    except Exception:  # UnscopedSecretError under multiplex with no scope, or no Hermes
        return ""


def resolve_token() -> tuple[str, str]:
    """``(token, source)``; ``("", "")`` when nothing defines one."""
    for name in TOKEN_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value, f"env:{name}"
    process_home = _process_home()
    values = _env_file_values(process_home)
    for name in TOKEN_VARS:
        value = str(values.get(name) or "").strip()
        if value:
            return value, f"dotenv:{name}"
    for name in TOKEN_VARS:
        value = _scoped(name).strip()
        if value:
            return value, f"scope:{name}"
    current = _current_home()
    if current is not None and str(current) != str(process_home):
        values = _env_file_values(current)
        for name in TOKEN_VARS:
            value = str(values.get(name) or "").strip()
            if value:
                return value, f"profile-dotenv:{name}"
    return "", ""
