"""runner.py — run lifecycle (split out of engine_ops)."""
import json, subprocess, sys, time
from datetime import datetime, timedelta

from _paths import ROOT, SCRIPTS
from locking import _FileLock, LOCK_PATH
from db import is_cancel_requested, _utcnow, RUN_TIMEOUT, _empty_dir, RunRecorder
from manifest import scope_to_run_args, _read_run_manifest, summarize_manifest, _new_run_id, _tail

RUN_DAILY = "scripts.scheduler.run.run_daily"            # spawned as `python -m <module>` (cwd = ROOT)
SYNC_BACKTEST = "scripts.analytics.store.sync_backtest"  # pushes ledger/sim -> Neon backtest_results
# The sandbox working trees a backtest writes to (cleared by clear_sandbox; never the live trees).
SANDBOX_DIRS = ["ledger/sim", "data/predictions/sim", "reports/sim",
                "data/briefs/sim", "data/research/sim", "data/social/sim"]
MAX_BACKTEST_DAYS = 90                   # up to ~3 months back; clamp so a typo can't fan out forever
CANCEL_POLL_SECONDS = 5                  # how often we check cancel_requested mid-run


# ------------------------------------------------------------- run + record
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
    rec = RunRecorder(conn, run_id, trigger, scope_json)
    if not rec.start():
        # If we can't even create the row, still try to mark the request failed.
        _finish_request(conn, request_id, "failed", run_id,
                        f"could not start run: {rec.start_error}"[:500])
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

    # 5. record the outcome. Finish the REQUEST row FIRST, then rec.finish() (which writes the
    # terminal engine_runs row and clears current_run_id LAST). Ordering is load-bearing: a stale
    # current_run_id self-heals via reap_stale_runs, but a generation_request stuck at 'running'
    # has no reaper — so it must be marked terminal before we release the run, else a crash between
    # the two writes would strand it forever. (Do NOT reorder these.)
    _finish_request(conn, request_id, _request_status(status), run_id, errors)
    rec.finish(status, results, errors, log_excerpt)
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
    for sub in SANDBOX_DIRS:
        _empty_dir(ROOT / sub)
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

    rec = RunRecorder(conn, run_id, "backtest", scope, trigger_literal=True)
    if not rec.start():
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
    rec.finish(status, results, errors, log_excerpt)
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
