"""Update channel: GET reads (never writes), PUT switches through Hermes' own functions.

Hermes is stubbed with a small fake of the surface update_channel._hermes() returns, so
the plugin-owned logic is pinned without a real install; the last tests check the names
it needs against the real hermes-agent tree (and the real import, where Hermes runs).
"""
import ast
import os
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from keryx_stream import PluginConfig
from keryx_stream import update_channel as uc_mod
from keryx_stream.server import build_app

AUTH = {"Authorization": "Bearer t"}
SHA16 = "a4f3b2c1d0e9f8a7"


class FakeHermes:
    """The update-channel surface, with a config dict standing in for config.yaml."""

    def __init__(self, *, default="stable", record=None, stamp=None, method="git", commit_build=False,
                 checks=None, set_error=None):
        self.config = {"update": {"installs": {SHA16: record}}} if record else {}
        self._default, self._stamp, self._method = default, stamp or {}, method
        self._commit_build, self._checks, self._set_error = commit_build, checks or {}, set_error
        self.writes, self.check_calls, self.locked = [], [], False
        uc = SimpleNamespace(
            resolve_update_channel=lambda cfg, root: self._record().get("channel") or self._default,
            channel_record=lambda cfg, root: self._record(),
            default_channel=lambda root: self._default,
            rides_default_channel=lambda rec, ch, root: rec.get("channel") is None and ch == self._default,
            set_install_channel=self._set,
            install_id=lambda root: SHA16,
            _channel_write_lock=self._lock,
            _read_stamp=lambda root: self._stamp,
            _package_channel=lambda stamp: stamp.get("payload") in ("bundled", "light", "runtime"),
        )
        self.ns = SimpleNamespace(
            uc=uc, detect_install_method=lambda root: self._method,
            get_config_path=lambda: Path("/hermes-home/config.yaml"),
            get_project_root=lambda: Path("/hermes-home/hermes-agent"),
            read_config=lambda path: self.config, check_for_updates=self._check,
            is_commit_build=lambda root: self._commit_build,
            commit_build_message="This is an app build; it updates with the app.",
            yaml_update=self._yaml_update,
        )

    def _record(self):
        return ((self.config.get("update") or {}).get("installs") or {}).get(SHA16) or {}

    def _set(self, channel, root):
        if self._set_error:
            raise ValueError(self._set_error)
        self.writes.append(("set", channel))
        self.config.setdefault("update", {}).setdefault("installs", {})[SHA16] = {"path": str(root),
                                                                                  "channel": channel}
        return SHA16

    @contextmanager
    def _lock(self, path):
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def _yaml_update(self, path, key, value):
        assert self.locked, "the record must change under Hermes' channel lock"
        assert key == f"update.installs.{SHA16}" and value is None
        self.writes.append(("forget", key))
        self.config["update"]["installs"].pop(SHA16)

    def _check(self, *, channel, cache_path, force=False):
        assert "hermes-home" not in str(cache_path), "GET must not touch Hermes' own check cache"
        self.check_calls.append((channel, force))
        return dict(self._checks.get(channel, {}))


@pytest.fixture
def fake(monkeypatch, tmp_path):
    def install(**kw):
        f = FakeHermes(**kw)
        monkeypatch.setattr(uc_mod, "_hermes", lambda: f.ns)
        monkeypatch.setattr(uc_mod, "_cache_path", lambda ch: tmp_path / f"{ch}.json")
        return f
    return install


AHEAD = {"stable": {"sourceVersion": "0.22.4", "behind": 0, "aheadOfRelease": True, "rollback": False}}


def test_status_rides_the_default_forward_only(fake):
    f = fake(checks=AHEAD)
    out = uc_mod.status()
    assert out == {"channel": "stable", "default": "stable", "forward_only": True, "can_set": True,
                   "stable_version": "0.22.4", "behind": 0, "ahead_of_stable": True,
                   "message": "This build is newer than stable v0.22.4. You'll get the next stable release."}
    assert f.writes == [] and f.check_calls == [("stable", False)]


def test_status_explicit_stable_says_it_moves_back(fake):
    fake(record={"channel": "stable"},
         checks={"stable": {"sourceVersion": "0.22.4", "behind": -1, "rollback": True}})
    out = uc_mod.status()
    assert out["forward_only"] is False and out["ahead_of_stable"] is True and out["behind"] == -1
    assert "moves it back" in out["message"]


