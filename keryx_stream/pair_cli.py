"""``hermes keryx``: pair a phone by QR, list the paired phones, revoke one (0.6).

``hermes keryx pair`` prints a QR holding this gateway's address, its Link's
address and a one-time code (see pairing.py for what the code can and can't
do), then waits until the phone has claimed it. Stopping the command withdraws
the code. The QR carries no key and no password.

The payload is a URI only the app's own scanner acts on. Opened as a link from a
web page, another app or the phone's camera app, it pairs nothing (the app just
points at its scanner):

    keryx://pair?v=1&gw=<gateway url>&link=<link url>&code=<code>&name=<label>

Addresses are found, in order: the command line (``--url``, ``--link``), then
``keryx_stream.pair`` in config.yaml (``gateway_url``, ``link_url``), then
``tailscale serve`` (the HTTPS name it serves the dashboard and the Link on).
"""
from __future__ import annotations

import ipaddress
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from urllib.parse import quote, urlsplit

from . import pairing

SCHEME = "keryx://pair"
PAYLOAD_VERSION = "1"
DASHBOARD_PORT = 9119
LINK_PORT = 8646
MIN_MINUTES, MAX_MINUTES = 1, 30


# ---- argparse ------------------------------------------------------------------------------

def setup(parser) -> None:
    sub = parser.add_subparsers(dest="keryx_command", metavar="{pair,devices,revoke}")
    pair = sub.add_parser(
        "pair", help="show a QR code that pairs the Keryx app with this gateway",
        description="Show a QR code for the Keryx app to scan. It holds this gateway's address, "
                    "its Link's address and a one-time code: no key, no password. The code works "
                    "once, and only until the phone pairs, the time runs out or you stop this command.")
    pair.add_argument("--url", help="the dashboard address your phone uses, e.g. https://box.tailnet.ts.net:9119")
    pair.add_argument("--link", help="the Hermes Link address your phone uses (default: same host, port 8646)")
    pair.add_argument("--name", help="what the app calls this gateway (default: this machine's name)")
    pair.add_argument("--minutes", type=int, default=pairing.DEFAULT_TTL_S // 60,
                      help=f"how long the code stays valid, {MIN_MINUTES}-{MAX_MINUTES} (default 10)")
    sub.add_parser("devices", help="list the phones paired with this gateway's Link")
    revoke = sub.add_parser("revoke", help="unpair a phone: its Link token stops working at once")
    revoke.add_argument("device", help="the device id (or its name, when only one has it)")


def handle(args) -> int:
    command = getattr(args, "keryx_command", None)
    if command == "pair":
        return cmd_pair(args)
    if command == "devices":
        return cmd_devices()
    if command == "revoke":
        return cmd_revoke(args.device)
    print("usage: hermes keryx {pair,devices,revoke}  (hermes keryx pair -h for help)")
    return 2


# ---- addresses -----------------------------------------------------------------------------

class AddressError(ValueError):
    """An address the phone can't use; the message says what to pass instead."""


def check_url(raw: str, what: str) -> str:
    """``raw`` as a clean ``http(s)://host[:port][/path]``, or AddressError."""
    url = (raw or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise AddressError(f"{what} must be an http:// or https:// address, got {raw!r}")
    if parts.username or parts.password:
        raise AddressError(f"{what} can't carry a user name or password: {raw!r}")
    if parts.query or parts.fragment:
        raise AddressError(f"{what} can't carry a ?query or #fragment: {raw!r}")
    return url.rstrip("/")


def is_private_host(host: str) -> bool:
    """A host only reachable on a private network or a tailnet, where plain
    http doesn't cross the open internet."""
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith((".ts.net", ".local", ".lan", ".internal", ".home.arpa")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _plugin_config() -> dict:
    try:
        from hermes_cli.config import load_config
        return (load_config() or {}).get("keryx_stream", {}) or {}
    except Exception:
        return {}


def tailscale_serve_urls(port: int) -> list[str]:
    """The https addresses ``tailscale serve`` fronts local ``port`` with."""
    exe = shutil.which("tailscale")
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "serve", "status", "--json"], capture_output=True,
                             text=True, timeout=5, check=False).stdout
        web = (json.loads(out or "{}") or {}).get("Web") or {}
    except Exception:
        return []
    return serve_urls_for(web, port)


