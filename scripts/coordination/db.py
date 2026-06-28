"""db.py — engine DB + run-state foundation (split out of engine_ops). Conn-first SQL primitives +
config/env seed + RUN_TIMEOUT. Imports NO coordination sibling."""
import os, sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from _paths import ROOT, SCRIPTS

# Seed the non-secret runtime knobs from config/engine.json BEFORE the import-time RUN_TIMEOUT read
# below (env wins, so systemd EnvironmentFile still overrides). This is also what the admin
# "set config" command writes, so a changed knob takes effect on the next poller restart.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
try:
    import config_loader as _cfg
    _cfg.apply_runtime_env(ROOT / "config" / "engine.json")
except Exception:
    pass


def _int_env(name, default):
    """Parse an int env var, falling back to `default` on a missing/garbage value. A bad value must
    NEVER raise at import time: that would crash the poller before main() runs, and systemd
    Restart=always would re-read the same bad .env and crash-LOOP it — unrecoverable on a VM with
    no inbound ports. (set_config also validates such keys, but this is the last line of defence.)"""
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


RUN_TIMEOUT = _int_env("ASSETFRAME_RUN_TIMEOUT", 5400)   # 90 min hard cap (garbage -> default)


class ConfigError(Exception):
    """Raised when required configuration (e.g. DATABASE_URL) is absent."""


# --------------------------------------------------------------------------- env
def _load_dotenv_into_environ():
    """Populate os.environ from ROOT/.env for any key not already set. Mirrors the
    sync-db.mjs loader so the Python engine reads the same file. Best-effort."""
    envp = ROOT / ".env"
    try:
        for line in envp.read_text(encoding="utf-8").splitlines():
            t = line.strip()
            if not t or t.startswith("#") or "=" not in t:
                continue
            k, _, v = t.partition("=")
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def database_url():
    """Resolve DATABASE_URL from the environment / .env. Clear error if missing."""
    _load_dotenv_into_environ()
    url = (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
           or os.environ.get("STORAGE_DATABASE_URL") or os.environ.get("STORAGE_URL"))
    if not url:
        raise ConfigError(
            "DATABASE_URL is not set. Add it to the environment or the engine .env "
            "(the prod Neon URL). The OCI runner cannot reach Neon without it.")
    return url


def connect():
    """Open a psycopg3 connection to the prod Neon database (autocommit).

    autocommit=True keeps each statement atomic, which is what heartbeat / claim /
    state-update want. claim_next_request manages its own explicit transaction for
    the SELECT ... FOR UPDATE SKIP LOCKED / UPDATE pair.
    """
    return psycopg.connect(database_url(), autocommit=True, row_factory=dict_row)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- engine_state
def heartbeat(conn):
    """Stamp the singleton so the admin console flips the VM to 'online'."""
    conn.execute(
        "UPDATE engine_state SET last_heartbeat_at = now(), updated_at = now() WHERE id = 1")


def is_paused(conn):
    """True when automation is paused (the daily timer respects this; manual runs do not)."""
    row = conn.execute(
        "SELECT automation_paused FROM engine_state WHERE id = 1").fetchone()
    return bool(row and row.get("automation_paused"))


def set_current_run(conn, run_id):
    """Set (or clear, with run_id=None) engine_state.current_run_id."""
    conn.execute(
        "UPDATE engine_state SET current_run_id = %s, updated_at = now() WHERE id = 1",
        (run_id,))


