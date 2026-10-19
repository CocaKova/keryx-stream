import sys
from pathlib import Path

# Make the repo root importable so `import keryx_stream` works under bare pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _pairing_home(tmp_path, monkeypatch):
    """Every test pairs into its own temp Hermes home, never the real one."""
    import keryx_stream.pairing as pairing

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setattr(pairing, "_home", lambda: home)
    return home
