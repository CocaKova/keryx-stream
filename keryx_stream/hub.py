"""In-process pub/sub stream hub keyed by (platform, chat_id).

The gateway's stream worker thread publishes token deltas via
``publish_threadsafe``; delivery hops onto each subscriber's event loop with
``call_soon_threadsafe``. Queues are bounded — a stalled subscriber drops its
own events, never blocks the agent.

Salvaged, byte-for-byte, from the original in-tree Keryx side-channel (the
coalescing is the load-bearing part: it keeps a fast brain from overflowing a
bounded queue and dropping a token, which would break the client's stream/commit
byte-match). Nothing here touches hermes-agent internals, so it unit-tests
standalone.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("keryx_stream.hub")

# Per-subscriber event buffer. Generous relative to token rate x ping interval.
_QUEUE_MAX = 2048


class _Subscription:
    __slots__ = ("queue", "loop")

    def __init__(
        self,
        queue: "asyncio.Queue[Tuple[str, Optional[str]]]",
        loop: asyncio.AbstractEventLoop,
    ):
        self.queue = queue
        self.loop = loop


class KeryxStreamHub:
    """In-process pub/sub keyed by (platform, chat_id)."""

    def __init__(self) -> None:
        self._subs: Dict[Tuple[str, str], List[_Subscription]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(platform: str, chat_id: str) -> Tuple[str, str]:
        return (str(platform).strip().lower(), str(chat_id).strip())

    def subscribe(self, platform: str, chat_id: str) -> _Subscription:
        sub = _Subscription(
            asyncio.Queue(maxsize=_QUEUE_MAX), asyncio.get_running_loop()
        )
        key = self._key(platform, chat_id)
        with self._lock:
            self._subs.setdefault(key, []).append(sub)
        logger.info("keryx subscriber attached: %s", key)
        return sub

    def unsubscribe(self, platform: str, chat_id: str, sub: _Subscription) -> None:
        key = self._key(platform, chat_id)
        with self._lock:
            lst = self._subs.get(key)
            if lst and sub in lst:
                lst.remove(sub)
                if not lst:
                    del self._subs[key]
        logger.info("keryx subscriber detached: %s", key)

    def has_subscribers(self, platform: str, chat_id: str) -> bool:
        with self._lock:
            return bool(self._subs.get(self._key(platform, chat_id)))

    def publish_threadsafe(
        self, platform: str, chat_id: str, event: str, text: Optional[str]
    ) -> None:
        """Mirror one stream event to every subscriber. Never raises, never blocks."""
        key = self._key(platform, chat_id)
        with self._lock:
            subs = list(self._subs.get(key, ()))
        for sub in subs:
            try:
                sub.loop.call_soon_threadsafe(self._offer, sub.queue, (event, text))
            except Exception:
                # Subscriber's loop is gone — pruned when its handler exits.
                pass

    @staticmethod
    def _offer(
        queue: "asyncio.Queue[Tuple[str, Optional[str]]]",
        item: Tuple[str, Optional[str]],
    ) -> None:
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.debug("keryx subscriber queue full; dropping %s", item[0])


def drain_coalesced(
    queue: "asyncio.Queue[Tuple[str, Optional[str]]]",
    first: Tuple[str, Optional[str]],
) -> Tuple[List[Tuple[str, Optional[str]]], bool]:
    """Merge a burst of queued token deltas into as few frames as possible.

    Takes the item already pulled from ``queue`` (``first``) plus everything
    currently queued (non-blocking) and returns ``(frames, stop)``: an ordered
    list of ``(event, text)`` frames ready to write, and whether a ``stop`` was
    seen (the caller then closes the channel).

    Consecutive ``delta`` (or ``reasoning``) events are concatenated into a
    single frame; ``segment``/``stop`` boundaries flush the accumulator and pass
    through in order. Byte-exact — concatenation is associative — so the client's
    accumulated stream still matches the final committed message.
    """
    pending: List[Tuple[str, Optional[str]]] = [first]
    while True:
        try:
            pending.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    frames: List[Tuple[str, Optional[str]]] = []
    buf: List[str] = []
    buf_event: Optional[str] = None
    stop = False

    def _flush() -> None:
        nonlocal buf, buf_event
        if buf:
            frames.append((buf_event or "delta", "".join(buf)))
            buf = []
            buf_event = None

    for event, text in pending:
        if event in ("delta", "reasoning"):
            if buf_event not in (None, event):
                _flush()
            buf_event = event
            buf.append(text or "")
            continue
        _flush()
        frames.append((event, text))
        if event == "stop":
            stop = True
            break
    _flush()
    return frames, stop


# Process-wide singleton — the gateway hooks publish here, the SSE server
# subscribes here.
hub = KeryxStreamHub()
