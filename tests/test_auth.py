"""Bearer-token resolution (auth.py) and the forward-mode path that presents it.

The lookup order is env → the process home's .env → profile secret scope → the
routed profile's .env, and nothing is ever written back to os.environ."""
import os

import pytest

import keryx_stream
import keryx_stream.auth as auth
from keryx_stream import PluginConfig, register


@pytest.fixture
def clean_env(monkeypatch):
    for name in auth.TOKEN_VARS:
        monkeypatch.delenv(name, raising=False)
    # Default: no scope, no routed profile; each test opts in.
    monkeypatch.setattr(auth, "_scoped", lambda name: "")
    monkeypatch.setattr(auth, "_current_home", lambda: None)
    return monkeypatch


def _home(tmp_path, name, body):
    home = tmp_path / name
    home.mkdir()
    (home / ".env").write_text(body)
    return home


def _fake_env_reader(monkeypatch):
    """Parse KEY=VALUE files without Hermes (the unit CI job has no tree)."""
    def read(home):
        path = os.path.join(str(home), ".env")
        if not home or not os.path.exists(path):
            return {}
        out = {}
        for line in open(path):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        return out
    monkeypatch.setattr(auth, "_env_file_values", read)


def test_env_wins_and_keryx_token_beats_api_key(clean_env, tmp_path):
    clean_env.setenv("API_SERVER_KEY", "api")
    clean_env.setenv("KERYX_STREAM_TOKEN", "kx")
    clean_env.setattr(auth, "_process_home", lambda: _home(tmp_path, "h", "API_SERVER_KEY=file\n"))
    assert auth.resolve_token() == ("kx", "env:KERYX_STREAM_TOKEN")
    clean_env.delenv("KERYX_STREAM_TOKEN")
    assert auth.resolve_token() == ("api", "env:API_SERVER_KEY")


def test_own_home_dotenv_when_env_is_scrubbed(clean_env, tmp_path):
    _fake_env_reader(clean_env)
    clean_env.setattr(auth, "_process_home", lambda: _home(tmp_path, "h", "API_SERVER_KEY=fromfile\n"))
    clean_env.setattr(auth, "_scoped", lambda name: "fromscope")
    before = dict(os.environ)
    assert auth.resolve_token() == ("fromfile", "dotenv:API_SERVER_KEY")
    assert dict(os.environ) == before  # nothing leaked into the process env


def test_secret_scope_when_home_has_no_key(clean_env, tmp_path):
    _fake_env_reader(clean_env)
    clean_env.setattr(auth, "_process_home", lambda: _home(tmp_path, "h", "OTHER=1\n"))
    clean_env.setattr(auth, "_scoped", lambda name: "scoped" if name == "API_SERVER_KEY" else "")
    assert auth.resolve_token() == ("scoped", "scope:API_SERVER_KEY")


def test_routed_profile_dotenv_last(clean_env, tmp_path):
    _fake_env_reader(clean_env)
    clean_env.setattr(auth, "_process_home", lambda: _home(tmp_path, "h", ""))
    routed = _home(tmp_path, "profile", "KERYX_STREAM_TOKEN=routed\n")
    clean_env.setattr(auth, "_current_home", lambda: routed)
    before = dict(os.environ)
    assert auth.resolve_token() == ("routed", "profile-dotenv:KERYX_STREAM_TOKEN")
    assert dict(os.environ) == before


def test_routed_profile_equal_to_process_home_is_not_reread(clean_env, tmp_path):
    home = _home(tmp_path, "h", "")
    reads = []
    clean_env.setattr(auth, "_process_home", lambda: home)
    clean_env.setattr(auth, "_current_home", lambda: home)
    clean_env.setattr(auth, "_env_file_values", lambda h: reads.append(h) or {})
    assert auth.resolve_token() == ("", "")
    assert reads == [home]


def test_nothing_anywhere_is_empty(clean_env, tmp_path):
    _fake_env_reader(clean_env)
    clean_env.setattr(auth, "_process_home", lambda: tmp_path / "missing")
    assert auth.resolve_token() == ("", "")


def test_real_hermes_dotenv_reader(clean_env, tmp_path):
    """With a Hermes tree installed, the .env is read through Hermes' own
    tokenizer (quotes, comments, export)."""
    pytest.importorskip("agent.secret_scope")
    home = _home(tmp_path, "h", '# comment\nexport API_SERVER_KEY="quoted-key"  # trailing\n')
    try:
        from agent.secret_scope import invalidate_env_file_cache
        invalidate_env_file_cache()
    except Exception:
        pass
    clean_env.setattr(auth, "_process_home", lambda: home)
    before = dict(os.environ)
    assert auth.resolve_token() == ("quoted-key", "dotenv:API_SERVER_KEY")
    assert dict(os.environ) == before


# --- forward mode presents the resolved token ---------------------------------