# ------------------------------------------------------------ generation_requests
def claim_next_request(conn):
    """Atomically claim the oldest queued request, or None.

    - Any queued row with cancel_requested=true is short-circuited to 'cancelled'
      (finished_at=now()) WITHOUT running — an admin cancelled it before it started.
    - Otherwise the oldest queued, non-cancelled row is flipped to 'running'
      (started_at=now()) under SELECT ... FOR UPDATE SKIP LOCKED, so two pollers (or a
      retry) never claim the same row. Returns the claimed row dict, or None.
    """
    # 1. drain queued+cancelled rows first (cheap; no run).
    with conn.transaction():
        cur = conn.execute(
            "UPDATE generation_requests SET status = 'cancelled', finished_at = now() "
            "WHERE id IN (SELECT id FROM generation_requests "
            "             WHERE status = 'queued' AND cancel_requested = true "
            "             FOR UPDATE SKIP LOCKED) "
            "RETURNING id")
        cur.fetchall()

    # 2. claim the oldest runnable queued row.
    with conn.transaction():
        row = conn.execute(
            "UPDATE generation_requests SET status = 'running', started_at = now() "
            "WHERE id = (SELECT id FROM generation_requests "
            "            WHERE status = 'queued' AND cancel_requested = false "
            "            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
            "RETURNING *").fetchone()
    return row


def is_cancel_requested(conn, request_id):
    """True if the web app has flagged this request for cancellation mid-run."""
    if not request_id:
        return False
    row = conn.execute(
        "SELECT cancel_requested FROM generation_requests WHERE id = %s",
        (request_id,)).fetchone()
    return bool(row and row.get("cancel_requested"))


def reap_stale_runs(conn, max_age_s=None):
    """Reap ORPHANED engine_runs — mark any row left 'running' LONGER than max_age_s as 'failed'. A
    run's outcome-write happens IN its own process (run_and_record / run_backtest_batch), so if that
    process is SIGKILLed mid-run — a deploy restart (systemctl's TimeoutStopSec), an OOM, the systemd
    ceiling, a host reboot — the row freezes at 'running'/finished_at=NULL forever (the symptom: a run
    stuck 'running' a day later). Called at poller startup AND every Neon pass, so an orphan self-heals
    within one tick with NO manual restart.

    AGE-BASED (default RUN_TIMEOUT + 10-min grace) is the safety: the daily run is a SEPARATE oneshot
    process that ALSO takes the run lock, so a blanket 'WHERE status=running' sweep would wrongly fail
    a legitimately in-flight oneshot. The engine's own RUN_TIMEOUT records every healthy run's outcome
    by RUN_TIMEOUT seconds, so anything STILL 'running' past RUN_TIMEOUT+grace is provably dead and safe
    to sweep — while a 30-min in-flight batch is left alone. Also clears a now-stale current_run_id.
    Best-effort; a missing table is a no-op."""
    if max_age_s is None:
        # RUN_TIMEOUT bounds GENERATION; a healthy run then publishes (export+R2+sync, ~3x900s). Give
        # 1h of grace so the full generate+publish lifetime is always inside the threshold and a slow
        # (but alive) run is never swept — only a genuinely orphaned one is.
        max_age_s = RUN_TIMEOUT + 3600
    try:
        conn.execute(
            "UPDATE engine_runs SET status = 'failed', "
            "  errors = coalesce(errors, 'orphaned (process killed mid-run before recording outcome)'), "
            "  finished_at = now() "
            "WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)",
            (int(max_age_s),))
    except Exception:
        pass
    # Clear engine_state.current_run_id only if it no longer points at a row that is STILL 'running'
    # (so a live run's banner is never cleared out from under it).
    try:
        conn.execute(
            "UPDATE engine_state SET current_run_id = NULL "
            "WHERE current_run_id IS NOT NULL AND current_run_id NOT IN "
            "  (SELECT id FROM engine_runs WHERE status = 'running')")
    except Exception:
        pass


def _empty_dir(path):
    """Remove every child of `path` (files -> unlink, dirs -> rmtree); no-op if `path` isn't a dir.
    Per-child errors are swallowed. Returns True iff `path` was a directory (so callers can record
    which dirs they actually cleared). Shared by runner._wipe_sandbox_state + commands clear handlers."""
    import shutil
    if not path.is_dir():
        return False
    for child in path.iterdir():
        try:
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink()
        except Exception:
            pass
    return True
