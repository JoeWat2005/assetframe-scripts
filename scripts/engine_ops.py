"""engine_ops.py — the shared DB + run layer for the OCI engine runner.

The Oracle Cloud VM has NO inbound ports. It coordinates with the AssetFrame web
app ONLY through three Neon tables: it POLLS + WRITES, the web app READS + ENQUEUES.

    generation_requests  — the admin "Engine console" enqueues a manual scoped run;
                           the poller claims + runs it and writes status/run_id/error.
    engine_runs          — one row per run (schedule or manual): status, results,
                           errors, log tail. The console reads this for live status.
    engine_state         — singleton (id=1): automation_paused, last_heartbeat_at,
                           current_run_id. The heartbeat is how the console knows the
                           VM is "online".

This module is the single place that touches those tables. poller.py and
scheduled_run.py call into it. Everything here is defensive: run_and_record never
raises — it captures failures and records them on the run/request rows.

Concurrency: run_daily.py is heavy and must never run twice at once (the daily timer
and the manual poller are separate processes). run_and_record serialises every run
behind a filesystem lock (flock on POSIX) at ROOT/.run.lock.

Cancellation is co-operative: the web app sets generation_requests.cancel_requested
(or, mid-run, the row is polled); run_and_record terminates the run_daily subprocess
and records status 'cancelled'.

DATABASE_URL is read from the environment, falling back to the engine's .env file
(same loader contract as sync-db.mjs). A missing DATABASE_URL raises a clear
ConfigError, not a stack trace.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
RUN_DAILY = SCRIPTS / "run_daily.py"
LOCK_PATH = ROOT / ".run.lock"          # serialises run_daily across timer + poller
LOG_EXCERPT_BYTES = 8 * 1024            # last ~8KB of combined stdout/stderr
RUN_TIMEOUT = int(os.environ.get("ASSETFRAME_RUN_TIMEOUT", "5400"))   # 90 min hard cap
CANCEL_POLL_SECONDS = 5                 # how often we check cancel_requested mid-run


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


# --------------------------------------------------------------- Upstash (wake signal)
# The poller's idle loop heartbeats + checks for queued work via Upstash Redis (REST), NOT Neon,
# so the Neon compute can auto-suspend and we stay inside the free-tier compute-hours. Neon is only
# touched when there is real work (a wake flag the web sets on enqueue) or on a periodic safety
# sweep. GRACEFUL: with no Upstash env these return None/False and the poller falls back to polling
# Neon every tick (the original behaviour) — so this is inert until UPSTASH_* is set on the box.
HEARTBEAT_KEY = "af:engine:heartbeat"
WAKE_KEY = "af:engine:wake"
HEARTBEAT_TTL = 180  # seconds; matches the web console's online window


def _upstash_creds():
    _load_dotenv_into_environ()
    url = (os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("UPSTASH_KV_REST_API_URL")
           or os.environ.get("KV_REST_API_URL"))
    token = (os.environ.get("UPSTASH_REDIS_REST_TOKEN")
             or os.environ.get("UPSTASH_KV_REST_API_TOKEN")
             or os.environ.get("KV_REST_API_TOKEN"))
    if url and token:
        return url.rstrip("/"), token
    return None, None


def upstash_enabled():
    """True when Upstash REST creds are configured (else the poller stays on Neon polling)."""
    return _upstash_creds()[0] is not None


def _upstash(command):
    """Run one Upstash REST command (e.g. ["SET","k","v"]). Returns the result, or None on any
    error / when Upstash isn't configured. Never raises — a wake-signal blip must not crash the loop."""
    url, token = _upstash_creds()
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, data=json.dumps(command).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except Exception:
        return None


def heartbeat_upstash():
    """Write the engine heartbeat to Upstash with a TTL (expires if the poller dies)."""
    return _upstash(["SET", HEARTBEAT_KEY, _utcnow().isoformat(), "EX", str(HEARTBEAT_TTL)])


def wake_pending():
    """True if the web flagged that a generation request is waiting (set on enqueue)."""
    return bool(_upstash(["GET", WAKE_KEY]))


def clear_wake():
    """Clear the wake flag — call it right before draining Neon so a request enqueued mid-drain
    re-sets the flag and is picked up next tick (no lost requests)."""
    _upstash(["DEL", WAKE_KEY])


def signal_wake():
    """Set the wake flag (the web does this on enqueue; also handy for tests / manual pokes)."""
    return _upstash(["SET", WAKE_KEY, "1", "EX", "3600"])


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


