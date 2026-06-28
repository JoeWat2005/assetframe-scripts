"""AssetFrame engine package.

Concern-grouped subpackages (pipeline / scheduler / analytics / delivery / coordination), several of
them split a second level deep into role subgroups (e.g. pipeline/marketdata, pipeline/authoring,
scheduler/run, analytics/store). The modules keep their flat intra-package imports (`import sessions`,
`from taxonomy import ...`); this package init adds the scripts/ root + EVERY (nested) subpackage dir
to sys.path so those imports resolve regardless of how deeply a module now lives. Driven off
__init__.py presence, so a new subgroup is picked up here with no edit. Every entrypoint runs as
`python -m scripts.<pkg>[.<subgroup>].<module>` from the repo root, which imports this package first —
applying the shim before any module body runs.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# scripts/ root (for _paths.py) + every directory that is a package (has an __init__.py), at any depth.
_dirs = [str(_HERE)] + [str(p.parent) for p in _HERE.rglob("__init__.py")]
for _p in _dirs:
    if _p not in sys.path:
        sys.path.insert(0, _p)
