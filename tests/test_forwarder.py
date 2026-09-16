"""Forwarder: queueing, drop-oldest, and HTTP delivery to the hub owner."""
import json
import queue
import threading
import urllib.error

from keryx_stream.forwarder import Forwarder


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _collecting_forwarder(posts, status=200):
    fwd = Forwarder.__new__(Forwarder)
    fwd._url = "http://127.0.0.1:1/keryx/publish"
    fwd._token = "tok"
    fwd._events = queue.Queue()
    fwd._thread = threading.Thread(target=fwd._worker, daemon=True)
    fwd._thread.start()

    def fake_urlopen(request, timeout=None):
        posts.append((request.get_method(), request.full_url,
                      json.loads(request.data.decode()), dict(request.headers)))
        return FakeResponse(status)

    import keryx_stream.forwarder as mod
    orig = mod.urllib.request.urlopen
    mod.urllib.request.urlopen = fake_urlopen
    return fwd, orig


def test_post_carries_bearer_and_payload():
    posts = []
    fwd, orig = _collecting_forwarder(posts)
    try:
        fwd._post("cli", "s1", "delta", "hello")
    finally:
        import keryx_stream.forwarder as mod
        mod.urllib.request.urlopen = orig
    method, url, body, headers = posts[0]
    assert method == "POST" and url.endswith("/keryx/publish")
    assert body == {"platform": "cli", "chat_id": "s1", "event": "delta", "text": "hello"}
    assert headers.get("Authorization") == "Bearer tok"


def test_queue_drop_oldest_keeps_newest():
    fwd = Forwarder.__new__(Forwarder)
    fwd._events = queue.Queue(maxsize=1)
    fwd.publish("cli", "s1", "delta", "one")
    fwd.publish("cli", "s1", "delta", "two")
    fwd.publish("cli", "s1", "delta", "three")
    items = []
    while True:
        try:
            items.append(fwd._events.get_nowait())
        except queue.Empty:
            break
    assert items == [("cli", "s1", "delta", "three")]


def test_http_error_is_swallowed_not_raised():
    posts = []
    fwd, orig = _collecting_forwarder(posts)

    def boom(request, timeout=None):
        import email.message
        raise urllib.error.HTTPError(request.full_url, 401, "no",
                                     hdrs=email.message.Message(), fp=None)

    import keryx_stream.forwarder as mod
    mod.urllib.request.urlopen = boom
    try:
        fwd._post("cli", "s1", "stop", None)  # must not raise
    finally:
        mod.urllib.request.urlopen = orig


def test_worker_drains_queue_end_to_end():
    posts = []
    fwd, orig = _collecting_forwarder(posts)
    try:
        fwd.publish("cli", "s1", "start", None)
        fwd.publish("cli", "s1", "delta", "hello")
        fwd.publish("cli", "s1", "stop", None)
        fwd.close(timeout=5.0)
    finally:
        import keryx_stream.forwarder as mod
        mod.urllib.request.urlopen = orig
    assert [p[2]["event"] for p in posts] == ["start", "delta", "stop"]
    assert posts[-1][2]["text"] is None


def test_close_is_idempotent_after_stop():
    fwd, orig = _collecting_forwarder([])
    try:
        fwd.close(timeout=5.0)
    finally:
        import keryx_stream.forwarder as mod
        mod.urllib.request.urlopen = orig
    # Worker consumed _STOP and returned; a second close must not hang.
    fwd.close(timeout=1.0)
