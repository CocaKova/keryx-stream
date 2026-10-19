"""Pairing by QR (0.6): one-time codes, device tokens, revoke, and the claim route."""
import json
import os
import stat

import pytest
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig, pairing
from keryx_stream.server import build_app


def _store(home):
    return json.loads((home / pairing.STORE_FILE).read_text())


def test_a_claimed_code_gives_a_token_and_stores_only_hashes(_pairing_home):
    code = pairing.mint(now=1000)
    out = pairing.claim(code, "Pixel 9", "100.64.0.7", now=1001)
    assert out.status == 200
    token = out.body["token"]
    raw = (_pairing_home / pairing.STORE_FILE).read_text()
    assert code not in raw and token not in raw
    assert _store(_pairing_home)["devices"][0]["name"] == "Pixel 9"
    if os.name == "posix":
        assert stat.S_IMODE((_pairing_home / pairing.STORE_FILE).stat().st_mode) == 0o600
    assert pairing.DeviceTokens().verify(token)
    assert not pairing.DeviceTokens().verify(code)


def test_a_code_works_once(_pairing_home):
    code = pairing.mint(now=1000)
    assert pairing.claim(code, "a", now=1001).status == 200
    assert pairing.claim(code, "b", now=1002).status == 410
    assert len(pairing.devices()) == 1


def test_an_expired_code_is_refused(_pairing_home):
    code = pairing.mint(ttl_s=60, now=1000)
    assert pairing.status(code, now=1059) == "waiting"
    assert pairing.status(code, now=1060) == "expired"
    assert pairing.claim(code, "a", now=1060).status == 410
    assert pairing.devices() == []


def test_five_wrong_codes_burn_the_waiting_one(_pairing_home):
    code = pairing.mint(now=1000)
    for _ in range(pairing.MAX_FAILURES - 1):
        assert pairing.claim("guess", "x", now=1001).status == 403
    assert pairing.status(code, now=1001) == "waiting"
    assert pairing.claim("guess", "x", now=1001).status == 403
    assert pairing.status(code, now=1001) == "burned"
    # The right code is no good after the burn.
    assert pairing.claim(code, "x", now=1002).status == 410


def test_a_new_pair_replaces_the_waiting_code(_pairing_home):
    first = pairing.mint(now=1000)
    second = pairing.mint(now=1001)
    assert pairing.status(first, now=1002) == "replaced"
    assert pairing.claim(first, "x", now=1002).status == 403
    assert pairing.claim(second, "x", now=1002).status == 200


def test_withdraw_takes_back_only_its_own_code(_pairing_home):
    first = pairing.mint(now=1000)
    second = pairing.mint(now=1001)
    pairing.withdraw(first)
    assert pairing.status(second, now=1002) == "waiting"
    pairing.withdraw(second)
    assert pairing.claim(second, "x", now=1002).status == 410


def test_status_reports_the_claim_to_the_waiting_cli(_pairing_home):
    code = pairing.mint(now=1000)
    pairing.claim(code, "Pixel 9", "100.64.0.7", now=1001)
    got = pairing.status(code, now=1002)
    assert got["name"] == "Pixel 9" and got["from"] == "100.64.0.7" and got["device_id"].startswith("d_")


def test_bad_claims_say_nothing_about_the_waiting_code(_pairing_home):
    assert pairing.claim(None, "x").status == 400
    assert pairing.claim("x" * 129, "x").status == 400
    assert pairing.claim("anything", "x").status == 410


def test_names_cannot_smuggle_terminal_escapes():
    assert pairing.clean_name("\x1b[31mevil\x1b[0m‮") == "[31mevil[0m"
    assert pairing.clean_name("   ") == "phone"
    assert len(pairing.clean_name("n" * 500)) == pairing.MAX_NAME


def test_revoke_by_id_or_unique_name_and_the_token_dies(_pairing_home):
    tokens = {}
    for name in ("Pixel", "Galaxy", "Galaxy"):
        code = pairing.mint()
        out = pairing.claim(code, name)
        tokens.setdefault(name, []).append(out.body)
    verifier = pairing.DeviceTokens()
    assert verifier.verify(tokens["Pixel"][0]["token"])
    assert [d["name"] for d in pairing.revoke("pixel")] == ["Pixel"]
    assert not verifier.verify(tokens["Pixel"][0]["token"])
    with pytest.raises(ValueError):
        pairing.revoke("Galaxy")
    gone = pairing.revoke(tokens["Galaxy"][1]["device_id"])
    assert gone[0]["id"] == tokens["Galaxy"][1]["device_id"]
    assert verifier.verify(tokens["Galaxy"][0]["token"])
    with pytest.raises(LookupError):
        pairing.revoke("nope")


# ---- the routes ----------------------------------------------------------------------------

def _client(token="master"):
    return TestClient(TestServer(build_app(PluginConfig(token=token, default_platform="matrix"))))


@pytest.mark.asyncio
async def test_claim_route_pairs_and_the_device_token_opens_app_routes():
    code = pairing.mint()
    async with _client() as client:
        resp = await client.post("/keryx/pair/claim", json={"code": code, "name": "Pixel"})
        assert resp.status == 200
        assert resp.headers["Cache-Control"] == "no-store"
        token = (await resp.json())["token"]
        device = {"Authorization": f"Bearer {token}"}
        # Past the auth gate: the stream answers 400 for the missing chat_id, not 401.
        assert (await client.get("/keryx/stream", headers=device)).status == 400
        # The forwarders' door stays master-only.
        publish = {"platform": "matrix", "chat_id": "!r", "event": "delta", "text": "x"}
        assert (await client.post("/keryx/publish", json=publish, headers=device)).status == 401
        master = {"Authorization": "Bearer master"}
        assert (await client.post("/keryx/publish", json=publish, headers=master)).status == 200
        # Revoked: refused on the next request.
        pairing.revoke("Pixel")
        assert (await client.get("/keryx/stream", headers=device)).status == 401


@pytest.mark.asyncio
async def test_claim_route_refuses_a_wrong_code_and_bad_bodies():
    pairing.mint()
    async with _client() as client:
        assert (await client.post("/keryx/pair/claim", json={"code": "nope"})).status == 403
        assert (await client.post("/keryx/pair/claim", data="not json")).status == 400
        assert (await client.post("/keryx/pair/claim", json=["code"])).status == 400


@pytest.mark.asyncio
async def test_claim_route_wont_pair_a_link_without_a_key(monkeypatch):
    import keryx_stream.auth as auth

    monkeypatch.setattr(auth, "resolve_token", lambda: ("", ""))
    code = pairing.mint()
    async with _client(token="") as client:
        assert (await client.post("/keryx/pair/claim", json={"code": code})).status == 503
    assert pairing.status(code) == "waiting"


@pytest.mark.asyncio
async def test_health_advertises_pairing():
    async with _client() as client:
        assert "pair" in (await (await client.get("/keryx/health")).json())["features"]
