"""commands.py — engine_commands control channel (split out of engine_ops)."""
import json, os, subprocess, sys, time
from datetime import datetime

import psycopg

from _paths import ROOT, SCRIPTS
from locking import _FileLock, LOCK_PATH
from db import _load_dotenv_into_environ, _empty_dir, EngineDB
from wake import upstash_enabled, _upstash, HEARTBEAT_KEY, clear_wake
from manifest import _tail, _read_run_manifest
from runner import _publish_chain, run_backtest_batch, RUN_DAILY, MAX_BACKTEST_DAYS, SANDBOX_DIRS


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


# Thin conn-first wrappers over EngineDB — SAME signatures, so the poller, the engine_ops façade,
# and the tests are unchanged. The two-phase engine_commands claim/reap bodies now live on EngineDB
# (claim_next_command unified with claim_next_request into EngineDB._claim_next).
def claim_next_command(conn):
    return EngineDB(conn).claim_next_command()


def reap_stale_commands(conn):
    return EngineDB(conn).reap_stale_commands()


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
    return {"status": status, "result": result, "restart": bool(restart),
            "log": (_tail(log, 4096) if log else None)}


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
    subdirs = ["reports", "data/payloads", "data/predictions", "data/analysis", "data/candles",
               "content", "runs"]
    cleared = []
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            for sub in subdirs:
                if _empty_dir(ROOT / sub):
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
    NO date key it clears the WHOLE bucket. A present-but-malformed date is REJECTED (never silently
    widened to a full-bucket wipe). Destructive — the web confirms first."""
    import re as _re
    date = (args or {}).get("date")
    if date is None:
        prefix = ""                       # no date supplied -> whole bucket (the documented intent)
    elif isinstance(date, str) and _re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        prefix = f"{date}/"
    else:
        # A malformed / extra-whitespace date must NOT fall through to "" — that would delete the
        # ENTIRE bucket. Require an EXACT YYYY-MM-DD; omit `date` for a deliberate full-bucket clear.
        return False, (f"date {date!r} must be exactly 'YYYY-MM-DD' — omit the date entirely to "
                       "clear the whole bucket"), None, False
    try:
        client, bucket = _r2_client()
    except Exception as ex:
        return False, f"R2 not configured: {str(ex)[:160]}", None, False
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
    cleared = []
    try:
        with _FileLock(LOCK_PATH, blocking=False):
            for sub in SANDBOX_DIRS:
                if _empty_dir(ROOT / sub):
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
