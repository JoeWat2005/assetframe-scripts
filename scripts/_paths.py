"""Single source of truth for engine filesystem anchors (depth-independent), so a module can move
between subpackages without recomputing parent.parent. Data paths stay CWD-relative to ROOT (every
run uses cwd = repo root)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # scripts/_paths.py -> scripts/ -> repo root
SCRIPTS = ROOT / "scripts"
