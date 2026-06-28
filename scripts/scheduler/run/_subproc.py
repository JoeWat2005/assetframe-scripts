"""Subprocess + small env/JSON utilities for the daily-run orchestrator (extracted from run_daily).

_run/_run_rc launch a child engine script with the repo root as cwd and never raise; _parse_last_json
robustly reads the last TOP-LEVEL JSON object a child printed; _envint is a bounded int env read."""
import json
import os
import subprocess
import sys

from _paths import ROOT


def _envint(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _run(cmd, timeout=180):
    """Run a child script; return (ok, stdout, stderr). Never raises."""
    ok, _rc, out, err = _run_rc(cmd, timeout=timeout)
    return ok, out, err


def _run_rc(cmd, timeout=180):
    """Like _run but also returns the exit CODE — needed by the critic, which signals
    its verdict via the exit code (0 approve/revise, 2 reject/stand_aside) as well as
    JSON. Returns (ok, returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run([sys.executable] + cmd, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return False, -1, "", f"timeout after {timeout}s"
    except Exception as ex:
        return False, -1, "", str(ex)[:200]


def _parse_last_json(text):
    """Best-effort: parse the last TOP-LEVEL JSON object printed on a child's stdout.

    Must NOT use rfind('{'): the critic prints its verdict pretty-printed (indent=1), so the last
    '{' is a NESTED object (e.g. "_telemetry") — slicing from there yields a sub-object that parses
    fine but lacks "decision", silently dropping a good verdict to needs_brief. Instead scan for each
    top-level '{', raw_decode it (which consumes the whole object incl. its nested braces), and
    return the last one that decoded — robust to both pretty-printed and single-line output."""
    if not text:
        return {}
    dec = json.JSONDecoder()
    out, idx, n = {}, 0, len(text)
    while True:
        b = text.find("{", idx)
        if b == -1:
            break
        try:
            obj, end = dec.raw_decode(text, b)
            if isinstance(obj, dict):
                out = obj
            idx = max(end, b + 1)
        except json.JSONDecodeError:
            idx = b + 1
    return out