def serve_urls_for(web: dict, port: int) -> list[str]:
    found = []
    for hostport, entry in web.items():
        handlers = (entry or {}).get("Handlers") or {}
        root = handlers.get("/") or {}
        target = urlsplit(str(root.get("Proxy") or ""))
        if target.hostname in ("127.0.0.1", "localhost", "::1") and target.port == port:
            host, _, served = hostport.rpartition(":")
            found.append(f"https://{host}" if served == "443" else f"https://{hostport}")
    return sorted(found)


def resolve_addresses(url: str | None, link: str | None, cfg: dict | None = None,
                      serve=tailscale_serve_urls) -> tuple[str, str]:
    """``(gateway, link)`` for the QR. Raises AddressError with what to pass."""
    cfg = cfg if cfg is not None else _plugin_config()
    pair_cfg = cfg.get("pair") or {}
    gateway = url or str(pair_cfg.get("gateway_url") or "").strip()
    if not gateway:
        dash_port = int(pair_cfg.get("dashboard_port") or DASHBOARD_PORT)
        found = serve(dash_port)
        if len(found) != 1:
            raise AddressError(
                "Pass the dashboard address your phone uses: "
                "hermes keryx pair --url https://<host>:<port>" +
                (f" (tailscale serve has several: {', '.join(found)})" if found else ""))
        gateway = found[0]
    gateway = check_url(gateway, "The gateway address")
    link = link or str(pair_cfg.get("link_url") or "").strip()
    if not link:
        link_port = int(cfg.get("port") or LINK_PORT)
        served = [u for u in serve(link_port) if urlsplit(u).hostname == urlsplit(gateway).hostname]
        link = served[0] if served else f"http://{urlsplit(gateway).hostname}:{link_port}"
    link = check_url(link, "The Link address")
    if urlsplit(link).hostname != urlsplit(gateway).hostname:
        # The app refuses this too: a QR that sends the Link somewhere else than
        # the gateway is the shape a forged code would take.
        raise AddressError("The Link has to be on the same host as the gateway "
                           f"({urlsplit(gateway).hostname}), not {urlsplit(link).hostname}.")
    return gateway, link


def build_payload(gateway: str, link: str, code: str, name: str) -> str:
    fields = [("v", PAYLOAD_VERSION), ("gw", gateway), ("link", link), ("code", code), ("name", name)]
    return SCHEME + "?" + "&".join(f"{k}={quote(v, safe=':/')}" for k, v in fields)


# ---- the QR --------------------------------------------------------------------------------

def render_qr(text: str, quiet: int = 2) -> list[str]:
    """``text`` as terminal lines, two modules per character cell (▀ with the
    top module as foreground, the bottom one as background). Colours are set
    explicitly, black on white, so it scans on dark and light terminals alike."""
    from ._qrcodegen import QrCode

    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    n = qr.get_size()

    def dark(x: int, y: int) -> bool:
        return 0 <= x < n and 0 <= y < n and qr.get_module(x, y)

    lines = []
    for y in range(-quiet, n + quiet, 2):
        row, last = [], None
        for x in range(-quiet, n + quiet):
            colours = ("30" if dark(x, y) else "97", "40" if dark(x, y + 1) else "107")
            if colours != last:
                row.append(f"\x1b[{colours[0]};{colours[1]}m")
                last = colours
            row.append("▀")
        lines.append("".join(row) + "\x1b[0m")
    return lines


# ---- commands ------------------------------------------------------------------------------

def _link_has_pairing(cfg: dict) -> tuple[bool, str]:
    """Whether the Link running on this machine can take a claim."""
    host = str(cfg.get("host") or "0.0.0.0")
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{host}:{int(cfg.get('port') or LINK_PORT)}/keryx/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:  # noqa: S310 — local, fixed scheme
            body = json.loads(resp.read() or b"{}")
    except Exception:
        return False, (f"The Link isn't answering at {url}. Is the gateway running with "
                       "keryx-stream enabled? (hermes plugins list)")
    if "pair" not in (body.get("features") or []):
        return False, (f"The running Link is keryx-stream {body.get('version', '?')}, which can't pair "
                       "yet. Restart the gateway to load this version: hermes gateway restart")
    return True, ""


API_SERVER_OFF = (
    "Hermes's API server isn't answering behind the Link, so Missions, Runs and the Hub won't "
    "load in Keryx. Turn it on: API_SERVER_ENABLED=true and an API_SERVER_KEY in ~/.hermes/.env, "
    "then hermes gateway restart."
)


