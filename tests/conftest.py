import sys
from pathlib import Path

# Make the repo root importable so `import keryx_stream` works under bare pytest.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
