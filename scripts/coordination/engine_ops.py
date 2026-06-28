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
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from _paths import ROOT, SCRIPTS         # repo-root anchors (the scripts/__init__ shim is on sys.path)
RUN_DAILY = "scripts.scheduler.run_daily"          # spawned as `python -m <module>` (cwd = ROOT)
SYNC_BACKTEST = "scripts.analytics.sync_backtest"  # pushes ledger/sim -> Neon backtest_results
from locking import _FileLock, LOCK_PATH   # run lock lives in locking.py now; re-exported here
# The sandbox working trees a backtest writes to (cleared by clear_sandbox; never the live trees).
SANDBOX_DIRS = ["ledger/sim", "data/predictions/sim", "reports/sim",
                "data/briefs/sim", "data/research/sim", "data/social/sim"]
MAX_BACKTEST_DAYS = 90                   # up to ~3 months back; clamp so a typo can't fan out forever
LOG_EXCERPT_BYTES = 24 * 1024           # last ~24KB of combined stdout/stderr (richer dashboard log)

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
# UPSTASH_KEY_PREFIX namespaces a 2nd environment (e.g. "dev:") onto the SAME Upstash DB so the
# dev + prod pollers don't collide on heartbeat/wake; unset = prod (no prefix). The web must set
# the SAME prefix on its matching environment (Vercel Preview = dev).
KEY_PREFIX = os.environ.get("UPSTASH_KEY_PREFIX", "")
HEARTBEAT_KEY = f"{KEY_PREFIX}af:engine:heartbeat"
WAKE_KEY = f"{KEY_PREFIX}af:engine:wake"
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


# --- background heartbeat daemon --------------------------------------------
# A long run_and_record() (a multi-minute run_daily / backtest subprocess) BLOCKS the poller's
# single-threaded loop, so the in-loop heartbeat never fires and the box flips to OFFLINE for the
# whole run. This daemon keeps the Upstash heartbeat (the web's primary online signal) fresh every
# few seconds INDEPENDENTLY of the blocking run. Upstash-only (no Neon connection sharing across
# threads); best-effort (never raises). The in-loop heartbeat(conn)/heartbeat_upstash() calls stay
# as a top-up that also refreshes the Neon heartbeat between runs.
_HB_THREAD = None
_HB_STOP = None


def start_heartbeat_daemon(interval=10):
    """Start (idempotently) a daemon thread that calls heartbeat_upstash() every `interval` seconds
    until stop_heartbeat_daemon(). Call once at poller startup."""
    global _HB_THREAD, _HB_STOP
    if _HB_THREAD is not None and _HB_THREAD.is_alive():
        return
    _HB_STOP = threading.Event()
    stop = _HB_STOP

    def _run():
        while True:
            try:
                heartbeat_upstash()
            except Exception:
                pass
            if stop.wait(interval):     # returns True once stop is set -> exit
                return

    _HB_THREAD = threading.Thread(target=_run, name="assetframe-heartbeat", daemon=True)
    _HB_THREAD.start()


def stop_heartbeat_daemon():
    """Signal the heartbeat daemon to stop (best-effort; the thread is a daemon so it never blocks exit)."""
    if _HB_STOP is not None:
        _HB_STOP.set()


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
    args = ["--mode", "production"]
    for a in (scope.get("assets") or []):
        if a is None:
            continue
        args += ["--asset", str(a).strip().lower()]
    # Optional BACKDATE: generate a report AS-OF a past time so its prediction window has already
    # closed — lets you test scoring / the ledger immediately instead of waiting for the window.
    # Validated to run_daily's exact "YYYY-MM-DD HH:MM" format (a bad value is ignored, never passed).
    as_of = scope.get("as_of")
    if isinstance(as_of, str) and as_of.strip():
        try:
            datetime.strptime(as_of.strip()[:16], "%Y-%m-%d %H:%M")
            args += ["--as-of", as_of.strip()[:16]]
        except ValueError:
            pass
    return args


