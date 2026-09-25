"""HTTP forwarder: hook events from a non-hub process to the hub owner.

When a keryx-stream instance loses the SSE-port bind (the gateway was already
running), its agent's hook callbacks still fire — a ``hermes chat`` one-shot
or any CLI process runs the same AIAgent mixins the gateway does. The
forwarder carries those events to the hub owner's ``POST /keryx/publish``
route so an attached subscriber sees the foreign session's deltas and tool
activity live.

Delivery is queued to a single worker thread: hook callbacks (pre/post tool
especially) run inline on the agent's tool path and must never block on a
network call. The queue is bounded with drop-oldest, mirroring the hub's own
overflow policy — a stalled link loses the oldest frames, never the live turn.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("keryx_stream.forwarder")

_QUEUE_MAX = 2048
_POST_TIMEOUT_S = 5.0
_STOP = object()
_TOKEN_RETRY_S = 30.0


class Forwarder:
    """Bearer-authed fire-and-forget publisher to the hub owner."""

    def __init__(self, url: str, token: str, resolve=None):
        self._url = url
        self._token = token
        # A worker that started before its credentials were reachable (scrubbed
        # env, secret source hydrated late) retries resolution instead of
        # posting unauthenticated for the life of the process.
        self._resolve = resolve
        self._next_resolve = 0.0
        self._events: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread = threading.Thread(
            target=self._worker, name="keryx-stream-forwarder", daemon=True
        )
        self._thread.start()

    def publish(self, platform: str, chat_id: str, event: str, text) -> None:
        try:
            self._events.put_nowait((str(platform), str(chat_id), str(event), text))
        except queue.Full:
            try:
                self._events.get_nowait()
                self._events.task_done()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait((str(platform), str(chat_id), str(event), text))
            except queue.Full:
                pass

    def _worker(self) -> None:
        while True:
            item = self._events.get()
            try:
                if item is _STOP:
                    return
                self._post(*item)
            except Exception:
                logger.debug("keryx-stream: forward of %s failed", item, exc_info=True)
            finally:
                self._events.task_done()

    def _current_token(self) -> str:
        resolve = getattr(self, "_resolve", None)
        if not self._token and resolve is not None and time.monotonic() >= getattr(self, "_next_resolve", 0.0):
            self._next_resolve = time.monotonic() + _TOKEN_RETRY_S
            try:
                self._token = (resolve() or ("", ""))[0]
            except Exception:
                logger.debug("keryx-stream: token re-resolve failed", exc_info=True)
        return self._token

    def _post(self, platform: str, chat_id: str, event: str, text) -> None:
        token = self._current_token()
        body = json.dumps(
            {"platform": platform, "chat_id": chat_id, "event": event, "text": text}
        ).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=_POST_TIMEOUT_S) as resp:
                if resp.status != 200:
                    logger.debug("keryx-stream: publish answered HTTP %s", resp.status)
        except urllib.error.HTTPError as exc:
            logger.debug("keryx-stream: publish refused (HTTP %s)", exc.code)
        except Exception:
            logger.debug("keryx-stream: publish unreachable", exc_info=True)

    def close(self, timeout: float = 2.0) -> None:
        """Drain and stop the worker (used by tests)."""
        self._events.put(_STOP)
        self._thread.join(timeout=timeout)