def api_server_up(cfg: dict, timeout: float = 2.0) -> bool | None:
    """Whether the API server the Link relays to answers its open ``/health``. None when the
    relay is off (``upstream_url: ""``): nothing behind the Link is expected then.

    A stock Hermes runs no API server until it's turned on, and the pairing itself works without
    one; only the app's Missions, Runs and Hub find out later, as a bare 502 (VIRDARA, 10-16)."""
    from . import _upstream_url

    upstream = _upstream_url(cfg)
    if not upstream:
        return None
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/health", timeout=timeout):  # noqa: S310
            return True
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except Exception:
        return False


def cmd_pair(args) -> int:
    cfg = _plugin_config()
    minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(args.minutes)))
    try:
        gateway, link = resolve_addresses(args.url, args.link, cfg)
    except AddressError as exc:
        print(exc, file=sys.stderr)
        return 1
    ok, why = _link_has_pairing(cfg)
    if not ok:
        print(why, file=sys.stderr)
        return 1
    name = pairing.clean_name(args.name or socket.gethostname().split(".")[0], "Hermes")
    code = pairing.mint(minutes * 60)
    try:
        expires = datetime.fromtimestamp(time.time() + minutes * 60).strftime("%H:%M")
        print()
        for line in render_qr(build_payload(gateway, link, code, name)):
            print("  " + line)
        print()
        print("  Scan this in Keryx: Connect your gateway › Scan a pairing code.")
        print(f"  Gateway {gateway}")
        print(f"  Link    {link}")
        for where, address in (("gateway", gateway), ("Link", link)):
            parts = urlsplit(address)
            if parts.scheme == "http" and not is_private_host(parts.hostname or ""):
                print(f"  ⚠ The {where} address is plain http on a public host: "
                      "traffic to it isn't encrypted.")
        if api_server_up(cfg) is False:
            print("  ⚠ " + API_SERVER_OFF)
        print(f"  The code works once, until {expires} or until you stop this command. "
              "It holds no key and no password.")
        print("  Waiting for the phone… (Ctrl-C to cancel)")
        return _wait(code)
    except KeyboardInterrupt:
        print("\n  Cancelled. The code no longer works.")
        return 130
    finally:
        pairing.withdraw(code)


def _wait(code: str, poll_s: float = 1.0) -> int:
    while True:
        state = pairing.status(code)
        if isinstance(state, dict):
            print(f"  ✓ Paired {state.get('name')} ({state.get('device_id')})"
                  + (f" from {state.get('from')}" if state.get("from") else "") + ".")
            print("  It shows in `hermes keryx devices`; `hermes keryx revoke` unpairs it.")
            return 0
        if state == "expired":
            print("  The code expired before a phone claimed it. Run `hermes keryx pair` again.")
            return 1
        if state == "burned":
            print("  ⚠ Five wrong codes were tried, so this one was withdrawn. If that wasn't you, "
                  "something on your network is guessing. Run `hermes keryx pair` again.")
            return 1
        if state == "replaced":
            print("  Another `hermes keryx pair` started; this code no longer works.")
            return 1
        time.sleep(poll_s)


def cmd_devices() -> int:
    rows = pairing.devices()
    if not rows:
        print("No phones are paired with this Link. Pair one with `hermes keryx pair`.")
        return 0
    print(f"{'ID':<12} {'NAME':<24} {'PAIRED':<17} FROM")
    for d in rows:
        when = datetime.fromtimestamp(int(d.get("created_at") or 0)).strftime("%Y-%m-%d %H:%M")
        print(f"{d.get('id', ''):<12} {str(d.get('name', ''))[:24]:<24} {when:<17} {d.get('from') or ''}")
    return 0


def cmd_revoke(ref: str) -> int:
    try:
        gone = pairing.revoke(ref)
    except (LookupError, ValueError) as exc:
        print(str(exc).strip("'\""), file=sys.stderr)
        return 1
    for d in gone:
        print(f"Unpaired {d.get('name')} ({d.get('id')}): its Link token stops working now.")
    # Hermes' basic auth signs stateless tokens: a new password doesn't end a
    # session, a new signing secret ends them all (plugins/dashboard_auth/basic).
    print("Its dashboard sign-in is separate. To end that too on a lost phone with basic auth, "
          "change dashboard.basic_auth.secret and restart the dashboard (that signs out every device).")
    return 0