# --------------------------------------------------------------- manifest parse
def _read_run_manifest(since=None):
    """Find the most recently written runs/<date>/run_manifest.json and return it.

    run_daily writes runs/<London-date>/run_manifest.json. We don't know the date the
    child chose (London vs UTC edge), so pick the newest manifest by mtime. If `since` (epoch
    seconds) is given, a newest manifest OLDER than it means THIS run died before writing its own
    manifest -> return (None, None) rather than the PREVIOUS run's manifest (which would report the
    wrong success counts for the failed run). Returns (manifest_dict_or_None, path_or_None)."""
    runs_dir = ROOT / "runs"
    if not runs_dir.is_dir():
        return None, None
    manifests = sorted(runs_dir.glob("*/run_manifest.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    if not manifests:
        return None, None
    newest = manifests[0]
    if since is not None and newest.stat().st_mtime < since:
        return None, None
    try:
        return json.loads(newest.read_text(encoding="utf-8-sig")), newest
    except Exception:
        return None, newest


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
        out["score"] = {"scored": len(s.get("scored") or []),
                        "skipped": len(s.get("skipped") or []),
                        "errors": len(s.get("errors") or [])}
    if manifest.get("token_cost") is not None:
        out["token_cost"] = manifest["token_cost"]
    # bubble up any per-asset errors so failures are visible in the summary.
    errs = [{"ticker": j.get("ticker"), "errors": j.get("errors")}
            for j in jobs if j.get("errors")]
    if errs:
        out["job_errors"] = errs
    return out


# --------------------------------------------------------------------- locking
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
    # publish (R2 upload) is NON-FATAL: a transient single-file R2 failure must NOT skip the Neon
    # sync — the web reads editions/scored_results from Neon, and the R2 files can be re-pushed later
    # with "Re-publish reports". export + sync are fatal (the sync is what makes a run visible).
    steps = [
        ("export", [sys.executable, "-m", "scripts.delivery.export_content"], True),
        ("publish", [sys.executable, "-m", "scripts.delivery.publish"], False),
        ("sync", ["node", str(SCRIPTS / "sync-db.mjs")], True),
    ]
    logs = []
    warn = None
    for name, cmd, fatal in steps:
        if request_id and is_cancel_requested(conn, request_id):
            return False, f"cancelled before {name}", "\n".join(logs)
        try:
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900)
        except Exception as ex:
            if fatal:
                return False, f"{name} failed to launch: {ex}"[:400], "\n".join(logs)
            warn = f"{name} failed to launch: {ex}"[:240]
            continue
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        logs.append(f"=== {name} (rc={p.returncode}) ===\n{_tail(out, 2048)}")
        if p.returncode != 0:
            if fatal:
                return False, f"{name} exited {p.returncode}: {_tail(out, 240)}"[:400], "\n".join(logs)
            warn = f"publish exited {p.returncode} — some R2 uploads failed; synced anyway, use Re-publish reports"[:240]
    return True, warn, "\n".join(logs)


def run_and_record(conn, trigger, scope, request_id=None, sandbox=False):
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

    sandbox=True runs an ISOLATED backtest: run_daily.py is invoked with --sandbox (so it
    sets ASSETFRAME_SANDBOX=1 for itself and every child — writes go to ledger/sim,
    data/predictions/sim, reports/sim, and the live calibration map is never rebuilt), and
    the publish chain (export/publish/sync) is SKIPPED entirely so nothing reaches R2/Neon
    editions. The run is tagged (results['sandbox']=True) so it is distinguishable in
    engine_runs.

    Never raises. Returns the run id. trigger in ('schedule','manual','backtest'); status in
    ('done','failed','cancelled').
    """
    run_id = _new_run_id(trigger, request_id)
    scope_json = scope if isinstance(scope, (dict, list)) else (scope or {})
    args = scope_to_run_args(scope_json)
    if sandbox:
        args = args + ["--sandbox"]

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
            # SANDBOX: a backtest must NOT touch published editions/R2/Neon, so we skip the
            # publish chain entirely and tag the run so it is distinguishable in engine_runs.
            if sandbox:
                results = {**(results or {}), "sandbox": True, "publish": "skipped (sandbox)"}
            elif status == "done" and ((results or {}).get("generated")
                                       or ((results or {}).get("score") or {}).get("scored")):
                # Publish when the run GENERATED a new edition OR SCORED a closed window. Scoring
                # mutates the ledger (a scored_results row) WITHOUT authoring an edition, and the public
                # track record reads scored_results from Neon — so a score-only / quiet day (markets
                # shut, only closed windows graded) must still run export + sync-db, or the scores are
                # stranded in the local CSV forever (the bug that kept the track record empty).
                pub_ok, pub_err, pub_log = _publish_chain(conn, request_id)
                log_excerpt = _tail((log_excerpt or "") + "\n\n" + pub_log)
                results = {**(results or {}), "publish": "ok" if pub_ok else "failed"}
                if not pub_ok:
                    status, errors = "failed", pub_err
            elif status == "done":
                # Nothing generated AND nothing scored — a true no-op. Skip the publish chain (sync-db
                # over empty content would otherwise trip its anti-wipe guard). Not a failure.
                results = {**(results or {}), "publish": "skipped (nothing generated or scored)"}
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


def _backdated_as_of(as_of, k):
    """Return the as_of moment moved BACK by k calendar days, same HH:MM, as 'YYYY-MM-DD HH:MM'.
    Day 0 is as_of itself; day k = as_of minus k days. The report_id embeds HHMM+date, so each
    day is a distinct backdated report (AF-YYYYMMDDHHMM-TICKER) and they never collide."""
    base = datetime.strptime(as_of.strip()[:16], "%Y-%m-%d %H:%M")
    return (base - timedelta(days=k)).strftime("%Y-%m-%d %H:%M")


def _wipe_sandbox_state(conn):
    """Reset ALL sandbox state so a backtest run starts FRESH: empties the sim/ working trees
    (ledger/sim, data/predictions/sim, reports/sim, data/{briefs,research,social}/sim) AND clears the
    Neon backtest_results / backtest_predictions tables. This stops leftover rows from a previous
    backtest (e.g. an already-scored window) bleeding into the new run. ONLY ever touches sandbox
    state — never the live ledger, editions, scored_results or reports. Best-effort; never raises."""
    import shutil
    for sub in SANDBOX_DIRS:
        d = ROOT / sub
        if not d.is_dir():
            continue
        for child in d.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except Exception:
                pass
    for tbl in ("backtest_predictions", "backtest_results"):
        try:
            conn.execute(f"DELETE FROM {tbl}")   # admin-only sandbox tables (never the live track record)
        except Exception:
            pass


def run_backtest_batch(conn, assets, as_of, days=1):
    """Run a MULTI-DAY sandbox backtest: for each of `days` consecutive days counting BACK from
    as_of (day 0 = as_of, day k = as_of - k days, SAME HH:MM), generate + score the given assets
    AS-OF that closed window — every day a distinct backdated report (report_id embeds HHMM+date,
    so days never collide). ALL days run under ONE run lock, all sandboxed (--sandbox, no publish,
    no live calibration). After every day completes, sync_backtest.py pushes ledger/sim ->
    backtest_results once. ONE engine_runs row (trigger 'backtest') summarises the whole batch.

    `days` is clamped to 1..MAX_BACKTEST_DAYS. The single-day path (days=1) runs exactly one day,
    so run_and_record's behaviour for an ordinary single-day backtest is preserved.

    Never raises. Returns the run id. Validation (>=1 asset, valid closed as_of) is the caller's
    (_cmd_run_backtest); this records 'failed' if those are somehow violated."""
    assets = [str(a).strip().lower() for a in (assets or []) if a is not None and str(a).strip()]
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 1
    days = max(1, min(MAX_BACKTEST_DAYS, days))

    run_id = f"backtest-{_utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    scope = {"assets": assets, "as_of": (as_of or "").strip()[:16], "days": days}

    # validate up front so a bad batch is recorded cleanly, not run.
    bad = None
    if not assets:
        bad = "run_backtest requires at least one asset"
    else:
        try:
            datetime.strptime(scope["as_of"], "%Y-%m-%d %H:%M")
        except ValueError:
            bad = f"run_backtest as_of {as_of!r} must be 'YYYY-MM-DD HH:MM'"

    try:
        conn.execute(
            "INSERT INTO engine_runs (id, trigger, scope, status, started_at) "
            "VALUES (%s, 'backtest', %s, 'running', now()) "
            "ON CONFLICT (id) DO UPDATE SET trigger = 'backtest', scope = excluded.scope, "
            "  status = 'running', started_at = now(), results = NULL, errors = NULL, "
            "  log_excerpt = NULL, finished_at = NULL",
            (run_id, json.dumps(scope)))
        set_current_run(conn, run_id)
    except Exception:
        return run_id   # can't even create the run row -> nothing else we can record to

    status = "failed"
    errors = bad
    log_excerpt = bad or ""
    day_results = []
    total_scored = 0
    if bad is None:
        try:
            with _FileLock(LOCK_PATH, blocking=False) as _lk:   # noqa: F841 — one lock for ALL days
                _wipe_sandbox_state(conn)   # each backtest run starts FRESH — clear the sim ledger +
                                            # Neon backtest tables so leftover rows never bleed in
                logs = []
                day_status = "done"
                for k in range(days):
                    day_as_of = _backdated_as_of(scope["as_of"], k)
                    day_scope = {"assets": assets, "as_of": day_as_of}
                    args = scope_to_run_args(day_scope) + ["--sandbox"]
                    st, res, err, log = _exec_run_daily(conn, args, None)
                    sc = (res or {}).get("score") or {}
                    scored_n = sc.get("scored") if isinstance(sc.get("scored"), int) else 0
                    total_scored += int(scored_n or 0)
                    day_results.append({
                        "day": k, "as_of": day_as_of, "status": st,
                        "generated": (res or {}).get("generated"),
                        "scored": scored_n, "errors": err})
                    logs.append(f"=== day {k} as_of={day_as_of} (status={st}) ===\n{log or ''}")
                    if st != "done":
                        day_status = st   # surface a failed/cancelled day but keep going through the batch
                # After ALL days: push ledger/sim -> Neon backtest_results once.
                sync_status, sync_log = _run_sync_backtest()
                logs.append(f"=== sync_backtest ===\n{sync_log}")
                status = day_status if day_status != "done" else ("done" if sync_status else "failed")
                if not sync_status:
                    errors = "sync_backtest failed (see log)"
                log_excerpt = _tail("\n\n".join(logs))
        except _FileLock.Locked:
            status, errors = "failed", "another run is already in progress (lock held)"
            log_excerpt = errors
        except Exception as ex:
            status, errors = "failed", f"run_backtest_batch error: {ex}"[:500]
            log_excerpt = errors

    results = {
        "sandbox": True, "publish": "skipped (sandbox)", "trigger": "backtest",
        "days": days, "assets": assets, "total_scored": total_scored,
        "day_runs": day_results,
    }
    try:
        conn.execute(
            "UPDATE engine_runs SET status = %s, results = %s, errors = %s, "
            "  log_excerpt = %s, finished_at = now() WHERE id = %s",
            (status, json.dumps(results), errors, log_excerpt or None, run_id))
    except Exception:
        pass
    try:
        set_current_run(conn, None)
    except Exception:
        pass
    return run_id


def _run_sync_backtest():
    """Run sync_backtest.py (ledger/sim -> Neon backtest_results) as a subprocess. Returns
    (ok: bool, log_tail: str). Best-effort: a sync failure is recorded but never raises."""
    try:
        p = subprocess.run([sys.executable, "-m", SYNC_BACKTEST], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=300)
    except Exception as ex:
        return False, f"sync_backtest failed to launch: {ex}"[:300]
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, _tail(out, 2000)


def _exec_run_daily(conn, args, request_id):
    """Spawn run_daily.py, poll for cancellation, capture output + manifest.

    Returns (status, results, errors, log_excerpt). status in done|failed|cancelled.
    Output is captured by redirecting the child's stdout+stderr to a temp file we tail
    (so we don't deadlock on a full OS pipe during a long run)."""
    import tempfile
    cmd = [sys.executable, "-m", RUN_DAILY] + args
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

    manifest, _path = _read_run_manifest(since=start)   # ignore a stale prior-run manifest
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


# ============================================================ engine_commands
# A SECOND web->box control channel (after generation_requests): the admin console enqueues an
# allow-listed COMMAND to control the box — restart the poller, pull latest code, re-run the
# publish chain (recovers a generate-that-failed-to-publish, e.g. boto3 missing), fetch logs, or
# set an allow-listed config value. The poller claims + runs them on its normal Neon cadence via
# claim_next_command + run_command, exactly like generation_requests. The allow-list is enforced
# HERE (the box NEVER runs an unknown verb), independently of whatever the web sends — that is the
# security boundary. Restart is a GRACEFUL SELF-EXIT, not sudo: NoNewPrivileges=true on the poller
# unit blocks the poller from escalating, so we record the command done and let systemd
# Restart=always relaunch the process (onto new code, if pull_latest ran first).

# Keys set_config may write to ROOT/.env — ONLY keys the engine actually consumes (every settable
# key is attack surface, so keep it minimal). Never secrets/credentials/URLs.
_SETTABLE_CONFIG_KEYS = {
    "ASSETFRAME_AUTHOR_BRIEFS", "ADVISOR_DATA_PROVIDER", "ASSETFRAME_RUN_TIMEOUT",
    "ASSETFRAME_BRIEF_MODEL", "ASSETFRAME_RETENTION_DAYS",
    "ASSETFRAME_BRIEF_BATCH", "ASSETFRAME_CRITIC_MODEL", "ASSETFRAME_BRIEF_CONCURRENCY",
    "ASSETFRAME_BRIEF_WEB_MAX_USES", "ASSETFRAME_DATA_LICENSE", "TWELVEDATA_RATE_PER_MIN",
}
# Per-key value validators — reject a value that would brick the box via an allow-listed key. In
# particular ASSETFRAME_RUN_TIMEOUT is int()-parsed at import; a non-integer would crash-loop the
# poller. A key with no validator only gets the generic single-line / length check.
_CONFIG_VALUE_VALIDATORS = {
    # Generation kill-timeout. Capped at 7200 so generate (<=RUN_TIMEOUT) + publish (up to 3x900s)
    # stays under the daily oneshot's systemd TimeoutStartSec=10800 — else systemd SIGKILLs mid-publish
    # and orphans the engine_runs row. 7200 still leaves the batch path its full budget at 4-5 assets.
    "ASSETFRAME_RUN_TIMEOUT": lambda v: v.isdigit() and 60 <= int(v) <= 7200,
    # Brief / critic model must be a Claude id (a typo here would break every brief). Allow-list shape.
    "ASSETFRAME_BRIEF_MODEL": lambda v: v.startswith("claude-") and 8 <= len(v) <= 60,
    "ASSETFRAME_CRITIC_MODEL": lambda v: v.startswith("claude-") and 8 <= len(v) <= 60,
    # Local reports/runs retention in days (0 = keep everything). Bounded so a typo can't be wild.
    "ASSETFRAME_RETENTION_DAYS": lambda v: v.isdigit() and 0 <= int(v) <= 3650,
    # Batch authoring toggle (1 = Message Batches path) + concurrent-brief cap on the sync path.
    "ASSETFRAME_BRIEF_BATCH": lambda v: v in ("0", "1"),
    "ASSETFRAME_BRIEF_CONCURRENCY": lambda v: v.isdigit() and 1 <= int(v) <= 16,
    # Web searches per news-on brief (input-cost dial). Bounded so a typo can't run away.
    "ASSETFRAME_BRIEF_WEB_MAX_USES": lambda v: v.isdigit() and 1 <= int(v) <= 15,
    # Data-license mode: commercial = only commercially-licensed feeds back a published report.
    "ASSETFRAME_DATA_LICENSE": lambda v: v in ("personal", "commercial"),
    # Active data feed. Allow-list closes the silent-fallback trap (a typo -> Yahoo with only a note).
    "ADVISOR_DATA_PROVIDER": lambda v: v in ("yahoo", "twelvedata", "eodhd", "coingecko"),
    # TwelveData requests/min pacing (0 = no throttle). Bounded so a typo can't disable pacing wildly.
    "TWELVEDATA_RATE_PER_MIN": lambda v: v.isdigit() and 0 <= int(v) <= 1000,
}
# tail_logs may only read these systemd units (prevents arbitrary -u injection).
_KNOWN_POLLER_UNITS = {"assetframe-poller.service", "assetframe-poller-dev.service"}


def claim_next_command(conn):
    """Atomically claim the oldest queued engine_command, or None. Mirrors claim_next_request:
    queued+cancel_requested rows are short-circuited to 'cancelled'; otherwise the oldest queued,
    non-cancelled row is flipped to 'running' under FOR UPDATE SKIP LOCKED so two pollers never
    claim the same row. Returns the claimed row dict, or None. Quietly returns None when the
    engine_commands table doesn't exist yet (migration 1750000020000 not applied)."""
    try:
        with conn.transaction():
            conn.execute(
                "UPDATE engine_commands SET status = 'cancelled', finished_at = now() "
                "WHERE id IN (SELECT id FROM engine_commands "
                "             WHERE status = 'queued' AND cancel_requested = true "
                "             FOR UPDATE SKIP LOCKED)")
        with conn.transaction():
            row = conn.execute(
                "UPDATE engine_commands SET status = 'running', started_at = now() "
                "WHERE id = (SELECT id FROM engine_commands "
                "            WHERE status = 'queued' AND cancel_requested = false "
                "            ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) "
                "RETURNING *").fetchone()
        return row
    except psycopg.errors.UndefinedTable:
        return None   # table not migrated yet — nothing to do (no log spam)


def reap_stale_commands(conn):
    """Called once on poller startup: mark any engine_commands left 'running' by a PREVIOUS process
    (a restart command whose outcome-write was lost, or a crash) as 'failed', so the admin console
    never shows a phantom 'running' command forever (claim_next_command only ever re-claims
    'queued', so a stale 'running' row is otherwise never reconciled). Best-effort; a missing table
    is a no-op."""
    try:
        conn.execute(
            "UPDATE engine_commands SET status = 'failed', "
            "  result = coalesce(result, 'interrupted (poller restarted)'), finished_at = now() "
            "WHERE status = 'running'")
    except Exception:
        pass


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


def run_command(conn, row):
    """Execute one claimed engine_command and record the outcome on the row. Never raises.

    Returns {status, result, restart}: status in done|failed; restart=True asks the poller to
    self-exit so systemd relaunches it (restart_poller + a successful pull_latest). The command
    name is dispatched through the allow-list — an unknown verb is recorded 'failed', never run."""
    cmd_id = row.get("id")
    command = (row.get("command") or "").strip()
    args = row.get("args") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        return _finish_command(conn, cmd_id, "failed", f"unknown command '{command}'", None, False)
    try:
        ok, result, log, restart = handler(conn, args)
        return _finish_command(conn, cmd_id, "done" if ok else "failed", result, log, bool(restart) and bool(ok))
    except Exception as ex:
        return _finish_command(conn, cmd_id, "failed", f"command error: {ex}"[:400], None, False)


def _finish_command(conn, cmd_id, status, result, log, restart):
    """Write the command outcome (status/result/log_excerpt/finished_at). Done BEFORE any restart
    self-exit, so a relaunch never finds the command stuck 'running'. Returns the dispatch result."""
    try:
        conn.execute(
            "UPDATE engine_commands SET status = %s, result = %s, log_excerpt = %s, "
            "  finished_at = now() WHERE id = %s",
            (status, (result or None), (_tail(log, 4096) if log else None), cmd_id))
    except Exception:
        pass
    return {"status": status, "result": result, "restart": bool(restart)}


# ---- handlers: each returns (ok: bool, result: str, log: str|None, restart: bool) -------------
def _cmd_restart_poller(conn, args):
    """Bounce the poller. We do NOT sudo (NoNewPrivileges blocks it); we record done and ask the
    poller to self-exit — systemd Restart=always (RestartSec=5) relaunches it within seconds."""
    return True, "restart requested — poller self-exits; systemd relaunches it", None, True


def _cmd_pull_latest(conn, args):
    """git fetch + git pull --ff-only + reinstall deps, then restart onto the new code. Mirrors the
    CI deploy minus the sudo systemctl (replaced by the self-exit). --ff-only is non-destructive —
    it refuses rather than rewrites if the tree diverged. Held under the run lock so it never pulls
    mid-generation."""
    steps = [
        ["git", "fetch", "--prune", "origin"],
        ["git", "pull", "--ff-only"],
        [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"],
        ["npm", "install", "--omit=dev", "--no-audit", "--no-fund"],
    ]
    logs = []
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            # Discard the box's local edits to git-tracked working files so the ff-only pull can't
            # fail on a dirty tree. config/assets.json is re-synced from Neon on restart; the ledger
            # is normally UNTRACKED (gitignored), so its checkout is a harmless no-op — it only bites
            # for the single historical deploy that crossed the ledger-untrack commit. Best-effort.
            for _path in ("config/assets.json", "ledger/outcome_ledger.csv"):
                try:
                    subprocess.run(["git", "checkout", "--", _path], cwd=str(ROOT),
                                   capture_output=True, text=True, timeout=60)
                except Exception:
                    pass
            for cmd in steps:
                try:
                    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
                except Exception as ex:
                    return False, f"{cmd[0]} failed to launch: {ex}"[:300], "\n".join(logs), False
                out = ((p.stdout or "") + (p.stderr or "")).strip()
                logs.append(f"$ {' '.join(cmd)} (rc={p.returncode})\n{_tail(out, 1500)}")
                if p.returncode != 0:
                    return False, f"{cmd[0]} exited {p.returncode}", "\n".join(logs), False
    except _FileLock.Locked:
        return False, "another run is in progress — retry pull_latest shortly", None, False
    return True, "pulled latest + reinstalled deps — restarting onto new code", "\n".join(logs), True


def _cmd_run_maintenance(conn, args):
    """Re-run the publish chain (export -> publish -> sync) WITHOUT generating. Recovers a run
    whose generation succeeded but publish/sync failed (e.g. boto3 missing, a transient R2/Neon
    blip) — the reports are already on disk; this just pushes them to R2 + Neon. Held under the
    run lock so it never collides with a generation or the daily timer."""
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            ok, err, log = _publish_chain(conn, None)
    except _FileLock.Locked:
        return False, "another run is in progress — retry run_maintenance shortly", None, False
    if ok:
        return True, "publish chain re-ran (export -> publish -> sync)", log, False
    return False, f"publish chain failed: {err}", log, False


def _cmd_tail_logs(conn, args):
    """Capture recent poller logs for the admin console. Best-effort journalctl for the poller
    unit (only the two known unit names are allowed); falls back to the most recent engine_runs
    log excerpts from Neon if journald isn't readable as this user."""
    try:
        lines = max(20, min(1000, int(args.get("lines"))))
    except (TypeError, ValueError):
        lines = 200
    unit = args.get("unit")
    if unit not in _KNOWN_POLLER_UNITS:
        unit = "assetframe-poller-dev.service" if "-dev" in str(ROOT) else "assetframe-poller.service"
    out = ""
    try:
        p = subprocess.run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and (p.stdout or "").strip():
            out = p.stdout
    except Exception:
        out = ""
    if not out.strip():
        try:
            rows = conn.execute(
                "SELECT id, status, started_at, errors, log_excerpt FROM engine_runs "
                "ORDER BY started_at DESC LIMIT 5").fetchall()
            parts = [f"=== run {r.get('id')} [{r.get('status')}] {r.get('started_at')} ===\n"
                     f"{(r.get('errors') or '')}\n{(r.get('log_excerpt') or '')}" for r in (rows or [])]
            out = "\n\n".join(parts) or "(no logs: journalctl unreadable and no engine_runs rows)"
        except Exception as ex:
            out = f"(could not read logs: {ex})"
    return True, f"captured {unit} logs ({lines} lines requested)", out, False


def _cmd_set_config(conn, args):
    """Set ONE allow-listed config key in config/engine.json — the single runtime-settings file
    (config_loader.apply_runtime_env seeds it into the environment at each entrypoint's startup, so
    it takes effect on the next restart). Tight allow-list; never writes secrets/credentials/URLs
    (those stay in .env)."""
    key = (args.get("key") or "").strip()
    if key not in _SETTABLE_CONFIG_KEYS:
        return False, f"key '{key}' is not settable (allowed: {sorted(_SETTABLE_CONFIG_KEYS)})", None, False
    value = "" if args.get("value") is None else str(args.get("value"))
    if "\n" in value or "\r" in value or len(value) > 200:
        return False, "value must be a single line of <= 200 chars", None, False
    validator = _CONFIG_VALUE_VALIDATORS.get(key)
    if validator and not validator(value):
        return False, f"value {value!r} is not valid for {key}", None, False
    cfgp = ROOT / "config" / "engine.json"
    try:
        data = json.loads(cfgp.read_text(encoding="utf-8-sig")) if cfgp.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception as ex:
        return False, f"could not read engine.json: {ex}"[:200], None, False
    data[key] = value
    try:
        # Atomic write (tmp + os.replace) so a crash mid-write can never truncate the settings file.
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        tmp = ROOT / "config" / "engine.json.tmp"
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, cfgp)
    except Exception as ex:
        return False, f"could not write engine.json: {ex}"[:200], None, False
    return True, f"set {key} in config/engine.json (restart the poller for it to take effect)", None, False


def _sync_assets_from_neon(conn):
    """Rebuild config/assets.json from Neon engine_assets (the dashboard's source of truth) — but
    ONLY after config_loader validates it. Atomic; a bad/empty universe NEVER replaces the good
    config, so the dashboard can't break generation. Returns (ok: bool, message: str). Called by the
    sync_assets command AND on poller startup, so after ANY deploy/restart the box's config reflects
    the dashboard (and the git-tracked config/assets.json default is just a bootstrap)."""
    base_sql = ("id, name, instrument, ticker, provider_symbols, asset_class, session_profile, "
                "cadence, timezone, roll_utc, related, forecast_window, publish_policy, report_tier, enabled")
    mt_sql = (", cadence_day, timeframes, include_fundamentals, include_news, fundamentals_source, "
              "chart_intervals")
    # Try the multi-timeframe columns; if the migration that adds them hasn't run on this box's DB
    # yet (deploy skew: new code + old DB), fall back to the base columns so the sync still works.
    try:
        rows = conn.execute(
            f"SELECT {base_sql}{mt_sql} FROM engine_assets ORDER BY sort_order, id").fetchall()
    except psycopg.errors.UndefinedTable:
        return False, "engine_assets not migrated yet"
    except psycopg.errors.UndefinedColumn:
        try:
            rows = conn.execute(
                f"SELECT {base_sql} FROM engine_assets ORDER BY sort_order, id").fetchall()
        except psycopg.errors.UndefinedTable:
            return False, "engine_assets not migrated yet"
        except Exception as ex:
            return False, f"could not read engine_assets: {ex}"[:200]
    except Exception as ex:
        return False, f"could not read engine_assets: {ex}"[:200]
    if not rows:
        return False, "engine_assets is empty — kept the existing config"
    assets = []
    for r in rows:
        ps = r.get("provider_symbols")
        if isinstance(ps, str):
            try:
                ps = json.loads(ps)
            except Exception:
                ps = {}
        a = {
            "id": r.get("id"), "name": r.get("name"), "instrument": r.get("instrument"),
            "ticker": r.get("ticker"), "provider_symbols": ps or {}, "asset_class": r.get("asset_class"),
            "session_profile": r.get("session_profile"), "cadence": r.get("cadence"),
            "timezone": r.get("timezone"), "roll_utc": r.get("roll_utc"), "related": r.get("related") or "",
            "forecast_window": r.get("forecast_window"), "publish_policy": r.get("publish_policy"),
            "report_tier": r.get("report_tier"), "enabled": bool(r.get("enabled")),
        }
        # multi-timeframe + fetch config — omit nulls/empties so config_loader applies its defaults.
        if r.get("cadence_day") not in (None, ""):
            a["cadence_day"] = r.get("cadence_day")
        tfs = r.get("timeframes")
        if isinstance(tfs, str):
            try:
                tfs = json.loads(tfs)
            except Exception:
                tfs = None
        if isinstance(tfs, list) and tfs:
            a["timeframes"] = [str(t) for t in tfs]
        civ = r.get("chart_intervals")
        if isinstance(civ, str):
            try:
                civ = json.loads(civ)
            except Exception:
                civ = None
        if isinstance(civ, list) and civ:
            a["chart_intervals"] = [str(i) for i in civ]
        if r.get("include_fundamentals") is not None:
            a["include_fundamentals"] = bool(r.get("include_fundamentals"))
        if r.get("include_news") is not None:
            a["include_news"] = bool(r.get("include_news"))
        if r.get("fundamentals_source"):
            a["fundamentals_source"] = r.get("fundamentals_source")
        assets.append(a)
    cfg = ROOT / "config" / "assets.json"
    tmp = ROOT / "config" / "assets.json.tmp"
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            tmp.write_text(json.dumps({"assets": assets}, indent=2), encoding="utf-8")
            if str(SCRIPTS) not in sys.path:
                sys.path.insert(0, str(SCRIPTS))
            import config_loader   # validates taxonomy/session/cadence/tz enums; raises on any bad asset
            config_loader.load_assets(tmp)   # <- the safety gate: a bad universe raises here
            os.replace(tmp, cfg)             # atomic; only reached if validation passed
    except _FileLock.Locked:
        return False, "another run is in progress — config not synced"
    except Exception as ex:
        try:
            tmp.unlink()
        except Exception:
            pass
        return False, f"validation failed — kept the existing config. {str(ex)[:240]}"
    enabled = sum(1 for a in assets if a["enabled"])
    try:                                   # diagnostics snapshot in engine.sqlite (best-effort)
        import ledger_db
        ledger_db.cache_assets(assets, db_path=ROOT / "ledger" / "engine.sqlite")
    except Exception:
        pass
    return True, f"synced {len(assets)} assets to config/assets.json ({enabled} enabled)"


def _cmd_sync_assets(conn, args):
    """Rebuild config/assets.json from the dashboard's engine_assets (validated before it applies)."""
    ok, msg = _sync_assets_from_neon(conn)
    return ok, msg, None, False


def _cmd_reset_ledger(conn, args):
    """Truncate ledger/outcome_ledger.csv to its header row — starts the track record fresh. The box
    runs the poller, so this works without SSH/sudo. (Neon scored_results is cleared separately.)"""
    p = ROOT / "ledger" / "outcome_ledger.csv"
    try:
        if not p.exists():
            return True, "no ledger file — nothing to reset", None, False
        lines = p.read_text(encoding="utf-8").splitlines()
        header = lines[0] if lines else ""
        tmp = ROOT / "ledger" / "outcome_ledger.csv.tmp"
        tmp.write_text((header + "\n") if header else "", encoding="utf-8")
        os.replace(tmp, p)
        return True, f"ledger reset to header ({len(lines) - 1 if lines else 0} rows cleared)", None, False
    except Exception as ex:
        return False, f"could not reset ledger: {ex}"[:200], None, False


def _cmd_clear_reports(conn, args):
    """Clear the engine's working dirs (reports/data/content/runs) on the box — a dashboard-driven
    system refresh, so you never need SSH + sudo. The ledger is NOT touched (use reset_ledger).
    Held under the run lock so it never deletes mid-generation."""
    import shutil
    subdirs = ["reports", "data/payloads", "data/predictions", "data/analysis", "data/candles",
               "content", "runs"]
    cleared = []
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            for sub in subdirs:
                d = ROOT / sub
                if not d.is_dir():
                    continue
                for child in d.iterdir():
                    try:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink()
                    except Exception:
                        pass
                cleared.append(sub)
    except _FileLock.Locked:
        return False, "another run is in progress — retry clear_reports shortly", None, False
    return True, f"cleared working dirs: {', '.join(cleared) or '(none present)'}", None, False


def _cmd_run_scoring(conn, args):
    """Run run_daily --mode score_only: grade any closed prediction windows into the ledger WITHOUT
    generating new reports. Held under the run lock. Use it to push the track record forward on demand."""
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            p = subprocess.run([sys.executable, "-m", RUN_DAILY, "--mode", "score_only"],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=900)
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            if p.returncode != 0:
                return False, f"scoring run exited {p.returncode}", _tail(out, 2000), False
            # score_only only writes the LOCAL ledger CSV; push the freshly-scored rows to Neon
            # scored_results (the public track record). Run the publish chain INSIDE the run lock —
            # exactly like run_and_record — so a separate-process daily run can't grab the freed lock
            # and publish concurrently against the shared content/ dir (a half-written-export race).
            pub_ok, pub_err, pub_log = _publish_chain(conn, None)
    except _FileLock.Locked:
        return False, "another run is in progress — retry run_scoring shortly", None, False
    msg = ("scoring run complete (score_only) + synced" if pub_ok
           else f"scored locally, but Neon sync failed: {pub_err}")
    return True, msg, _tail((out + "\n\n" + (pub_log or "")).strip(), 2000), False


def _r2_client():
    """Build the R2 (S3-compatible) client from env (R2_ACCOUNT_ID/ACCESS_KEY_ID/SECRET_ACCESS_KEY/
    BUCKET, loaded from ROOT/.env). Returns (client, bucket). Raises if creds/boto3 are missing."""
    _load_dotenv_into_environ()
    acct = os.environ.get("R2_ACCOUNT_ID")
    ak = os.environ.get("R2_ACCESS_KEY_ID")
    sk = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    if not (acct and ak and sk and bucket):
        raise RuntimeError("R2_* env vars not set")
    import boto3
    client = boto3.client(
        "s3", endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=ak, aws_secret_access_key=sk, region_name="auto")
    return client, bucket


def _cmd_compute_due(conn, args):
    """Run run_daily --mode dry_run (no generation, no network) to compute the engine's DUE plan,
    then write each asset's due status back to engine_assets so the dashboard can show which
    instruments are scheduled to generate. Safe + read-only w.r.t. reports."""
    _start = time.time()
    try:
        # hold the run lock: this dry-run writes runs/<date>/run_manifest.json, which would race +
        # clobber a real daily oneshot's manifest if they overlap (this was the only run path that
        # didn't take the lock).
        with _FileLock(LOCK_PATH, blocking=False):
            p = subprocess.run([sys.executable, "-m", RUN_DAILY, "--mode", "dry_run"],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=180)
    except _FileLock.Locked:
        return False, "a run is in progress; compute-due skipped (try again shortly)", None, False
    except Exception as ex:
        return False, f"dry_run failed to launch: {ex}"[:200], None, False
    if p.returncode != 0:
        return False, f"dry_run exited {p.returncode}", _tail((p.stdout or "") + (p.stderr or ""), 1000), False
    manifest, _path = _read_run_manifest(since=_start)
    plan = (manifest or {}).get("plan") or []
    if not plan:
        return False, "no plan found in the dry-run manifest", None, False
    updated = 0
    for entry in plan:
        aid = entry.get("asset_id")
        if not aid:
            continue
        due = entry.get("decision") == "generate"
        reason = str(entry.get("reason") or "")[:200]
        try:
            conn.execute(
                "UPDATE engine_assets SET due = %s, due_reason = %s, due_checked_at = now() WHERE id = %s",
                (due, reason, aid))
            updated += 1
        except psycopg.errors.UndefinedColumn:
            return False, "engine_assets is missing the due columns — run npm run migrate:up", None, False
        except Exception:
            pass
    due_now = sum(1 for e in plan if e.get("decision") == "generate")
    return True, f"due plan computed: {due_now}/{len(plan)} due now (updated {updated})", None, False


def _cmd_service_check(conn, args):
    """Health-check that the box can reach Neon, R2 and Upstash. Read-only; the result (per-service
    status) shows in the Box command log."""
    lines = []
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        lines.append(f"Neon:    OK (SELECT 1 -> {row.get('ok') if row else '?'})")
    except Exception as ex:
        lines.append(f"Neon:    FAIL ({str(ex)[:140]})")
    try:
        client, bucket = _r2_client()
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        lines.append(f"R2:      OK (bucket '{bucket}' reachable)")
    except Exception as ex:
        lines.append(f"R2:      FAIL ({str(ex)[:140]})")
    try:
        if upstash_enabled():
            hb = _upstash(["GET", HEARTBEAT_KEY])
            lines.append(f"Upstash: OK (heartbeat -> {hb or 'none yet'})")
        else:
            lines.append("Upstash: not configured (poller falls back to per-tick Neon polling)")
    except Exception as ex:
        lines.append(f"Upstash: FAIL ({str(ex)[:140]})")
    out = "\n".join(lines)
    ok = "FAIL" not in out
    return ok, "service check: " + ("all reachable" if ok else "one or more FAILED — see log"), out, False


def _cmd_clear_r2(conn, args):
    """Delete report objects from R2. args {date:'YYYY-MM-DD'} clears just that date's prefix; with
    no date it clears the WHOLE bucket. Destructive — the web confirms first."""
    import re as _re
    try:
        client, bucket = _r2_client()
    except Exception as ex:
        return False, f"R2 not configured: {str(ex)[:160]}", None, False
    date = (args or {}).get("date")
    prefix = f"{date}/" if date and _re.match(r"^\d{4}-\d{2}-\d{2}$", str(date)) else ""
    deleted = 0
    try:
        token = None
        while True:
            kw = {"Bucket": bucket, "MaxKeys": 1000}
            if prefix:
                kw["Prefix"] = prefix
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            objs = [{"Key": o["Key"]} for o in resp.get("Contents", [])]
            if objs:
                client.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                deleted += len(objs)
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
    except Exception as ex:
        return False, f"R2 delete failed after {deleted}: {str(ex)[:160]}", None, False
    scope = f"date {date}" if prefix else "the whole bucket"
    return True, f"deleted {deleted} object(s) from R2 ({scope})", None, False


def _cmd_clear_wake(conn, args):
    """Clear the Upstash wake flag (in case a stale wake key is stuck on the next tick)."""
    clear_wake()
    return True, "cleared the Upstash wake flag", None, False


def _cmd_run_backtest(conn, args):
    """Run an ISOLATED backtest: generate + score one or more assets AS-OF one or more closed
    windows, writing ONLY to the sim/ subtrees (ledger/sim, data/predictions/sim, reports/sim)
    and NEVER publishing (no export/publish/sync) or rebuilding the live calibration map.

    args {assets: [asset_id...], as_of: "YYYY-MM-DD HH:MM", days: int=1}. A backtest REQUIRES a
    closed window (as_of) and at least one asset — without a past as_of the prediction window
    wouldn't be closed (nothing to score), and an unscoped backtest would generate the whole
    universe into sim, which is never what a targeted test wants. Either missing -> clear error.

    `days` (default 1, clamped 1..MAX_BACKTEST_DAYS) simulates MULTIPLE consecutive days counting
    BACK from as_of (day 0 = as_of, day k = as_of - k days, same HH:MM) — each a distinct backdated
    report. All days run under ONE run lock, all sandboxed; after they complete, sync_backtest.py
    pushes ledger/sim -> Neon backtest_results once. Delegates to run_backtest_batch, which records
    ONE engine_runs row (trigger 'backtest') summarising the batch."""
    assets = [str(a).strip().lower() for a in (args.get("assets") or []) if a is not None and str(a).strip()]
    if not assets:
        return False, "run_backtest requires at least one asset (args.assets)", None, False
    as_of = args.get("as_of")
    if not (isinstance(as_of, str) and as_of.strip()):
        return False, "run_backtest requires a closed window (args.as_of 'YYYY-MM-DD HH:MM')", None, False
    try:
        datetime.strptime(as_of.strip()[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return False, f"run_backtest as_of {as_of!r} must be 'YYYY-MM-DD HH:MM'", None, False
    days = args.get("days", 1)
    try:
        days = int(days)
    except (TypeError, ValueError):
        return False, f"run_backtest days {days!r} must be an integer (1..{MAX_BACKTEST_DAYS})", None, False
    if days < 1:
        return False, f"run_backtest days must be >= 1 (got {days})", None, False
    days = min(MAX_BACKTEST_DAYS, days)
    run_id = run_backtest_batch(conn, assets, as_of.strip()[:16], days=days)
    span = "1 day" if days == 1 else f"{days} days (back from as_of)"
    return True, (f"backtest run {run_id} complete (sandbox: ledger/sim + reports/sim, no publish; "
                  f"synced to backtest_results) — assets={assets} as_of={as_of.strip()[:16]} "
                  f"· {span}"), None, False


def _cmd_clear_sandbox(conn, args):
    """Reset the box's SANDBOX working trees so the admin can start a fresh backtest: empties
    ledger/sim, data/predictions/sim, reports/sim (and only those — the live ledger/reports/data
    are NEVER touched). It removes EVERY child of each dir (files and subdirs alike, via rmtree), so
    the data/predictions/sim/scored/ per-prediction sidecars are wiped too. Mirrors _cmd_clear_reports'
    safe per-dir clearing and is held under the run lock so it never deletes mid-backtest. The Neon
    backtest_results / backtest_predictions tables are cleared separately."""
    import shutil
    cleared = []
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            for sub in SANDBOX_DIRS:
                d = ROOT / sub
                if not d.is_dir():
                    continue
                for child in d.iterdir():
                    try:
                        if child.is_dir():
                            shutil.rmtree(child, ignore_errors=True)
                        else:
                            child.unlink()
                    except Exception:
                        pass
                cleared.append(sub)
    except _FileLock.Locked:
        return False, "another run is in progress — retry clear_sandbox shortly", None, False
    return True, f"cleared sandbox dirs: {', '.join(cleared) or '(none present)'}", None, False


_COMMAND_HANDLERS = {
    "restart_poller": _cmd_restart_poller,
    "pull_latest": _cmd_pull_latest,
    "run_maintenance": _cmd_run_maintenance,
    "tail_logs": _cmd_tail_logs,
    "set_config": _cmd_set_config,
    "sync_assets": _cmd_sync_assets,
    "reset_ledger": _cmd_reset_ledger,
    "clear_reports": _cmd_clear_reports,
    "run_scoring": _cmd_run_scoring,
    "compute_due": _cmd_compute_due,
    "service_check": _cmd_service_check,
    "clear_r2": _cmd_clear_r2,
    "clear_wake": _cmd_clear_wake,
    "run_backtest": _cmd_run_backtest,
    "clear_sandbox": _cmd_clear_sandbox,
}

# Canonical allow-list (the web keeps its own copy for defence in depth; this is the boundary).
ALLOWED_COMMANDS = tuple(_COMMAND_HANDLERS.keys())