def test_status_stable_up_to_date_and_available(fake):
    fake(checks={"stable": {"sourceVersion": "0.22.4", "behind": 0}})
    assert uc_mod.status()["message"] == "Stable v0.22.4 · up to date."
    fake(checks={"stable": {"sourceVersion": "0.22.5", "behind": -1}})
    out = uc_mod.status()
    assert out["message"] == "Stable v0.22.5 is available." and out["ahead_of_stable"] is False


def test_status_edge_counts_new_changes(fake):
    f = fake(record={"channel": "main"},
             checks={"stable": {"sourceVersion": "0.22.4", "behind": -1, "rollback": True},
                     "main": {"branch": "main", "behind": 2}})
    out = uc_mod.status(force=True)
    assert (out["channel"], out["behind"], out["ahead_of_stable"]) == ("main", 2, True)
    assert out["message"] == "Edge · 2 new changes."
    assert f.check_calls == [("stable", True), ("main", True)]


def test_status_reports_a_failed_check(fake):
    fake(checks={"stable": {"error": "release-unavailable", "message": "Could not resolve the stable channel"}})
    out = uc_mod.status()
    assert out["behind"] is None and out["message"] == "Could not resolve the stable channel"


def test_status_offline_skips_the_lookups(fake):
    f = fake(checks=AHEAD)
    out = uc_mod.status(offline=True)
    assert f.check_calls == [] and out["stable_version"] is None and out["message"] is None


@pytest.mark.parametrize("kw, why", [
    ({"commit_build": True}, "app build"),
    ({"stamp": {"payload": "bundled"}}, "owned by"),
    ({"stamp": {"updateMechanism": "external", "distribution": "the Docker image"}}, "the Docker image"),
    ({"method": "pip"}, "installed with pip"),
])
def test_not_settable_installs(fake, kw, why):
    f = fake(**kw)
    out = uc_mod.status()
    assert out["can_set"] is False and why in out["message"] and f.check_calls == []
    with pytest.raises(uc_mod.NotSettable, match=why):
        uc_mod.switch("main", False)
    assert f.writes == []


def test_switch_to_edge_saves_main(fake):
    f = fake()
    uc_mod.switch("main", False)
    assert f.writes == [("set", "main")] and f._record()["channel"] == "main"


def test_switch_to_stable_and_stay_forgets_the_record(fake):
    f = fake(record={"path": "/x", "channel": "main"})
    uc_mod.switch("stable", True)
    assert f.writes == [("forget", f"update.installs.{SHA16}")] and f._record() == {}
    assert uc_mod.status(offline=True)["forward_only"] is True


def test_switch_to_stable_without_stay_saves_it(fake):
    f = fake(record={"path": "/x", "channel": "main"})
    uc_mod.switch("stable", False)
    assert f.writes == [("set", "stable")]
    assert uc_mod.status(offline=True)["forward_only"] is False


def test_stay_needs_stable_to_be_the_default(fake):
    f = fake(default="main")
    with pytest.raises(uc_mod.NotSettable, match="explicitly"):
        uc_mod.switch("stable", True)
    assert f.writes == []


def test_bad_channel_and_hermes_refusals(fake):
    fake()
    with pytest.raises(uc_mod.BadChannel):
        uc_mod.switch("canary", False)
    fake(set_error="channels don't apply here")
    with pytest.raises(uc_mod.NotSettable, match="don't apply"):
        uc_mod.switch("main", False)


# ---- routes ---------------------------------------------------------------------------------

def _client():
    return TestClient(TestServer(build_app(PluginConfig(token="t", default_platform="matrix"))))


@pytest.mark.asyncio
async def test_routes_need_auth(fake):
    fake()
    async with _client() as client:
        assert (await client.get("/keryx/update/channel")).status == 401
        assert (await client.put("/keryx/update/channel", json={"channel": "main"})).status == 401


