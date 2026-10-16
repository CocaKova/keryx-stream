"""`hermes keryx pair`: addresses, the payload, the terminal QR, the wait."""
from urllib.parse import parse_qs, urlsplit

import pytest

from keryx_stream import pair_cli, pairing
from keryx_stream.pair_cli import (
    AddressError,
    build_payload,
    check_url,
    resolve_addresses,
)

SERVE = {
    "box.tail1.ts.net:9119": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9119"}}},
    "box.tail1.ts.net:443": {"Handlers": {"/": {"Proxy": "http://localhost:8646"}}},
    "box.tail1.ts.net:7777": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:7777"}}},
}


def _serve(web=SERVE):
    return lambda port: pair_cli.serve_urls_for(web, port)


def test_check_url_takes_only_plain_http_addresses():
    assert check_url(" https://box:9119/ ", "x") == "https://box:9119"
    assert check_url("http://10.0.0.5:9119/hermes", "x") == "http://10.0.0.5:9119/hermes"
    for bad in ("ftp://box", "box:9119", "https://me:pw@box", "https://box/?a=1", "https://box/#x", ""):
        with pytest.raises(AddressError):
            check_url(bad, "x")


def test_tailscale_serve_finds_the_dashboard_and_the_link():
    assert resolve_addresses(None, None, {}, _serve()) == ("https://box.tail1.ts.net:9119", "https://box.tail1.ts.net")


def test_an_explicit_url_derives_the_link_on_the_same_host():
    gw, link = resolve_addresses("https://10.0.0.5:9119", None, {}, _serve({}))
    assert (gw, link) == ("https://10.0.0.5:9119", "http://10.0.0.5:8646")
    gw, link = resolve_addresses(None, None, {"pair": {"gateway_url": "http://lan:9119"}, "port": 9000}, _serve({}))
    assert link == "http://lan:9000"


def test_no_address_says_what_to_pass():
    with pytest.raises(AddressError, match="--url"):
        resolve_addresses(None, None, {}, _serve({}))


def test_a_link_on_another_host_is_refused():
    with pytest.raises(AddressError, match="same host"):
        resolve_addresses("https://box:9119", "http://elsewhere:8646", {}, _serve({}))


def test_payload_round_trips_and_carries_no_key():
    p = build_payload("https://box.tail1.ts.net:9119", "http://box.tail1.ts.net:8646", "c0de-_x", "my box")
    assert p.startswith("keryx://pair?v=1&gw=https://box.tail1.ts.net:9119&")
    q = {k: v[0] for k, v in parse_qs(urlsplit(p).query).items()}
    assert q == {"v": "1", "gw": "https://box.tail1.ts.net:9119", "link": "http://box.tail1.ts.net:8646",
                 "code": "c0de-_x", "name": "my box"}


def test_the_terminal_qr_is_two_modules_per_line():
    from keryx_stream._qrcodegen import QrCode

    text = "keryx://pair?v=1&code=x"
    lines = pair_cli.render_qr(text, quiet=2)
    n = QrCode.encode_text(text, QrCode.Ecc.MEDIUM).get_size() + 4  # plus the quiet zone
    assert len(lines) == (n + 1) // 2
    assert all(line.count("▀") == n for line in lines)
    assert all(line.endswith("\x1b[0m") for line in lines)


def test_is_private_host():
    for host in ("10.1.2.3", "100.64.0.7", "192.168.1.2", "box.tail1.ts.net", "localhost", "nas.local", "::1"):
        assert pair_cli.is_private_host(host), host
    for host in ("8.8.8.8", "example.com"):
        assert not pair_cli.is_private_host(host), host


def test_the_wait_ends_on_the_claim(capsys):
    code = pairing.mint()
    pairing.claim(code, "Pixel", "100.64.0.7")
    assert pair_cli._wait(code, poll_s=0) == 0
    assert "Paired Pixel" in capsys.readouterr().out


def test_the_wait_ends_on_a_burn():
    code = pairing.mint()
    for _ in range(pairing.MAX_FAILURES):
        pairing.claim("guess", "x")
    assert pair_cli._wait(code, poll_s=0) == 1


def test_pair_refuses_when_the_running_link_cant_pair(monkeypatch, capsys):
    monkeypatch.setattr(pair_cli, "_plugin_config", lambda: {"pair": {"gateway_url": "https://box:9119"}})
    monkeypatch.setattr(pair_cli, "_link_has_pairing", lambda cfg: (False, "restart the gateway"))

    class Args:
        url = link = name = None
        minutes = 10

    assert pair_cli.cmd_pair(Args()) == 1
    assert "restart the gateway" in capsys.readouterr().err
    assert not (pairing.store_path()).exists()  # no code minted


def test_the_api_server_behind_the_link_is_checked(monkeypatch):
    # Nothing listens on this port: a fresh Hermes with no API server (VIRDARA, 10-16).
    assert pair_cli.api_server_up({"upstream_url": "http://127.0.0.1:9"}, timeout=0.5) is False
    # The relay turned off: nothing behind the Link is expected.
    assert pair_cli.api_server_up({"upstream_url": ""}) is None


def test_the_api_server_answering_is_up(monkeypatch):
    import http.server
    import threading

    class Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert pair_cli.api_server_up({"upstream_url": f"http://127.0.0.1:{srv.server_port}"}) is True
    finally:
        srv.shutdown()
