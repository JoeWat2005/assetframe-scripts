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


# --------------------------------------------------------------------------- EngineDB
class EngineDB:
    """The conn-first DB + run-state primitives, gathered behind one object that holds the Neon
    `conn`. The module-level functions below (heartbeat/is_paused/set_current_run/claim_next_request/
    is_cancel_requested/reap_stale_runs + the engine_commands claim/reap in commands.py) are now thin
    wrappers `EngineDB(conn).x()` around these methods, so every existing caller, the engine_ops
    façade, and the tests keep their exact signatures. NEW code can instead hold an EngineDB (own-mode
    via EngineDB.connect(), or borrow-mode around an existing conn) and call the methods directly.

    Modes:
      * borrow (owns=False, the default — what the wrappers use): the caller owns the conn; __exit__
        NEVER closes it. The poller relies on this (a fresh conn per tick that must not be closed
        out from under it).
      * own (owns=True, via EngineDB.connect()): the EngineDB opened the conn and closes it on
        __exit__, so new code can do `with EngineDB.connect() as edb: ...`.
    """

    def __init__(self, conn, *, owns=False):
        self.conn = conn
        self._owns = owns

    @classmethod
    def connect(cls):
        """Open a fresh own-mode EngineDB (closes its conn on __exit__). For new code only — the
        wrappers construct EngineDB(conn) in borrow-mode so they never close the caller's conn."""
        return cls(connect(), owns=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._owns:
            try:
                self.conn.close()
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------ engine_state
    def heartbeat(self):
        """Stamp the singleton so the admin console flips the VM to 'online'."""
        self.conn.execute(
            "UPDATE engine_state SET last_heartbeat_at = now(), updated_at = now() WHERE id = 1")

    def is_paused(self):
        """True when automation is paused (the daily timer respects this; manual runs do not)."""
        row = self.conn.execute(
            "SELECT automation_paused FROM engine_state WHERE id = 1").fetchone()
        return bool(row and row.get("automation_paused"))

    def set_current_run(self, run_id):
        """Set (or clear, with run_id=None) engine_state.current_run_id."""
        self.conn.execute(
            "UPDATE engine_state SET current_run_id = %s, updated_at = now() WHERE id = 1",
            (run_id,))

    # ------------------------------------------------------------ generation_requests
    def is_cancel_requested(self, request_id):
        """True if the web app has flagged this request for cancellation mid-run."""
        if not request_id:
            return False
        row = self.conn.execute(
            "SELECT cancel_requested FROM generation_requests WHERE id = %s",
            (request_id,)).fetchone()
        return bool(row and row.get("cancel_requested"))

    def reap_stale_runs(self, max_age_s=None):
        """Reap ORPHANED engine_runs — mark any row left 'running' LONGER than max_age_s as 'failed'.
        A run's outcome-write happens IN its own process (run_and_record / run_backtest_batch), so if
        that process is SIGKILLed mid-run — a deploy restart (systemctl's TimeoutStopSec), an OOM, the
        systemd ceiling, a host reboot — the row freezes at 'running'/finished_at=NULL forever (the
        symptom: a run stuck 'running' a day later). Called at poller startup AND every Neon pass, so
        an orphan self-heals within one tick with NO manual restart.

        AGE-BASED (default RUN_TIMEOUT + 10-min grace) is the safety: the daily run is a SEPARATE
        oneshot process that ALSO takes the run lock, so a blanket 'WHERE status=running' sweep would
        wrongly fail a legitimately in-flight oneshot. The engine's own RUN_TIMEOUT records every
        healthy run's outcome by RUN_TIMEOUT seconds, so anything STILL 'running' past RUN_TIMEOUT+grace
        is provably dead and safe to sweep — while a 30-min in-flight batch is left alone. Also clears a
        now-stale current_run_id. Best-effort; a missing table is a no-op."""
        if max_age_s is None:
            # RUN_TIMEOUT bounds GENERATION; a healthy run then publishes (export+R2+sync, ~3x900s).
            # Give 1h of grace so the full generate+publish lifetime is always inside the threshold and
            # a slow (but alive) run is never swept — only a genuinely orphaned one is.
            max_age_s = RUN_TIMEOUT + 3600
        try:
            self.conn.execute(
                "UPDATE engine_runs SET status = 'failed', "
                "  errors = coalesce(errors, 'orphaned (process killed mid-run before recording outcome)'), "
                "  finished_at = now() "
                "WHERE status = 'running' AND started_at < now() - make_interval(secs => %s)",
                (int(max_age_s),))
        except Exception:
            pass
        # Clear engine_state.current_run_id only if it no longer points at a row that is STILL
        # 'running' (so a live run's banner is never cleared out from under it).
        try:
            self.conn.execute(
                "UPDATE engine_state SET current_run_id = NULL "
                "WHERE current_run_id IS NOT NULL AND current_run_id NOT IN "
                "  (SELECT id FROM engine_runs WHERE status = 'running')")
        except Exception:
            pass

    # -------------------------------------------------------------- two-phase claim
    def claim_next_request(self):
        """Atomically claim the oldest queued generation_request, or None.

        - Any queued row with cancel_requested=true is drained to 'cancelled' (finished_at=now())
          WITHOUT running — an admin cancelled it before it started.
        - Otherwise the oldest queued, non-cancelled row is flipped to 'running' (started_at=now())
          under SELECT ... FOR UPDATE SKIP LOCKED, so two pollers (or a retry) never claim the same
          row. Returns the claimed row dict, or None."""
        return self._claim_next("generation_requests")

    def claim_next_command(self):
        """Atomically claim the oldest queued engine_command, or None. Same two-phase claim as
        claim_next_request, but the engine_commands table may not be migrated yet (1750000020000),
        so an UndefinedTable is swallowed to a quiet None (no log spam)."""
        return self._claim_next("engine_commands", cancel_short_circuit=True)

    def reap_stale_commands(self):
        """Called once on poller startup: mark any engine_commands left 'running' by a PREVIOUS
        process (a restart command whose outcome-write was lost, or a crash) as 'failed', so the admin
        console never shows a phantom 'running' command forever (claim_next_command only ever re-claims
        'queued', so a stale 'running' row is otherwise never reconciled). Best-effort; a missing table
        is a no-op."""
        try:
            self.conn.execute(
                "UPDATE engine_commands SET status = 'failed', "
                "  result = coalesce(result, 'interrupted (poller restarted)'), finished_at = now() "
                "WHERE status = 'running'")
        except Exception:
            pass

    def _claim_next(self, table, *, cancel_short_circuit=False):
        """Unified two-phase FOR UPDATE SKIP LOCKED claim shared by claim_next_request +
        claim_next_command. Phase 1 drains queued+cancelled rows to 'cancelled'; phase 2 flips the
        oldest queued, non-cancelled row to 'running' and RETURNS it (or None). `table` selects the
        queue (generation_requests / engine_commands) and is interpolated into the SQL verbatim so
        each query stays byte-identical to its original.

        `cancel_short_circuit` is the engine_commands variant (claim_next_command): it omits the
        phase-1 `RETURNING id`/fetchall echo and GUARDS the whole pair with a `psycopg.errors.
        UndefinedTable -> None` (the engine_commands migration may not be applied yet). The
        generation_requests variant (cancel_short_circuit=False) keeps the `RETURNING id` drain echo
        and lets an UndefinedTable propagate, exactly as before."""
        try:
            # 1. drain queued+cancelled rows first (cheap; no run).
            with self.conn.transaction():
                cur = self.conn.execute(
                    f"UPDATE {table} SET status = 'cancelled', finished_at = now() "
                    f"WHERE id IN (SELECT id FROM {table} "
                    f"             WHERE status = 'queued' AND cancel_requested = true "
                    f"             FOR UPDATE SKIP LOCKED)"
                    + ("" if cancel_short_circuit else " RETURNING id"))
                if not cancel_short_circuit:
                    cur.fetchall()

            # 2. claim the oldest runnable queued row.
            with self.conn.transaction():
                row = self.conn.execute(
                    f"UPDATE {table} SET status = 'running', started_at = now() "
                    f"WHERE id = (SELECT id FROM {table} "
                    f"            WHERE status = 'queued' AND cancel_requested = false "
                    f"            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                    f"RETURNING *").fetchone()
            return row
        except psycopg.errors.UndefinedTable:
            if cancel_short_circuit:
                return None   # table not migrated yet — nothing to do (no log spam)
            raise


class OpsContext:
    """Injection shell for new/future control-server code: bundles an EngineDB (and, later, other
    coordination services) so a handler can take one `ops` argument instead of a raw conn. Purely an
    injection target — no existing caller is retrofitted onto it."""

    def __init__(self, db):
        self.db = db


# ---------------------------------------------------------------- engine_state (wrappers)
# Thin conn-first wrappers — SAME signatures as before, so every caller, the engine_ops façade, and
# the tests are untouched. Each constructs a borrow-mode EngineDB (owns=False) that NEVER closes the
# caller's conn, and delegates to the matching method above.
def heartbeat(conn):
    return EngineDB(conn).heartbeat()


def is_paused(conn):
    return EngineDB(conn).is_paused()


def set_current_run(conn, run_id):
    return EngineDB(conn).set_current_run(run_id)


def claim_next_request(conn):
    return EngineDB(conn).claim_next_request()


def is_cancel_requested(conn, request_id):
    return EngineDB(conn).is_cancel_requested(request_id)


def reap_stale_runs(conn, max_age_s=None):
    return EngineDB(conn).reap_stale_runs(max_age_s)


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
