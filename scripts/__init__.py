"""AssetFrame engine package.

Concern-grouped subpackages (pipeline / scheduler / analytics / delivery / coordination). The modules
keep their flat intra-package imports (`import sessions`, `from taxonomy import ...`); this package
init adds each subpackage dir + the scripts/ root to sys.path so those imports resolve regardless of
which subpackage a module now lives in. Every entrypoint runs as `python -m scripts.<pkg>.<module>`
from the repo root, which imports this package first — applying the shim before any module body runs.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _sub in ("pipeline", "scheduler", "analytics", "delivery", "coordination"):
    _p = str(_HERE / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
if str(_HERE) not in sys.path:          # scripts/ root, for _paths.py
    sys.path.insert(0, str(_HERE))
