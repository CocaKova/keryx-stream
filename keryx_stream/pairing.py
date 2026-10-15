"""Pairing a phone by QR (0.6): ``hermes keryx pair`` mints a code, the app claims it.

The QR the CLI prints carries the gateway's address, the Link's address and a
**one-time code**, never a key. ``POST /keryx/pair/claim`` trades that code for
a **device token** of the phone's own; the master key (``API_SERVER_KEY``)
never leaves this machine.

- A code is 144 random bits, valid for one claim, for at most ``ttl`` seconds,
  and only while ``hermes keryx pair`` is still running (the command withdraws
  it on exit). A photo of the QR, the terminal's scrollback or a transcript an
  agent captured is worthless once the phone has paired or the command ended.
- Five wrong codes burn the waiting one, so nobody gets to keep guessing.
- Only hashes are stored: the code's and each device token's (SHA-256).
- A device token opens the app's routes and nothing else: ``/keryx/publish``,
  the forwarders' door into the hub, stays master-only (server.py).
- ``hermes keryx revoke`` drops one phone; the next request it makes is refused.

The gateway's own sign-in is NOT this: a gated dashboard still signs the phone
in through its own password page in the browser. This store only covers the
Link.

One JSON file in the process's Hermes home, written atomically, mode 0600. The
CLI and the gateway are different processes; a lock file serialises their
read-modify-write where ``fcntl`` exists.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("keryx_stream.pairing")

STORE_FILE = "keryx_pairing.json"
DEFAULT_TTL_S = 600
MAX_FAILURES = 5
CODE_BYTES = 18      # 144 bits → 24 url-safe characters
TOKEN_BYTES = 32
MAX_NAME = 64

_thread_lock = threading.Lock()


def _home() -> Path:
    from .auth import _process_home

    return Path(_process_home())


def store_path(home: Path | None = None) -> Path:
    return (home or _home()) / STORE_FILE


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_name(raw: object, fallback: str = "phone") -> str:
    """A device name safe to print in a terminal: no control or format
    characters (no ANSI escapes, no bidi overrides), trimmed, bounded."""
    text = "".join(
        ch for ch in str(raw or "")
        if unicodedata.category(ch)[0] != "C"
    ).strip()
    return text[:MAX_NAME] or fallback


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise read-modify-write across threads and, where ``fcntl`` exists,
    across processes (the CLI and the gateway)."""
    with _thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # Windows: threads only
            yield
            return
        lock = path.with_suffix(".lock")
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("keryx-stream: %s unreadable; treating it as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


@dataclass(frozen=True)
class Claim:
    """What ``claim`` answers: an HTTP status and the JSON body to send."""
    status: int
    body: dict


def mint(ttl_s: int = DEFAULT_TTL_S, *, home: Path | None = None, now: float | None = None) -> str:
    """A fresh one-time code. It replaces any code still waiting (one pairing
    at a time) and forgets the last claim's receipt."""
    code = secrets.token_urlsafe(CODE_BYTES)
    t = time.time() if now is None else now
    path = store_path(home)
    with _locked(path):
        data = _read(path)
        data["pending"] = {
            "code_sha256": _digest(code),
            "created_at": int(t),
            "expires_at": int(t + ttl_s),
            "failures": 0,
        }
        data.pop("claimed", None)
        data.pop("burned", None)
        _write(path, data)
    return code


def status(code: str, *, home: Path | None = None, now: float | None = None) -> str | dict:
    """Where ``code`` stands, for the waiting CLI: ``"waiting"``, ``"expired"``,
    ``"burned"`` (too many wrong tries), ``"replaced"`` (another pair started),
    or the claim receipt ``{"device_id", "name", "from", "at"}``."""
    t = time.time() if now is None else now
    data = _read(store_path(home))
    mine = _digest(code)
    claimed = data.get("claimed") or {}
    if claimed.get("code_sha256") == mine:
        return {k: claimed.get(k) for k in ("device_id", "name", "from", "at")}
    if data.get("burned") == mine:
        return "burned"
    pending = data.get("pending") or {}
    if pending.get("code_sha256") != mine:
        return "replaced"
    if t >= float(pending.get("expires_at") or 0):
        return "expired"
    return "waiting"


def withdraw(code: str, *, home: Path | None = None) -> None:
    """Take ``code`` back if it is still the one waiting (the CLI's exit)."""
    path = store_path(home)
    with _locked(path):
        data = _read(path)
        pending = data.get("pending") or {}
        if pending.get("code_sha256") == _digest(code):
            data.pop("pending", None)
            _write(path, data)


def claim(code: object, name: object, peer: str = "", *, home: Path | None = None,
          now: float | None = None) -> Claim:
    """Trade a code for a device token. Single use; wrong tries count toward
    burning the waiting code; nothing about the waiting code is revealed."""
    t = time.time() if now is None else now
    if not isinstance(code, str) or not code or len(code) > 128:
        return Claim(400, {"error": {"message": "A pairing code is required."}})
    path = store_path(home)
    with _locked(path):
        data = _read(path)
        pending = data.get("pending") or {}
        if not pending:
            return Claim(410, {"error": {"message":
                "No pairing code is waiting. Run `hermes keryx pair` on the gateway."}})
        if t >= float(pending.get("expires_at") or 0):
            data.pop("pending", None)
            _write(path, data)
            return Claim(410, {"error": {"message":
                "That pairing code expired. Run `hermes keryx pair` again."}})
        if not hmac.compare_digest(_digest(code), str(pending.get("code_sha256") or "")):
            pending["failures"] = int(pending.get("failures") or 0) + 1
            if pending["failures"] >= MAX_FAILURES:
                data["burned"] = pending.get("code_sha256")
                data.pop("pending", None)
                logger.warning("keryx-stream: pairing code burned after %d wrong tries (last from %s)",
                               MAX_FAILURES, peer or "?")
            else:
                data["pending"] = pending
            _write(path, data)
            return Claim(403, {"error": {"message": "That pairing code isn't right."}})
        token = secrets.token_urlsafe(TOKEN_BYTES)
        device = {
            "id": "d_" + secrets.token_hex(4),
            "name": clean_name(name),
            "token_sha256": _digest(token),
            "created_at": int(t),
            "from": clean_name(peer, ""),
        }
        data.setdefault("devices", []).append(device)
        data["claimed"] = {
            "code_sha256": pending.get("code_sha256"),
            "device_id": device["id"],
            "name": device["name"],
            "from": device["from"],
            "at": int(t),
        }
        data.pop("pending", None)
        _write(path, data)
    logger.info("keryx-stream: paired device %s (%s) from %s", device["id"], device["name"], peer or "?")
    return Claim(200, {"token": token, "device_id": device["id"], "name": device["name"]})


def devices(*, home: Path | None = None) -> list[dict]:
    """The paired phones, oldest first, without their token hashes."""
    rows = _read(store_path(home)).get("devices") or []
    return [{k: d.get(k) for k in ("id", "name", "created_at", "from")} for d in rows if isinstance(d, dict)]


def revoke(ref: str, *, home: Path | None = None) -> list[dict]:
    """Drop the device with id ``ref``, or the one device named ``ref``.

    Raises ``LookupError`` when nothing matches and ``ValueError`` when a name
    matches more than one device (revoke by id then)."""
    path = store_path(home)
    with _locked(path):
        data = _read(path)
        rows = [d for d in data.get("devices") or [] if isinstance(d, dict)]
        hit = [d for d in rows if d.get("id") == ref]
        if not hit:
            hit = [d for d in rows if str(d.get("name", "")).casefold() == ref.casefold()]
            if len(hit) > 1:
                raise ValueError(f"{len(hit)} devices are named {ref!r}; revoke one by its id")
        if not hit:
            raise LookupError(f"no paired device {ref!r}")
        data["devices"] = [d for d in rows if d not in hit]
        _write(path, data)
    return [{k: d.get(k) for k in ("id", "name", "created_at", "from")} for d in hit]


class DeviceTokens:
    """The server's view of the paired devices: re-read only when the file
    changes, so a revoke lands on the next request without a read per request."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._stamp: tuple[int, int] | None = None
        self._hashes: tuple[str, ...] = ()
        self._lock = threading.Lock()

    def _current(self) -> tuple[str, ...]:
        path = store_path(self._home)
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            stamp = None
        with self._lock:
            if stamp != self._stamp:
                rows = (_read(path).get("devices") or []) if stamp else []
                self._hashes = tuple(
                    str(d.get("token_sha256")) for d in rows
                    if isinstance(d, dict) and d.get("token_sha256")
                )
                self._stamp = stamp
            return self._hashes

    def verify(self, presented: str) -> bool:
        if not presented:
            return False
        mine = _digest(presented)
        ok = False
        for known in self._current():
            # Every hash is compared, so the answer's timing says nothing about where it matched.
            ok |= hmac.compare_digest(mine, known)
        return ok