# --------------------------------------------------------------- scope -> args
def scope_to_run_args(scope):
    """Map a request scope (jsonb) to run_daily.py CLI args.

      {"all_due": true}            -> ["--mode", "production"]   (the full due batch)
      {"assets": ["aapl","btc"]}   -> ["--mode", "production", "--asset", "aapl",
                                       "--asset", "btc"]          (scoped, repeated --asset)

    Asset ids are lowercased to match config/assets.json ids. Unknown / empty scope
    falls back to the full production batch (safe default — scoring + due assets).
    """
    if isinstance(scope, str):
        try:
            scope = json.loads(scope)
        except Exception:
            scope = {}
    scope = scope or {}
    assets = scope.get("assets")
    if assets:
        args = ["--mode", "production"]
        for a in assets:
            if a is None:
                continue
            args += ["--asset", str(a).strip().lower()]
        return args
    # {"all_due": true} or anything else -> the scheduled-style full batch.
    return ["--mode", "production"]


# --------------------------------------------------------------- manifest parse
def _read_run_manifest():
    """Find the most recently written runs/<date>/run_manifest.json and return it.

    run_daily writes runs/<London-date>/run_manifest.json. We don't know the date the
    child chose (London vs UTC edge), so pick the newest manifest by mtime. Returns
    (manifest_dict_or_None, path_or_None)."""
    runs_dir = ROOT / "runs"
    if not runs_dir.is_dir():
        return None, None
    manifests = sorted(runs_dir.glob("*/run_manifest.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return None, None
    try:
        return json.loads(manifests[0].read_text(encoding="utf-8-sig")), manifests[0]
    except Exception:
        return None, manifests[0]


def summarize_manifest(manifest):
    """Reduce a run_manifest into the compact results jsonb stored on engine_runs.

    Captures the headline counts plus a per-asset status summary (asset_id, ticker,
    status, report_id) so the console can show what happened without the full manifest.
    """
    if not isinstance(manifest, dict):
        return {}
    jobs = manifest.get("jobs") or []
    per_asset = [{"asset_id": j.get("asset_id"), "ticker": j.get("ticker"),
                  "status": j.get("status"), "report_id": j.get("report_id")}
                 for j in jobs]
    out = {
        "run_id": manifest.get("run_id"),
        "mode": manifest.get("mode"),
        "run_date": manifest.get("run_date"),
        "assets_selected": manifest.get("assets_selected"),
        "assets_due": manifest.get("assets_due"),
        "generated": manifest.get("generated"),
        "needs_brief": manifest.get("needs_brief"),
        "brief_rejected": manifest.get("brief_rejected"),
        "brief_stand_aside": manifest.get("brief_stand_aside"),
        "assets": per_asset,
    }
    if manifest.get("score") is not None:
        s = manifest["score"]
        out["score"] = {"scored": len(s.get("scored", [])),
                        "skipped": len(s.get("skipped", [])),
                        "errors": len(s.get("errors", []))}
    if manifest.get("token_cost") is not None:
        out["token_cost"] = manifest["token_cost"]
    # bubble up any per-asset errors so failures are visible in the summary.
    errs = [{"ticker": j.get("ticker"), "errors": j.get("errors")}
            for j in jobs if j.get("errors")]
    if errs:
        out["job_errors"] = errs
    return out


# --------------------------------------------------------------------- locking
class _FileLock:
    """A best-effort cross-process exclusive lock at LOCK_PATH.

    POSIX (the OCI VM): fcntl.flock — released automatically if the process dies.
    Windows (dev/test): msvcrt.locking — good enough for local structural runs.
    blocking=False raises Locked if the lock is already held (a concurrent run)."""

    class Locked(Exception):
        pass

    def __init__(self, path=LOCK_PATH, blocking=False, timeout=0):
        self.path = Path(path)
        self.blocking = blocking
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            import fcntl   # POSIX
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            if self.blocking and self.timeout:
                deadline = time.time() + self.timeout
                while True:
                    try:
                        fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() >= deadline:
                            raise self.Locked("run lock held (timeout)")
                        time.sleep(0.5)
            else:
                try:
                    fcntl.flock(self._fh, flags)
                except OSError:
                    raise self.Locked("another run holds the lock")
        except ImportError:
            import msvcrt   # Windows
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise self.Locked("another run holds the lock")
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()} at={_utcnow().isoformat()}\n")
            self._fh.flush()
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            try:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except ImportError:
                import msvcrt
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
        return False


# ------------------------------------------------------------- run + record
def _new_run_id(trigger, request_id):
    """Deterministic-ish run id: req-<reqid> for manual, daily-<UTC date> for schedule."""
    if request_id:
        return f"req-{request_id}"
    if trigger == "schedule":
        return f"daily-{_utcnow().strftime('%Y-%m-%d')}"
    return f"manual-{_utcnow().strftime('%Y%m%dT%H%M%SZ')}"


def _tail(text, nbytes=LOG_EXCERPT_BYTES):
    if not text:
        return ""
    b = text.encode("utf-8", "replace")
    if len(b) <= nbytes:
        return text
    return b[-nbytes:].decode("utf-8", "replace")


def _publish_chain(conn, request_id):
    """After a successful generate, PUBLISH the run: export_content.py -> publish.py (R2)
    -> sync-db.mjs (Neon). run_daily only writes reports/ LOCALLY; this chain is what gets
    the editions to R2 and the database (hidden, awaiting admin approval). Order matters —
    export writes content/, publish uploads R2, sync writes Neon LAST so the DB never
    references an R2 object that isn't uploaded yet. sync-db's own guard refuses empty
    content (the wipe foot-gun) and the Phase-2 hidden-on-insert keeps approval_required
    editions hidden. Honours cancellation between steps. cwd=ROOT so export's `--web .`
    and sync-db both resolve to <repo>/content. Returns (ok, errors, log_tail)."""
    steps = [
        ("export", [sys.executable, str(SCRIPTS / "export_content.py")]),
        ("publish", [sys.executable, str(SCRIPTS / "publish.py")]),
        ("sync", ["node", str(SCRIPTS / "sync-db.mjs")]),
    ]
    logs = []
    for name, cmd in steps:
        if request_id and is_cancel_requested(conn, request_id):
            return False, f"cancelled before {name}", "\n".join(logs)
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        except Exception as ex:
            return False, f"{name} failed to launch: {ex}"[:400], "\n".join(logs)
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        logs.append(f"=== {name} (rc={p.returncode}) ===\n{_tail(out, 2048)}")
        if p.returncode != 0:
            return False, f"{name} exited {p.returncode}: {_tail(out, 240)}"[:400], "\n".join(logs)
    return True, None, "\n".join(logs)


def run_and_record(conn, trigger, scope, request_id=None):
    """Run run_daily.py for `scope` under the run lock; record everything to Neon.

    Steps:
      1. INSERT an engine_runs row (status 'running'); set engine_state.current_run_id.
      2. Acquire the run lock (ROOT/.run.lock). If already held -> record 'failed'
         (concurrent run) and return — never block the poller forever.
      3. subprocess run_daily.py <scope args>, streaming combined output to a buffer,
         polling generation_requests.cancel_requested every few seconds; on cancel,
         terminate (then kill) the child -> status 'cancelled'.
      4. Parse the newest run_manifest.json -> results jsonb.
      5. UPDATE engine_runs (status/results/errors/log_excerpt/finished_at), the
         generation_requests row if request_id (status/run_id/error/finished_at), and
         clear engine_state.current_run_id.

    Never raises. Returns the run id. trigger in ('schedule','manual'); status in
    ('done','failed','cancelled').
    """
    run_id = _new_run_id(trigger, request_id)
    scope_json = scope if isinstance(scope, (dict, list)) else (scope or {})
    args = scope_to_run_args(scope_json)

    # 1. create the run row + claim current_run_id (best-effort; never fatal).
    try:
        conn.execute(
            "INSERT INTO engine_runs (id, trigger, scope, status, started_at) "
            "VALUES (%s, %s, %s, 'running', now()) "
            "ON CONFLICT (id) DO UPDATE SET trigger = excluded.trigger, "
            "  scope = excluded.scope, status = 'running', started_at = now(), "
            "  results = NULL, errors = NULL, log_excerpt = NULL, finished_at = NULL",
            (run_id, trigger, json.dumps(scope_json)))
        set_current_run(conn, run_id)
    except Exception as ex:
        # If we can't even create the row, still try to mark the request failed.
        _finish_request(conn, request_id, "failed", run_id, f"could not start run: {ex}"[:500])
        return run_id

    status = "failed"
    errors = None
    results = {}
    log_excerpt = ""
    try:
        with _FileLock(LOCK_PATH, blocking=False) as _lk:   # noqa: F841
            status, results, errors, log_excerpt = _exec_run_daily(conn, args, request_id)
            # run_daily only GENERATES locally. On success, publish inside the SAME lock so
            # generate+publish is one atomic unit: export -> R2 -> Neon (hidden). Without
            # this the editions would never reach R2/the database for admin approval.
            if status == "done":
                pub_ok, pub_err, pub_log = _publish_chain(conn, request_id)
                log_excerpt = _tail((log_excerpt or "") + "\n\n" + pub_log)
                results = {**(results or {}), "publish": "ok" if pub_ok else "failed"}
                if not pub_ok:
                    status, errors = "failed", pub_err
    except _FileLock.Locked:
        status, errors = "failed", "another run is already in progress (lock held)"
        log_excerpt = errors
    except Exception as ex:                       # absolute backstop — never raise
        status, errors = "failed", f"run_and_record error: {ex}"[:500]
        log_excerpt = errors

    # 5. record the outcome.
    try:
        conn.execute(
            "UPDATE engine_runs SET status = %s, results = %s, errors = %s, "
            "  log_excerpt = %s, finished_at = now() WHERE id = %s",
            (status, json.dumps(results) if results else None, errors,
             log_excerpt or None, run_id))
    except Exception:
        pass
    _finish_request(conn, request_id, _request_status(status), run_id, errors)
    try:
        set_current_run(conn, None)
    except Exception:
        pass
    return run_id


def _exec_run_daily(conn, args, request_id):
    """Spawn run_daily.py, poll for cancellation, capture output + manifest.

    Returns (status, results, errors, log_excerpt). status in done|failed|cancelled.
    Output is captured by redirecting the child's stdout+stderr to a temp file we tail
    (so we don't deadlock on a full OS pipe during a long run)."""
    import tempfile
    cmd = [sys.executable, str(RUN_DAILY)] + args
    cancelled = False
    err_msg = None
    outbuf = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=outbuf,
                                stderr=subprocess.STDOUT, text=True)
    except Exception as ex:
        outbuf.close()
        return "failed", {}, f"could not launch run_daily.py: {ex}"[:500], ""

    start = time.time()
    last_cancel_check = 0.0
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            now = time.time()
            # hard timeout guard.
            if now - start > RUN_TIMEOUT:
                err_msg = f"run timed out after {RUN_TIMEOUT}s"
                _terminate(proc)
                break
            # co-operative cancel: poll the request row.
            if request_id and (now - last_cancel_check) >= CANCEL_POLL_SECONDS:
                last_cancel_check = now
                try:
                    if is_cancel_requested(conn, request_id):
                        cancelled = True
                        _terminate(proc)
                        break
                except Exception:
                    pass   # a transient DB hiccup must not crash the run loop
            time.sleep(0.5)
    finally:
        try:
            rc = proc.wait(timeout=30)
        except Exception:
            _terminate(proc, hard=True)
            rc = proc.poll()

    # read combined output tail.
    try:
        outbuf.seek(0)
        full = outbuf.read()
    except Exception:
        full = ""
    finally:
        try:
            outbuf.close()
        except Exception:
            pass
    log_excerpt = _tail(full)

    if cancelled:
        return "cancelled", {}, "cancelled by admin request", log_excerpt

    manifest, _path = _read_run_manifest()
    results = summarize_manifest(manifest) if manifest else {}

    if err_msg:                       # timeout (or explicit error)
        return "failed", results, err_msg, log_excerpt
    if rc == 0:
        return "done", results, None, log_excerpt
    # non-zero exit -> failed; surface the manifest's job errors if we have them.
    errors = f"run_daily.py exited {rc}"
    if results.get("job_errors"):
        errors += f"; {json.dumps(results['job_errors'])[:300]}"
    return "failed", results, errors[:500], log_excerpt


def _terminate(proc, hard=False):
    """Stop the child run_daily process (SIGTERM, then SIGKILL on POSIX)."""
    try:
        if hard:
            proc.kill()
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    except Exception:
        pass


def _request_status(run_status):
    """Map an engine_run status to the generation_requests status."""
    return {"done": "done", "failed": "failed", "cancelled": "cancelled"}.get(
        run_status, "failed")


def _finish_request(conn, request_id, status, run_id, error):
    """Close out the generation_requests row (no-op if this run wasn't request-driven)."""
    if not request_id:
        return
    try:
        conn.execute(
            "UPDATE generation_requests SET status = %s, run_id = %s, error = %s, "
            "  finished_at = now() WHERE id = %s",
            (status, run_id, (error or None) if error else None, request_id))
    except Exception:
        pass
