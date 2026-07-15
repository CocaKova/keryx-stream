"""Hub + coalescing: the load-bearing streaming primitives."""
import asyncio

import pytest

from keryx_stream.hub import KeryxStreamHub, drain_coalesced


def _q(items):
    q = asyncio.Queue()
    for it in items:
        q.put_nowait(it)
    return q


def test_drain_coalesces_consecutive_deltas():
    # first=("delta","lo"); queue holds the rest → one concatenated frame.
    q = _q([("delta", " wor"), ("delta", "ld")])
    frames, stop = drain_coalesced(q, ("delta", "lo"))
    assert frames == [("delta", "lo world")]
    assert stop is False


def test_drain_flushes_on_segment_boundary_preserving_order():
    q = _q([("segment", None), ("delta", "c")])
    frames, stop = drain_coalesced(q, ("delta", "b"))
    assert frames == [("delta", "b"), ("segment", None), ("delta", "c")]
    assert stop is False


def test_drain_stops_at_stop_and_ignores_events_after():
    q = _q([("stop", None), ("delta", "after")])
    frames, stop = drain_coalesced(q, ("delta", "x"))
    assert frames == [("delta", "x"), ("stop", None)]
    assert stop is True


def test_reasoning_and_answer_do_not_merge_across_types():
    q = _q([("delta", "answer")])
    frames, stop = drain_coalesced(q, ("reasoning", "think"))
    assert frames == [("reasoning", "think"), ("delta", "answer")]
    assert stop is False


def test_drain_uses_the_first_argument_verbatim():
    q = _q([])
    frames, stop = drain_coalesced(q, ("delta", "solo"))
    assert frames == [("delta", "solo")]
    assert stop is False


@pytest.mark.asyncio
async def test_hub_publish_reaches_subscriber_and_unsubscribe_prunes():
    h = KeryxStreamHub()
    sub = h.subscribe("matrix", "!room:server")
    assert h.has_subscribers("matrix", "!room:server") is True

    h.publish_threadsafe("matrix", "!room:server", "delta", "hi")
    await asyncio.sleep(0)  # let the scheduled offer run
    assert sub.queue.get_nowait() == ("delta", "hi")

    h.unsubscribe("matrix", "!room:server", sub)
    assert h.has_subscribers("matrix", "!room:server") is False


@pytest.mark.asyncio
async def test_hub_routing_is_scoped_by_platform_and_chat():
    h = KeryxStreamHub()
    a = h.subscribe("matrix", "roomA")
    b = h.subscribe("matrix", "roomB")
    h.publish_threadsafe("matrix", "roomA", "delta", "for-a")
    await asyncio.sleep(0)
    assert a.queue.get_nowait() == ("delta", "for-a")
    assert b.queue.empty()
