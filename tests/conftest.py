"""Pytest bootstrap for the relocated test suite: put the repo root on sys.path and import the
engine package so its subpackage sys.path shim runs, making the modules' flat imports resolvable."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # tests/ -> repo root
sys.path.insert(0, str(ROOT))
import scripts  # noqa: F401  (side-effect: applies the subpackage sys.path shim)