def test_forward_mode_hands_the_resolved_token_to_the_forwarder(clean_env, tmp_path):
    """Scrubbed worker: no env token, key only in the home's .env → load_config
    finds it and the forwarder is built with it."""
    import sys
    import types

    _fake_env_reader(clean_env)
    clean_env.setattr(auth, "_process_home", lambda: _home(tmp_path, "h", "API_SERVER_KEY=worker-key\n"))
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_config = lambda: {"keryx_stream": {"port": 8999}}  # type: ignore[attr-defined]
    clean_env.setitem(sys.modules, "hermes_cli.config", fake_config)
    clean_env.setattr(keryx_stream, "_start_sse_server", lambda cfg: None)  # port taken
    created = []

    class FakeForwarder:
        def __init__(self, url, token, resolve=None):
            created.append((url, token, resolve))
            self.publish = lambda *a: None

    import keryx_stream.forwarder as fwd
    clean_env.setattr(fwd, "Forwarder", FakeForwarder)

    class Ctx:
        def register_hook(self, name, cb):
            pass

    register(Ctx())
    url, token, resolve = created[0]
    assert url == "http://127.0.0.1:8999/keryx/publish"
    assert token == "worker-key"
    assert resolve is auth.resolve_token
    assert "API_SERVER_KEY" not in os.environ


def test_forwarder_without_token_resolves_late_and_sends_bearer(monkeypatch):
    """A worker whose credentials were unreachable at start picks the token up
    on its next post (rate-limited), and the POST carries it."""
    import json

    import keryx_stream.forwarder as mod

    calls = []
    fwd = mod.Forwarder("http://127.0.0.1:1/keryx/publish", "", resolve=lambda: calls.append(1) or ("late", "scope:API_SERVER_KEY"))
    posts = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=None: posts.append((dict(req.headers), json.loads(req.data))) or Resp())
    try:
        fwd._post("tui", "s1", "delta", "hi")
        fwd._post("tui", "s1", "delta", "again")
    finally:
        fwd.close()
    assert posts[0][0]["Authorization"] == "Bearer late"
    assert posts[1][0]["Authorization"] == "Bearer late"
    assert calls == [1]  # resolved once, then cached


def test_forwarder_retry_is_rate_limited_while_nothing_resolves(monkeypatch):
    import keryx_stream.forwarder as mod

    calls = []
    fwd = mod.Forwarder("http://127.0.0.1:1/keryx/publish", "", resolve=lambda: calls.append(1) or ("", ""))
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda req, timeout=None: (_ for _ in ()).throw(OSError("x")))
    try:
        for _ in range(5):
            fwd._post("tui", "s1", "delta", "x")
    finally:
        fwd.close()
    assert calls == [1]


# --- hooks the host lacks are skipped, not registered -------------------------

def test_register_skips_hooks_the_host_does_not_list(monkeypatch):
    """An older Hermes (the 0.21.3 tree) has no pre/post_auxiliary_call: those
    are skipped, the rest register, and the probe never counts them."""
    from keryx_stream.probe import probe

    probe.reset()
    monkeypatch.setattr(keryx_stream, "load_config", lambda: PluginConfig(enabled=True, token="t"))
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: object())
    monkeypatch.setattr(keryx_stream, "_server_thread", type("T", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(keryx_stream, "_wants_thinking", lambda cfg: False)
    all_hooks = set(keryx_stream._make_hook_callbacks(PluginConfig(), lambda *a: None, background=False))
    host = all_hooks - {"pre_auxiliary_call", "post_auxiliary_call"}
    real = keryx_stream._valid_names
    monkeypatch.setattr(keryx_stream, "_valid_names",
                        lambda mod, attr: set(host) if attr == "VALID_HOOKS" else real(mod, attr))
    registered = []

    class Ctx:
        def register_hook(self, name, cb):
            registered.append(name)

    try:
        register(Ctx())
        assert set(registered) == host
        assert not probe.has("pre_auxiliary_call") and not probe.has("post_auxiliary_call")
        assert probe.has("post_llm_call")
        assert "status" not in probe.stream_features()
    finally:
        probe.reset()


def test_register_survives_a_host_that_raises_on_one_hook(monkeypatch):
    from keryx_stream.probe import probe

    probe.reset()
    monkeypatch.setattr(keryx_stream, "load_config", lambda: PluginConfig(enabled=True, token="t"))
    monkeypatch.setattr(keryx_stream, "_start_sse_server", lambda cfg: object())
    monkeypatch.setattr(keryx_stream, "_server_thread", type("T", (), {"is_alive": lambda self: True})())
    monkeypatch.setattr(keryx_stream, "_wants_thinking", lambda cfg: False)
    monkeypatch.setattr(keryx_stream, "_valid_names", lambda mod, attr: None)
    registered = []

    class Ctx:
        def register_hook(self, name, cb):
            if name == "subagent_start":
                raise ValueError("unknown hook")
            registered.append(name)

    try:
        register(Ctx())
        assert "subagent_start" not in registered and "post_llm_call" in registered
        assert not probe.has("subagent_start")
    finally:
        probe.reset()
