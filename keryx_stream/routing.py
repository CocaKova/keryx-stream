"""Session → chat routing for the side-channel.

The shipped stream and tool hooks identify a turn by ``session_id`` only. A
client sitting on a chat transport doesn't know that id — the Keryx app on the
Matrix door subscribes with ``platform=matrix&chat_id=<room id>``, because the
room is all it has. So a gateway turn is published under BOTH keys: the
session key the hooks give us, and the chat key the gateway's session store
says that session belongs to.

The store arrives through ``pre_gateway_dispatch`` (a documented hook kwarg),
so this stays on the public plugin surface. Processes with no gateway (a
``hermes chat`` one-shot) never bind a store and simply publish the session
key alone.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("keryx_stream.routing")

_CACHE_MAX = 512


def _platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


class SessionRoutes:
    """Resolves and remembers the chat key behind a session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Any = None
        self._cache: OrderedDict[str, tuple[str, str]] = OrderedDict()

    def bind(self, store: Any) -> None:
        if store is not None and hasattr(store, "lookup_by_session_id"):
            self._store = store

    def resolve(self, session_id: str) -> tuple[str, str] | None:
        """Look the session up in the store and cache a hit. The store scan is
        linear under its lock, so call this once per API call (stream start,
        tool start) — never per token; tokens use [chat_key]."""
        with self._lock:
            hit = self._cache.get(session_id)
        if hit is not None:
            return hit
        store = self._store
        if store is None:
            return None
        try:
            entry = store.lookup_by_session_id(session_id)
        except Exception:
            logger.debug("keryx-stream: session lookup failed", exc_info=True)
            return None
        origin = getattr(entry, "origin", None)
        platform = _platform_name(getattr(origin, "platform", None) or getattr(entry, "platform", None))
        chat_id = str(getattr(origin, "chat_id", "") or "").strip()
        if not platform or not chat_id:
            return None
        key = (platform, chat_id)
        with self._lock:
            self._cache[session_id] = key
            while len(self._cache) > _CACHE_MAX:
                self._cache.popitem(last=False)
        return key

    def chat_key(self, session_id: str) -> tuple[str, str] | None:
        """Cached chat key only — safe on the token path."""
        with self._lock:
            return self._cache.get(session_id)