@pytest.mark.asyncio
async def test_get_and_put_round_trip(fake):
    f = fake(checks=AHEAD)
    async with _client() as client:
        resp = await client.get("/keryx/update/channel", headers=AUTH)
        assert resp.status == 200
        body = await resp.json()
        assert set(body) == {"channel", "default", "forward_only", "can_set", "stable_version", "behind",
                             "ahead_of_stable", "message"}
        assert body["channel"] == "stable" and f.writes == []
        resp = await client.put("/keryx/update/channel", headers=AUTH, json={"channel": "main", "stay": False})
        assert resp.status == 200 and (await resp.json())["channel"] == "main"
        resp = await client.put("/keryx/update/channel", headers=AUTH, json={"channel": "stable", "stay": True})
        assert resp.status == 200
        body = await resp.json()
        assert body["channel"] == "stable" and body["forward_only"] is True


@pytest.mark.asyncio
async def test_put_errors(fake):
    fake(method="docker")
    async with _client() as client:
        resp = await client.put("/keryx/update/channel", headers=AUTH, json={"channel": "edge"})
        assert resp.status == 400 and await resp.json() == {"error": "bad_channel"}
        resp = await client.put("/keryx/update/channel", headers=AUTH, data="not json")
        assert resp.status == 400
        resp = await client.put("/keryx/update/channel", headers=AUTH, json={"channel": "main"})
        assert resp.status == 409
        body = await resp.json()
        assert body["error"] == "not_settable" and "docker" in body["message"]


@pytest.mark.asyncio
async def test_slow_lookup_answers_with_the_channel_alone(fake, monkeypatch):
    import time

    import keryx_stream.server as server

    f = fake(checks=AHEAD)
    real = f.ns.check_for_updates
    f.ns.check_for_updates = lambda **kw: (time.sleep(0.5), real(**kw))[1]
    monkeypatch.setattr(server, "UPDATE_CHECK_TIMEOUT_S", 0.05)
    async with _client() as client:
        body = await (await client.get("/keryx/update/channel", headers=AUTH)).json()
    assert body["channel"] == "stable" and body["stable_version"] is None
    assert body["message"].startswith("Couldn't reach the update source in time")


@pytest.mark.asyncio
async def test_old_hermes_has_no_route_and_no_feature(monkeypatch):
    monkeypatch.setattr(uc_mod, "_hermes", lambda: None)
    async with _client() as client:
        resp = await client.get("/keryx/update/channel", headers=AUTH)
        assert resp.status == 404 and (await resp.json())["error"] == "unsupported"
        assert (await client.put("/keryx/update/channel", headers=AUTH, json={"channel": "main"})).status == 404
        assert "update_channel" not in (await (await client.get("/keryx/health")).json())["features"]


@pytest.mark.asyncio
async def test_health_advertises_the_feature_when_supported(fake):
    fake()
    async with _client() as client:
        assert "update_channel" in (await (await client.get("/keryx/health")).json())["features"]


# ---- against the real Hermes tree --------------------------------------------------------------

HERMES_ROOT = Path(os.environ.get("HERMES_AGENT_ROOT") or Path.home() / ".hermes" / "hermes-agent")
NEEDED = {
    "hermes_cli/update_channel.py": {"resolve_update_channel", "channel_record", "default_channel",
                                     "rides_default_channel", "set_install_channel", "install_id",
                                     "_channel_write_lock", "_read_stamp", "_package_channel"},
    "hermes_cli/config.py": {"detect_install_method", "get_config_path", "get_project_root",
                             "require_readable_config_before_write"},
    "hermes_cli/source_check.py": {"check_for_updates"},
    "hermes_cli/update_contract.py": {"COMMIT_BUILD_UPDATE_MESSAGE", "is_commit_build"},
    "utils.py": {"atomic_roundtrip_yaml_update"},
}


@pytest.mark.skipif(not (HERMES_ROOT / "hermes_cli").is_dir(), reason="no hermes-agent tree")
def test_every_name_it_needs_exists_in_the_hermes_tree():
    """Read, not imported, so it runs where Hermes' dependencies are not installed (same idea as
    test_hermes_compat, which can't see `utils` or the private channel helpers)."""
    missing = []
    for rel, names in NEEDED.items():
        tree = ast.parse((HERMES_ROOT / rel).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
        defined |= {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                    if isinstance(t, ast.Name)}
        missing += [f"{rel}:{name}" for name in names - defined]
    assert not missing, f"Hermes moved what update_channel.py calls: {missing}"


def test_real_import_resolves_where_hermes_runs():
    try:
        import hermes_cli.update_channel  # noqa: F401
    except Exception:
        pytest.skip("Hermes is not importable here")
    assert uc_mod.supported()
