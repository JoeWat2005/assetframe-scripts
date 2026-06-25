"""run_daily.py — the AssetFrame scheduler. The SCHEDULER decides what runs; Claude
only writes/criticises briefs inside the workflow.

Deterministic daily batch over config/assets.json:
  1. load + validate the universe (config_loader)
  2. resolve DUE assets by cadence x market calendar (calendar_rules)
  3. SCORE closed prediction windows first (score_report, dedup-guarded) — no look-ahead
  4. refresh ledger-derived memory (calibrate, research_memory)
  5. for each DUE asset (ThreadPool): intraday -> memory_pack -> [brief] -> scaffold ->
     confidence -> mvp_report (QA gate)
  6. write runs/<date>/run_manifest.json (per-asset decision/status/timings/errors)

Run modes:
  dry_run        (default) resolve the plan + write the manifest; NO network/scoring/generation
  score_only     score closed windows + refresh memory; no generation
  generate_only  generate due assets; skip scoring
  production     score + refresh + generate  (publish/approval wired in Phase 2)

Brief authoring: when an asset has no data/briefs/<TICKER>_research_brief.json and the
mode generates, brief_writer.py authors one (Anthropic API + web_search) and critic.py
adversarially reviews it. On approve the brief now exists and the pipeline continues; a
critic 'revise' triggers ONE bounded re-author; 'reject'/'stand_aside' skip the asset
(status brief_rejected / brief_stand_aside). If ANTHROPIC_API_KEY is unset, the SDK is
missing, or the writer fails, the asset degrades to "needs_brief" (operator-written
fallback) — a keyless run never hard-stops. An operator-written brief already present is
used as-is. Set ASSETFRAME_AUTHOR_BRIEFS=0 to force the legacy operator-only behaviour.
Each manifest job carries a token_cost block (writer + critic tokens/web-searches/$).
Idempotent: deterministic run_id/report_id + the scorer's dedup guard mean a re-run never
double-scores or double-appends.

Scale path (ASSETFRAME_BRIEF_BATCH=1): instead of one rate-limited Anthropic call per asset, ALL
due briefs are authored in one Message Batch and critiqued (on Haiku) in a second — no per-minute
rate limit, 50% cheaper, ~constant wall-clock as the universe grows. web_search + prompt caching
still apply inside the batch. The synchronous per-asset path is the automatic fallback if a batch
submission fails (or returns no clean outcome), so the run can never hard-stop on the batch step.

Usage:
  python scripts/run_daily.py [--universe config/assets.json] [--asset <id>]
        [--asset-class fx] [--mode dry_run|score_only|generate_only|production]
        [--date YYYY-MM-DD] [--as-of "YYYY-MM-DD HH:MM"] [--workers 4] [--no-render]
        [--sandbox]

--sandbox isolates a backtest from production: ASSETFRAME_SANDBOX=1 is set for the run
(and inherited by every child subprocess), redirecting persistent writes to sim/ subtrees
(ledger/sim, data/predictions/sim, reports/sim) and SKIPPING the calibration/research/
ledger_db memory refresh. The live ledger + calibration map are never rebuilt from a
sandbox run. (Publish is skipped by the caller — engine_ops.run_and_record — not here.)
"""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _paths import ROOT, SCRIPTS   # repo-root anchors (scripts/__init__ shim is on sys.path under -m)
sys.path.insert(0, str(SCRIPTS))
import config_loader
# Seed runtime settings from config/engine.json before any module-level env read below (env wins).
config_loader.apply_runtime_env(ROOT / "config" / "engine.json")
import calendar_rules
import memory_pack as mp
import brief_batch   # Anthropic Message-Batches author/critique orchestrator (scale path)

MODES = ("dry_run", "score_only", "generate_only", "production")
PRED_DIR = ROOT / "data" / "predictions"
BRIEF_DIR = ROOT / "data" / "briefs"
MEMPACK_DIR = ROOT / "data" / "memory_packs"
RESEARCH_DIR = ROOT / "data" / "research"
SOCIAL_DIR = ROOT / "data" / "social"
LEDGER = ROOT / "ledger" / "outcome_ledger.csv"

# Autonomous brief authoring (brief_writer.py + critic.py). Only engaged when a brief
# is MISSING and the mode generates; a keyless run or a writer failure degrades back to
# the operator-written "needs_brief" path so the engine never hard-stops on the AI step.
BRIEF_AUTHORING = os.environ.get("ASSETFRAME_AUTHOR_BRIEFS", "1") != "0"
WRITER_TIMEOUT = 600        # web_search authoring can be slow
CRITIC_TIMEOUT = 300

# Throttle the (Anthropic, rate-limited) brief authoring across the worker threads. The pipeline
# parallelises freely (intraday fetches etc.), but multiple briefs authoring AT ONCE burst past the
# API's per-minute token limit on a low usage tier and all fail. This semaphore caps how many briefs
# author concurrently. Default 1 = fully sequential briefs (safe on Anthropic Tier 1); raise
# ASSETFRAME_BRIEF_CONCURRENCY (config/engine.json) on a higher tier for speed.
try:
    _BRIEF_CONCURRENCY = max(1, int(os.environ.get("ASSETFRAME_BRIEF_CONCURRENCY", "1")))
except (TypeError, ValueError):
    _BRIEF_CONCURRENCY = 1
_BRIEF_SEM = threading.Semaphore(_BRIEF_CONCURRENCY)


def _envint(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Brief authoring via the Anthropic Message Batches API (scale path). When ON, ALL due assets'
# briefs are authored in one batch and critiqued in a second — no per-minute rate limit (batches
# have their own pool), 50% cheaper, ~constant wall-clock as the universe grows. web_search and
# prompt caching both still apply inside the batch. OFF (default) keeps the proven synchronous
# per-asset path, which is ALSO the automatic fallback if a batch submission fails. Enable per box
# in config/engine.json (ASSETFRAME_BRIEF_BATCH=1) after validating once with a sandbox backtest.
BRIEF_BATCH = os.environ.get("ASSETFRAME_BRIEF_BATCH", "0") == "1"
# Models/budgets for the batch path (the synchronous subprocesses read these same env vars).
BRIEF_MODEL = os.environ.get("ASSETFRAME_BRIEF_MODEL", "claude-sonnet-4-6")
BRIEF_MAX_TOKENS = _envint("ASSETFRAME_BRIEF_MAX_TOKENS", 20000)
# Critic runs on Haiku by default: the adversarial review is a structured check, so the cheapest/
# fastest model with the highest rate ceiling fits — ~80% cheaper than reviewing on Sonnet.
CRITIC_MODEL = os.environ.get("ASSETFRAME_CRITIC_MODEL", "claude-haiku-4-5-20251001")
CRITIC_MAX_TOKENS = _envint("ASSETFRAME_CRITIC_MAX_TOKENS", 3000)

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover
    LONDON = None


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
    """Best-effort: parse the last JSON object printed on a child's stdout."""
    if not text:
        return {}
    i = text.rfind("{")
    if i == -1:
        return {}
    try:
        return json.loads(text[i:])
    except Exception:
        return {}


def parse_args(argv):
    o = {"universe": "config/assets.json", "asset": [], "asset_class": None,
         "mode": "dry_run", "date": None, "as_of": None, "workers": 4, "no_render": False,
         "sandbox": False}
    keys = {"--universe": "universe", "--asset-class": "asset_class",
            "--mode": "mode", "--date": "date", "--as-of": "as_of", "--workers": "workers"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-render":
            o["no_render"] = True
        elif a == "--sandbox":              # isolate writes under sim/ + skip publish/calibration
            o["sandbox"] = True
        elif a == "--asset":               # repeatable — a multi-asset scope keeps EVERY id
            i += 1
            if i >= len(argv):
                print("ERROR: --asset needs a value"); sys.exit(2)
            o["asset"].append(argv[i])
        elif a in keys:
            i += 1
            if i >= len(argv):
                print(f"ERROR: {a} needs a value"); sys.exit(2)
            o[keys[a]] = argv[i]
        else:
            print(f"ERROR: unknown argument {a}"); sys.exit(2)
        i += 1
    o["workers"] = int(o["workers"])
    if o["mode"] not in MODES:
        print(f"ERROR: --mode must be one of {MODES}"); sys.exit(2)
    return o


def resolve_now(o):
    if o["as_of"]:
        return datetime.strptime(o["as_of"].strip()[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    if o["date"]:
        # treat --date as that date 06:00 Europe/London (the canonical run time)
        d = datetime.strptime(o["date"][:10], "%Y-%m-%d")
        now = (d.replace(hour=6, tzinfo=LONDON) if LONDON else d.replace(hour=6, tzinfo=timezone.utc)
               ).astimezone(timezone.utc)
        # --date is a BACKDATE: forward it as the as-of moment so intraday + scaffold past-date
        # the report too (no look-ahead), matching the already-backdated memory. Without this,
        # `--date` alone produced a TODAY-dated, live-priced report under a past run folder.
        if not o["as_of"]:
            o["as_of"] = now.strftime("%Y-%m-%d %H:%M")
        return now
    return datetime.now(timezone.utc)


def select_assets(o):
    assets = config_loader.load_assets(o["universe"])
    if o["asset"]:
        ids = {a.strip().lower() for a in o["asset"]}
        assets = [a for a in assets if a["id"].lower() in ids]
        if not assets:
            print(f"ERROR: none of assets {sorted(ids)} in {o['universe']}"); sys.exit(2)
    elif o["asset_class"]:
        assets = [a for a in assets if a["asset_class"] == o["asset_class"]]
    return assets


# --------------------------------------------------------------- score step
def score_step(now, tickers=None):
    """Score every in-scope prediction file whose window has closed (the scorer's dedup
    guard makes this safe to re-run), then refresh calibration + research memory.
    `tickers` (selected-asset tickers) scopes which files are eligible, so a scoped run
    cannot pull unrelated/stale predictions into the ledger; None = all in scope."""
    scored, skipped, errors = [], [], []
    for pf in sorted(PRED_DIR.glob("*_predictions.json")):
        try:
            p = json.loads(pf.read_text(encoding="utf-8-sig"))
            wend = datetime.strptime(p["window_end_utc"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except Exception as ex:
            errors.append({"file": pf.name, "error": str(ex)[:120]}); continue
        rid = p.get("report_id", "")
        tk = rid.rsplit("-", 1)[-1].upper() if rid else ""
        if tickers is not None and tk not in tickers:
            skipped.append({"file": pf.name, "reason": "out of scope"}); continue
        if wend >= now:
            skipped.append({"file": pf.name, "reason": "window still open"}); continue
        ok, out, err = _run(["-m", "scripts.pipeline.score_report", str(pf.relative_to(ROOT))])
        try:
            summary = json.loads(out[out.index("{"):]) if "{" in out else {}
        except Exception:
            summary = {}
        rec = {"file": pf.name, "report_id": p.get("report_id"),
               "skipped_duplicate": summary.get("skipped_duplicate"),
               "hit_rate_pct": summary.get("hit_rate_pct"),
               "unresolved_manual": summary.get("unresolved_manual"), "ok": ok}
        if not ok:
            rec["error"] = (err or out)[-200:]
            errors.append(rec)
        else:
            scored.append(rec)
    # refresh ledger-derived memory (cheap; best-effort). SANDBOX: NEVER rebuild the live
    # calibration_map / research_memory / ledger_db from a sandbox ledger — a backtest grades
    # into ledger/sim and must not bleed into the production memory the live confidence engine
    # reads. So skip the refresh entirely under ASSETFRAME_SANDBOX=1.
    if os.environ.get("ASSETFRAME_SANDBOX") == "1":
        refresh = {"skipped": "sandbox"}
    else:
        refresh = {}
        for label, cmd in (("calibrate", ["-m", "scripts.analytics.calibrate"]),
                           ("research_memory", ["-m", "scripts.analytics.research_memory"]),
                           ("ledger_db", ["-m", "scripts.analytics.ledger_db", "rebuild"])):
            ok, _o, err = _run(cmd, timeout=60)
            refresh[label] = "ok" if ok else f"failed: {(err or '')[-120:]}"
    return {"scored": scored, "skipped": skipped, "errors": errors, "memory_refresh": refresh}


# --------------------------------------------------------------- brief authoring
def author_brief_step(asset, brief_path):
    """Autonomously author + adversarially review the research brief.

    Flow (matches the V2 'AI drafts, AI critiques, Python validates' design):
      1. brief_writer.py  -> writes brief_path (web_search-enabled; self-validates schema)
      2. critic.py        -> adversarial verdict (exit 0 approve/revise, 2 reject/stand_aside)
      3. on 'revise': ONE bounded re-author with the critic's issues as guidance, then
         re-critique once.
      4. on 'approve' (or 'revise' that the critic then accepts): leave the brief in
         place so the pipeline continues to scaffold.
      5. on 'reject'/'stand_aside': the brief is NOT used this run (we remove an
         authored-but-rejected file so a stale draft can't leak into the next stage).

    Returns a result dict consumed by generate_asset:
      {"status": "authored"|"brief_rejected"|"brief_stand_aside"|"writer_unavailable"|
                 "brief_failed",
       "decision": <critic decision or None>, "critic_summary": str,
       "issues": [...], "token_cost": {...}}
    Never raises — on any failure it returns a status that degrades to needs_brief.
    """
    tk = asset["ticker"]
    res = {"status": "brief_failed", "decision": None, "critic_summary": "",
           "issues": [], "token_cost": {"writer": [], "critic": []}}

    analysis = ROOT / "data" / "analysis" / f"{tk}_analysis.json"
    mempack = MEMPACK_DIR / f"{tk}_memory_pack.json"
    research = RESEARCH_DIR / f"{tk}_research_pack.json"
    social = SOCIAL_DIR / f"{tk}_social_pack.json"
    if not analysis.exists() or not mempack.exists():
        res["status"] = "brief_failed"
        res["critic_summary"] = "missing analysis or memory_pack input for the writer"
        return res

    def _rel(p):
        return str(p.relative_to(ROOT))

    def _write(guidance=None):
        cmd = ["-m", "scripts.pipeline.brief_writer", tk, "--analysis", _rel(analysis),
               "--memory-pack", _rel(mempack), "--out", _rel(brief_path)]
        if research.exists():
            cmd += ["--research", _rel(research)]
        if social.exists():
            cmd += ["--social", _rel(social)]
        if guidance:
            cmd += ["--guidance", guidance]
        # Technical-focus when the asset opts out of news — AND always in a SANDBOX backtest: news /
        # web-search would pull TODAY's information into a past-dated report (look-ahead), so a
        # backtest is only honest without it. (The price/technicals are already trimmed to the as-of.)
        if not asset.get("include_news", True) or os.environ.get("ASSETFRAME_SANDBOX") == "1":
            cmd += ["--no-news"]
        ok, rc, out, err = _run_rc(cmd, timeout=WRITER_TIMEOUT)
        res["token_cost"]["writer"].append(_parse_last_json(out) or {"error": (err or "")[-160:]})
        # exit 3 == ANTHROPIC_API_KEY unset / SDK missing / API error -> degrade gracefully
        return ok, rc, (err or out)

    def _critique():
        cmd = ["-m", "scripts.pipeline.critic", _rel(brief_path), "--asset", tk, "--analysis", _rel(analysis)]
        if research.exists():
            cmd += ["--research", _rel(research)]
        ok, rc, out, err = _run_rc(cmd, timeout=CRITIC_TIMEOUT)
        verdict = _parse_last_json(out)
        res["token_cost"]["critic"].append(verdict.get("_telemetry") or {"error": (err or "")[-160:]})
        return verdict, rc, (err or out)

    # 1. author
    ok, rc, msg = _write()
    if not ok:
        if rc == 3:           # keyless / SDK missing / API error -> operator-written fallback
            res["status"] = "writer_unavailable"
        res["critic_summary"] = (msg or "")[-200:]
        return res

    # 2. critique
    verdict, _crc, cmsg = _critique()
    decision = verdict.get("decision")
    res["decision"] = decision
    res["critic_summary"] = verdict.get("summary", "") or (cmsg or "")[-200:]
    res["issues"] = verdict.get("issues", [])
    if not decision:          # critic API/parse failure -> can't trust it; degrade
        res["status"] = "writer_unavailable"
        return res

    # 3. No second 'repair' authoring pass. 'approve' AND 'revise' both mean the brief is PUBLISHABLE
    # (a 'revise' is minor edits) — so both GENERATE directly. The old repair pass DOUBLED the
    # Anthropic calls per asset (author+critic -> +re-author+re-critique); on a multi-asset parallel
    # run that bursts past the API rate limit and fails the brief (-> spurious "needs_brief"). The QA
    # gate (mvp_report.run_qa) is the hard backstop on the rendered report. Only reject/stand_aside skip.

    # 4/5. resolve on the critic's verdict
    if decision in ("approve", "revise"):
        res["status"] = "authored"
    elif decision == "stand_aside":
        res["status"] = "brief_stand_aside"
        res["critic_summary"] = verdict.get("stand_aside_reason") or res["critic_summary"]
        _safe_unlink(brief_path)
    else:                     # reject (or anything unexpected after the loop)
        res["status"] = "brief_rejected"
        _safe_unlink(brief_path)
    return res


def _issues_to_guidance(verdict):
    """Render the critic's issues into a compact guidance string for the re-author."""
    lines = []
    for it in verdict.get("issues", []):
        if isinstance(it, dict):
            lines.append(f"[{it.get('severity','issue')}] {it.get('field','')}: "
                         f"{it.get('problem','')} -> {it.get('fix','')}".strip())
        else:
            lines.append(str(it))
    for adj in verdict.get("confidence_adjustments", []):
        lines.append(f"[conviction] {adj}")
    return "\n".join(lines) or (verdict.get("summary", ""))


def _safe_unlink(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def _stamp_authored_brief(path, run_day):
    """Mark a freshly AUTHORED brief with its run date so the NEXT day's run regenerates it
    (operator-written briefs carry no marker and are kept). Best-effort; never raises."""
    try:
        b = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        b["_af_authored"] = True
        b["_af_date"] = run_day
        Path(path).write_text(json.dumps(b, indent=1) + "\n", encoding="utf-8")
    except Exception:
        pass


def _sum_token_cost(token_cost):
    """Roll the writer + critic per-call telemetry into one per-asset cost summary
    for the manifest record."""
    tin = tout = web = 0
    usd = 0.0
    for call in (token_cost.get("writer", []) + token_cost.get("critic", [])):
        if not isinstance(call, dict):
            continue
        tin += call.get("input_tokens", 0) or 0
        tout += call.get("output_tokens", 0) or 0
        web += call.get("web_searches", 0) or 0
        usd += call.get("est_cost_usd", 0.0) or 0.0
    return {"input_tokens": tin, "output_tokens": tout, "web_searches": web,
            "est_cost_usd": round(usd, 4)}


def _total_token_cost(summaries):
    """Sum per-asset token_cost summaries into a run-level total for the manifest."""
    tot = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0, "est_cost_usd": 0.0}
    for s in summaries:
        if not isinstance(s, dict):
            continue
        tot["input_tokens"] += s.get("input_tokens", 0) or 0
        tot["output_tokens"] += s.get("output_tokens", 0) or 0
        tot["web_searches"] += s.get("web_searches", 0) or 0
        tot["est_cost_usd"] += s.get("est_cost_usd", 0.0) or 0.0
    tot["est_cost_usd"] = round(tot["est_cost_usd"], 4)
    return tot


# --------------------------------------------------------------- generate step
# generate_asset is factored into three shared helpers so the synchronous per-asset path and the
# batched (Message Batches) path run IDENTICAL data-prep + scaffold/render — only the brief
# ACQUISITION differs (one subprocess per asset vs one batch for all assets).

def _new_job_rec(asset):
    return {"asset_id": asset["id"], "ticker": asset["ticker"], "asset_class": asset["asset_class"],
            "report_id": None, "status": "error", "stages": {}, "errors": [],
            "token_cost": {"input_tokens": 0, "output_tokens": 0, "web_searches": 0,
                           "est_cost_usd": 0.0}}


def _stage_runner(rec):
    """A `stage(name, cmd, timeout)` closure bound to one job rec (records ok/failed + errors)."""
    def stage(name, cmd, timeout=180):
        ok, out, err = _run(cmd, timeout=timeout)
        rec["stages"][name] = "ok" if ok else "failed"
        if not ok:
            rec["errors"].append({name: (err or out)[-240:]})
        return ok, out
    return stage


def _read_json(path):
    """In-process JSON load (NOT brief_writer._load_json, which sys.exits on failure)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _data_prep(asset, now, as_of, rec, stage):
    """Steps 1-2: intraday + bounded memory_pack. Returns True if intraday succeeded (analysis is
    on disk); memory_pack is best-effort. On intraday failure sets rec['status']='data_error'."""
    tk = asset["ticker"]
    backdated = bool(as_of)
    now_arg = now.strftime("%Y-%m-%d %H:%M")

    # 1. data + analysis
    icmd = ["-m", "scripts.pipeline.intraday", asset["provider_symbols"]["yahoo"], "--name", tk,
            "--hrange", "10d", "--roll-utc", str(asset.get("roll_utc", 0)),
            "--session-profile", asset["session_profile"]]
    _civ = asset.get("chart_intervals") or []
    if _civ:                                          # candle intervals the view is analysed from
        icmd += ["--chart-intervals", ",".join(_civ)]
    _td_sym = (asset.get("provider_symbols") or {}).get("twelvedata")
    if _td_sym:                                   # explicit TD symbol (e.g. gold XAU/USD spot)
        icmd += ["--td-symbol", _td_sym]
    if asset.get("include_fundamentals"):         # equity fundamentals (narrative-only; TD)
        icmd += ["--fundamentals", "1",
                 "--fundamentals-source", asset.get("fundamentals_source") or "auto"]
    if asset.get("related"):
        icmd += ["--related", asset["related"]]
    if backdated:
        icmd += ["--as-of", now_arg]   # trim bars to <= the as-of moment (no look-ahead)
    ok, _ = stage("intraday", icmd, timeout=120)
    if not ok:
        rec["status"] = "data_error"
        return False

    # 2. bounded memory pack (for the brief writer / critic; written for audit)
    try:
        pack = mp.build_pack(asset, as_of=now)
        MEMPACK_DIR.mkdir(parents=True, exist_ok=True)
        (MEMPACK_DIR / f"{tk}_memory_pack.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
        rec["stages"]["memory_pack"] = "ok"
        rec["memory_pack_tokens"] = pack.get("budget", {}).get("approx_tokens")
    except Exception as ex:
        rec["stages"]["memory_pack"] = "failed"
        rec["errors"].append({"memory_pack": str(ex)[:160]})
    return True


def _finish_asset(asset, now, no_render, as_of, rec, stage):
    """Steps 3.5-5: ledger_context + scaffold + render/QA. Sets the terminal rec['status']."""
    tk = asset["ticker"]
    backdated = bool(as_of)
    now_arg = now.strftime("%Y-%m-%d %H:%M")

    # 3.5 per-instrument ledger context (the confidence engine's "memory"): turn THIS
    # instrument's own closed, scored windows into hit-rate priors as-of `now`. No
    # look-ahead — a backdated run only sees rows that closed before the as-of moment.
    # This writes the exact file scaffold reads, so the PUBLISHED confidence number learns
    # from the track record (not just the prose brief). BTC reruns use only BTC's rows;
    # an empty/young ledger yields a valid neutral context. Best-effort (never blocks).
    lcmd = ["-m", "scripts.analytics.ledger_context", tk, "--ticker", tk,
            "--asset-class", asset.get("asset_class", ""), "--as-of", now_arg]
    stage("ledger_context", lcmd, timeout=60)

    # 4. scaffold (payload + predictions + deterministic confidence)
    scmd = ["-m", "scripts.pipeline.scaffold_payload", tk, "--session-profile", asset["session_profile"]]
    # Scoring cadence: every daily-frequency cadence (weekday/trading_day/...) scores at the day
    # close; weekly/monthly score at week/month end. Drives the canonical one-per-period window.
    score_cadence = {"weekly": "weekly", "monthly": "monthly"}.get(asset.get("cadence"), "daily")
    scmd += ["--cadence", score_cadence]
    if asset.get("forecast_window"):
        scmd += ["--forecast-window", asset["forecast_window"]]  # standard windows are a no-op
    _tfs = asset.get("timeframes") or []
    if len(_tfs) > 1 or (_tfs and _tfs != [asset.get("forecast_window")]):
        scmd += ["--timeframes", ",".join(_tfs)]   # multi-timeframe: one report, N horizon tracks
    if backdated:
        scmd += ["--as-of", now_arg]   # past-date the prediction window so it can be scored
    ok, _ = stage("scaffold", scmd)
    if not ok:
        rec["status"] = "scaffold_error"
        return rec

    # 5. render + QA gate (or forecast-only)
    payload = f"data/payloads/{tk}_af_payload.json"
    rcmd = ["-m", "scripts.pipeline.mvp_report", payload] + (["--no-render"] if no_render else [])
    ok, out = stage("mvp_report", rcmd, timeout=240)
    try:
        rec["report_id"] = json.loads(Path(ROOT / payload).read_text(encoding="utf-8-sig")).get("report_id")
    except Exception:
        pass
    rec["status"] = ("generated" if not no_render else "forecast_only") if ok else "qa_failed"
    return rec


def _apply_authored(rec, ab, brief_path, run_day):
    """Apply an author_brief_step result dict (synchronous path) to the job rec. Returns True if the
    brief is publishable (continue to finish), False if the asset degrades (caller returns rec)."""
    rec["brief_source"] = "authored"
    rec["brief_token_cost"] = ab["token_cost"]
    rec["token_cost"] = _sum_token_cost(ab["token_cost"])
    if ab.get("decision"):
        rec["critic_decision"] = ab["decision"]
    if ab.get("critic_summary"):
        rec["critic_summary"] = ab["critic_summary"]
    if ab.get("issues"):
        rec["critic_issues"] = ab["issues"]
    if ab["status"] != "authored":
        # writer_unavailable/brief_failed -> needs_brief (operator can supply one);
        # brief_rejected/brief_stand_aside -> skip this asset, record why.
        rec["status"] = ("needs_brief" if ab["status"] in ("writer_unavailable", "brief_failed")
                         else ab["status"])
        rec["stages"]["brief"] = "authored" if ab.get("decision") else "skipped"
        return False
    rec["stages"]["brief"] = "authored"
    _stamp_authored_brief(brief_path, run_day)   # mark fresh AI brief so tomorrow regenerates it
    return True


def generate_asset(asset, now, no_render, as_of=None):
    """Deterministic per-asset pipeline: intraday -> memory_pack -> ledger_context ->
    [brief] -> scaffold -> confidence -> mvp_report. Returns a manifest job record (never
    raises). When `as_of` is set the run is BACKDATED: `now` is the as-of moment and we
    forward it to the data + scaffold stages so the prediction window is past-dated (and
    therefore scoreable), with no look-ahead in the ledger memory."""
    t0 = time.time()
    tk = asset["ticker"]
    rec = _new_job_rec(asset)
    stage = _stage_runner(rec)

    if not _data_prep(asset, now, as_of, rec, stage):
        rec["duration_s"] = round(time.time() - t0, 1)
        return rec

    # 3. brief — autonomously authored + adversarially reviewed. An operator-written brief (already
    # present) is honoured as-is. ALWAYS author a fresh, ORIGINAL brief — purge any existing one so
    # every report AND backtest day gets newly written analysis (no recycled theses). A hand-written
    # OPERATOR brief is honoured only when AI authoring is disabled (ASSETFRAME_AUTHOR_BRIEFS=0).
    brief = BRIEF_DIR / f"{tk}_research_brief.json"
    run_day = now.strftime("%Y-%m-%d")
    if BRIEF_AUTHORING and brief.exists():
        _safe_unlink(brief)              # force a fresh, original re-author below
    rec["brief_source"] = "operator"
    if not brief.exists():
        if not BRIEF_AUTHORING:
            rec["status"] = "needs_brief"; rec["duration_s"] = round(time.time() - t0, 1); return rec
        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
        with _BRIEF_SEM:                  # throttle concurrent Anthropic authoring (rate-limit safe)
            ab = author_brief_step(asset, brief)
        if not _apply_authored(rec, ab, brief, run_day):
            rec["duration_s"] = round(time.time() - t0, 1)
            return rec

    _finish_asset(asset, now, no_render, as_of, rec, stage)
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


def generate_due_batched(due_assets, now, no_render, as_of, workers=1):
    """Phased generation through the Anthropic Message Batches API: prep ALL assets -> author ALL
    briefs in one batch -> critique ALL in a second batch -> finish ALL survivors. Produces the same
    per-asset job records generate_asset does; only the brief ACQUISITION is batched.

    Raises ONLY on an author-batch submission failure (run_daily then falls back to the synchronous
    path — nothing has been authored yet, so no double spend). A critic-batch failure is caught here
    and degrades the authored assets to needs_brief (their briefs are not published unreviewed)."""
    run_day = now.strftime("%Y-%m-%d")
    sandbox = os.environ.get("ASSETFRAME_SANDBOX") == "1"
    recs = {a["ticker"]: _new_job_rec(a) for a in due_assets}
    stages = {a["ticker"]: _stage_runner(recs[a["ticker"]]) for a in due_assets}
    t0 = {a["ticker"]: time.time() for a in due_assets}

    def _seal(tk):
        recs[tk]["duration_s"] = round(time.time() - t0[tk], 1)

    # Phase 1 — data prep (intraday + memory_pack) for every asset, in parallel.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_data_prep, a, now, as_of, recs[a["ticker"]], stages[a["ticker"]]): a
                for a in due_assets}
        for f in as_completed(futs):
            f.result()   # _data_prep never raises; rec is updated in place

    # Build the author work-list: prepped assets whose analysis + memory_pack are on disk.
    items = []
    for a in due_assets:
        tk = a["ticker"]
        rec = recs[tk]
        if rec["stages"].get("intraday") != "ok":
            _seal(tk)                     # data_error already recorded by _data_prep
            continue
        analysis_p = ROOT / "data" / "analysis" / f"{tk}_analysis.json"
        mempack_p = MEMPACK_DIR / f"{tk}_memory_pack.json"
        brief_p = BRIEF_DIR / f"{tk}_research_brief.json"
        analysis = _read_json(analysis_p)
        mempack = _read_json(mempack_p)
        if analysis is None or mempack is None:
            rec["status"] = "needs_brief"; rec["stages"]["brief"] = "skipped"
            rec["critic_summary"] = "missing analysis or memory_pack input for the writer"
            _seal(tk)
            continue
        if BRIEF_AUTHORING and brief_p.exists():
            _safe_unlink(brief_p)         # fresh, original authoring every run
        research_p = RESEARCH_DIR / f"{tk}_research_pack.json"
        social_p = SOCIAL_DIR / f"{tk}_social_pack.json"
        items.append({
            "ticker": tk, "asset": a, "brief_path": brief_p, "analysis": analysis,
            "memory_pack": mempack,
            "research": _read_json(research_p) if research_p.exists() else None,
            "social": _read_json(social_p) if social_p.exists() else None,
            # SANDBOX always authors technical-only (no live news -> no look-ahead in a backtest).
            "include_news": bool(a.get("include_news", True)) and not sandbox,
        })

    if not items:
        return [recs[a["ticker"]] for a in due_assets]

    # Phase 2 — author ALL briefs in one batch (+ one repair batch for schema-failers). A submission
    # error raises out of here -> caller falls back to the synchronous writer.
    author = brief_batch.author_briefs(
        [{k: it[k] for k in ("ticker", "analysis", "memory_pack", "research", "social", "include_news")}
         for it in items],
        model=BRIEF_MODEL, max_tokens=BRIEF_MAX_TOKENS)

    review_items = []
    for it in items:
        tk = it["ticker"]
        rec = recs[tk]
        res = author.get(tk) or {"brief": None, "telemetry": {}, "error": "no batch result"}
        rec["brief_source"] = "authored"
        rec["brief_token_cost"] = {"writer": [res.get("telemetry") or {}], "critic": []}
        if not res.get("brief"):
            rec["token_cost"] = _sum_token_cost(rec["brief_token_cost"])
            rec["status"] = "needs_brief"; rec["stages"]["brief"] = "skipped"
            rec["critic_summary"] = (res.get("error") or "brief authoring failed")[:240]
            _seal(tk)
            continue
        try:
            it["brief_path"].parent.mkdir(parents=True, exist_ok=True)
            it["brief_path"].write_text(
                json.dumps(res["brief"], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        except Exception as ex:
            rec["token_cost"] = _sum_token_cost(rec["brief_token_cost"])
            rec["status"] = "needs_brief"; rec["stages"]["brief"] = "skipped"
            rec["critic_summary"] = f"could not write authored brief: {ex}"[:240]
            _seal(tk)
            continue
        it["_brief"] = res["brief"]
        review_items.append(it)

    # Phase 3 — critique ALL authored briefs in one batch (Haiku). A batch-level failure degrades the
    # authored assets to needs_brief rather than publishing them unreviewed (mirrors the sync path's
    # writer_unavailable on a critic miss) — and avoids re-authoring (the briefs are already paid for).
    if review_items:
        try:
            review = brief_batch.review_briefs(
                [{"ticker": it["ticker"], "brief": it["_brief"], "analysis": it["analysis"],
                  "research": it["research"]} for it in review_items],
                model=CRITIC_MODEL, max_tokens=CRITIC_MAX_TOKENS)
        except Exception as ex:
            print(f"  critic batch failed ({type(ex).__name__}: {ex}); "
                  f"authored briefs degrade to needs_brief")
            review = {it["ticker"]: None for it in review_items}
    else:
        review = {}

    survivors = []
    for it in review_items:
        tk = it["ticker"]
        rec = recs[tk]
        verdict = review.get(tk)
        critic_tele = (verdict or {}).get("_telemetry") or {}
        rec["brief_token_cost"]["critic"] = [critic_tele] if critic_tele else []
        rec["token_cost"] = _sum_token_cost(rec["brief_token_cost"])
        if not verdict or not verdict.get("decision"):
            rec["status"] = "needs_brief"; rec["stages"]["brief"] = "authored"
            rec["critic_summary"] = "critic unavailable for this brief"
            _seal(tk)
            continue
        decision = verdict["decision"]
        rec["critic_decision"] = decision
        rec["critic_summary"] = verdict.get("summary", "") or ""
        if verdict.get("issues"):
            rec["critic_issues"] = verdict["issues"]
        if decision in ("approve", "revise"):
            rec["stages"]["brief"] = "authored"
            _stamp_authored_brief(it["brief_path"], run_day)
            survivors.append(it)
        elif decision == "stand_aside":
            rec["status"] = "brief_stand_aside"; rec["stages"]["brief"] = "authored"
            rec["critic_summary"] = verdict.get("stand_aside_reason") or rec["critic_summary"]
            _safe_unlink(it["brief_path"]); _seal(tk)
        else:                              # reject (or anything unexpected)
            rec["status"] = "brief_rejected"; rec["stages"]["brief"] = "authored"
            _safe_unlink(it["brief_path"]); _seal(tk)

    # Phase 4 — finish (ledger_context + scaffold + render) every survivor, in parallel.
    def _fin(it):
        _finish_asset(it["asset"], now, no_render, as_of, recs[it["ticker"]], stages[it["ticker"]])
        _seal(it["ticker"])
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for f in as_completed([pool.submit(_fin, it) for it in survivors]):
            f.result()

    for a in due_assets:                   # safety net: every rec carries a duration
        recs[a["ticker"]].setdefault("duration_s", round(time.time() - t0[a["ticker"]], 1))
    return [recs[a["ticker"]] for a in due_assets]


def _job_line(rec):
    """One status line per asset for the run log / admin console. Surfaces WHY an asset didn't
    generate (needs_brief / brief_rejected / *_error) so a failure is self-diagnosing."""
    note = ""
    if rec["status"] not in ("generated", "forecast_only"):
        bits = []
        if rec.get("critic_decision"):
            bits.append(f"critic={rec['critic_decision']}")
        reason = rec.get("critic_summary")
        if not reason and rec.get("errors"):
            reason = "; ".join(str(e) for e in rec["errors"])
        if reason:
            bits.append(str(reason)[:240])
        if bits:
            note = "  ->  " + " | ".join(bits)
    src = rec.get("brief_source") or ""
    src_tag = f"[{src}]" if src else ""
    return (f"  {rec['ticker']:8} {rec['status']:14} {src_tag:12} "
            f"{rec.get('report_id') or ''} ({rec.get('duration_s')}s){note}")


def _generate_due(due_assets, now, no_render, as_of, workers):
    """Generate every due asset, returning the list of job records. Uses the Message-Batches path
    when enabled (ASSETFRAME_BRIEF_BATCH=1), with a robust fall back to the synchronous per-asset
    path on ANY batch failure — a submission error, or a 'no clean outcome' result (the signature of
    a broken batch parse). A genuinely quiet day (a generated/rejected/stand_aside present) is NOT
    treated as a failure."""
    if BRIEF_BATCH and BRIEF_AUTHORING and due_assets:
        jobs = None
        try:
            jobs = generate_due_batched(due_assets, now, no_render, as_of, workers=workers)
        except Exception as ex:
            print(f"  brief-batch path failed ({type(ex).__name__}: {ex}); "
                  f"falling back to synchronous per-asset authoring")
            jobs = None
        if jobs is not None:
            if any(j["status"] in ("generated", "forecast_only", "brief_rejected", "brief_stand_aside")
                   for j in jobs):
                return jobs
            print("  brief-batch produced no clean outcomes; falling back to synchronous authoring")

    jobs = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(generate_asset, a, now, no_render, as_of): a for a in due_assets}
        for f in as_completed(futs):
            jobs.append(f.result())
    return jobs


# --------------------------------------------------------------- storage retention
# What grows on the box vs what is overwritten in place:
#   * data/{analysis,candles,briefs,memory_packs,ledger_context,payloads,predictions} hold ONE
#     file per asset, REWRITTEN every run -> constant size, nothing to prune.
#   * ledger/* is the append-only track record + its derived maps -> tiny, NEVER pruned.
#   * reports/<YYYY-MM-DD>/ and runs/<YYYY-MM-DD>/ are the only things that accumulate. Reports
#     are pushed to R2 (the web serves from there), so the local copies are a redundant cache;
#     run manifests are logs. Both are pruned past a retention window.
_RETENTION_DIRS = ("reports", "runs")
_RETENTION_DEFAULT_DAYS = 14


def _retention_days(default=_RETENTION_DEFAULT_DAYS):
    """Days of reports/runs editions to keep locally. ASSETFRAME_RETENTION_DAYS overrides;
    0 (or negative) disables pruning entirely (keep everything)."""
    try:
        return int(os.environ.get("ASSETFRAME_RETENTION_DAYS", default))
    except (TypeError, ValueError):
        return default


def _prune_old_dated_dirs(keep_days, today):
    """Delete YYYY-MM-DD folders older than keep_days under reports/ and runs/. The ledger,
    config and the per-asset data/ working files are never touched. Best-effort: never raises.
    `today` is wall-clock UTC date (so a backdated --as-of run can't mis-date the cutoff)."""
    summary = {"keep_days": keep_days, "removed": [], "kept": 0, "errors": 0}
    if not keep_days or keep_days <= 0:
        summary["disabled"] = True
        return summary
    import shutil
    cutoff = today - timedelta(days=keep_days)
    for sub in _RETENTION_DIRS:
        d = ROOT / sub
        if not d.is_dir():
            continue
        for child in sorted(d.iterdir()):
            if not child.is_dir():
                continue
            try:
                folder_date = datetime.strptime(child.name, "%Y-%m-%d").date()
            except ValueError:
                continue                      # not a dated folder (e.g. "_archive") -> keep it
            if folder_date < cutoff:
                try:
                    shutil.rmtree(child, ignore_errors=True)
                    summary["removed"].append(f"{sub}/{child.name}")
                except Exception:
                    summary["errors"] += 1
            else:
                summary["kept"] += 1
    return summary


def main():
    o = parse_args(sys.argv[1:])
    # SANDBOX must be the FIRST thing armed — BEFORE any scoring/generation/subprocess — so
    # every child (intraday/scaffold/score_report/...) inherits ASSETFRAME_SANDBOX=1 and
    # redirects its persistent writes to the sim/ subtrees. We also repoint PRED_DIR (the
    # scorer's scan dir) at data/predictions/sim so score_step grades ONLY sandbox files,
    # and pre-create the sim/ roots so the first write never races a missing dir.
    if o["sandbox"]:
        global PRED_DIR, BRIEF_DIR
        os.environ["ASSETFRAME_SANDBOX"] = "1"
        PRED_DIR = ROOT / "data" / "predictions" / "sim"
        # Isolate briefs too: a backtest must NOT reuse the LIVE brief (authored with current news =
        # look-ahead). The sim brief dir starts empty, so each backdated day authors a FRESH
        # technical-only (--no-news) brief and never touches the live data/briefs/ tree. scaffold
        # mirrors this (reads data/briefs/sim + data/research/sim + data/social/sim under sandbox).
        BRIEF_DIR = ROOT / "data" / "briefs" / "sim"
        (ROOT / "ledger" / "sim").mkdir(parents=True, exist_ok=True)
        PRED_DIR.mkdir(parents=True, exist_ok=True)
        BRIEF_DIR.mkdir(parents=True, exist_ok=True)
    now = resolve_now(o)
    run_date = (now.astimezone(LONDON) if LONDON else now).strftime("%Y-%m-%d")
    run_id = f"daily-{run_date}"
    assets = select_assets(o)
    holidays = calendar_rules.load_holidays()

    # due plan (the scheduler's deterministic decision)
    plan = []
    for a in assets:
        due, reason = calendar_rules.is_due(a, now, holidays)
        plan.append({"asset_id": a["id"], "asset_class": a["asset_class"],
                     "decision": "generate" if due else "skip", "reason": reason})
    due_assets = [a for a in assets if next(p for p in plan if p["asset_id"] == a["id"])["decision"] == "generate"]

    manifest = {"run_id": run_id, "mode": o["mode"], "run_date": run_date,
                "generated_at_utc": now.strftime("%Y-%m-%d %H:%M"), "timezone": "Europe/London",
                "universe": o["universe"], "assets_selected": len(assets),
                "assets_due": len(due_assets), "plan": plan}
    if o["sandbox"]:
        manifest["sandbox"] = True

    print(f"[{run_id}] mode={o['mode']} @ {now.strftime('%Y-%m-%d %H:%M')} UTC | "
          f"selected={len(assets)} due={len(due_assets)} "
          f"({', '.join(a['id'] for a in due_assets) or 'none'})")
    # Explain every NON-due asset (market closed / disabled) so the dashboard log makes a quiet
    # day self-explanatory instead of just showing a lower count.
    for p in plan:
        if p["decision"] == "skip":
            print(f"  - skip {p['asset_id']}: {p['reason']}")

    # In sandbox the windows are scored AFTER generation (with a full-candle refresh), so skip this
    # pre-generate pass for a backtest — it would only see leftover sim predictions with trimmed data.
    if o["mode"] in ("score_only", "production") and not o["sandbox"]:
        print("scoring closed windows + refreshing memory...")
        manifest["score"] = score_step(now, {a["ticker"] for a in assets})
        s = manifest["score"]
        print(f"  scored={len(s['scored'])} skipped={len(s['skipped'])} errors={len(s['errors'])} "
              f"refresh={s['memory_refresh']}")
        for sc in s["scored"]:
            print(f"    + scored {sc.get('report_id') or sc.get('file')}: hit_rate={sc.get('hit_rate_pct')}")
        for sk in s["skipped"][:12]:
            print(f"    . skip {sk.get('file')}: {sk.get('reason')}")
        for er in s["errors"][:12]:
            print(f"    ! error {er.get('file')}: {er.get('error') or er.get('report_id')}")

    if o["mode"] in ("generate_only", "production"):
        # Pace asset parallelism to the Twelve Data plan. The Basic free tier (8 req/min) needs
        # assets serialized so intraday's per-process throttle holds across the whole run; Grow
        # (55/min — set TWELVEDATA_RATE_PER_MIN=55) has ample headroom, so keep the configured
        # workers. iv = min seconds between TD calls; clamp only when it implies a low (<=30/min) rate.
        if os.environ.get("ADVISOR_DATA_PROVIDER") == "twelvedata" and o["workers"] > 1:
            rate = os.environ.get("TWELVEDATA_RATE_PER_MIN")
            iv = None
            if rate not in (None, ""):
                try:
                    r = float(rate); iv = (60.0 / r) if r > 0 else 0.0
                except ValueError:
                    print(f"  WARNING: TWELVEDATA_RATE_PER_MIN={rate!r} is not a number "
                          f"(expected e.g. 55) -- falling back to TWELVEDATA_MIN_INTERVAL_S pacing")
                    iv = None
            if iv is None:
                _mi = os.environ.get("TWELVEDATA_MIN_INTERVAL_S", "8") or "0"
                try:
                    iv = float(_mi)
                except ValueError:
                    print(f"  WARNING: TWELVEDATA_MIN_INTERVAL_S={_mi!r} is not a number -- "
                          f"using the safe 8s default (workers will serialize)")
                    iv = 8.0
            if iv >= 2.0:   # <=30 req/min: serialize so the per-process throttle holds across assets
                print(f"  twelvedata low-rate tier: clamping workers {o['workers']} -> 1 "
                      f"(set TWELVEDATA_RATE_PER_MIN to your plan's limit, e.g. 55 for Grow)")
                o["workers"] = 1
        _batch_tag = " [batch]" if (BRIEF_BATCH and BRIEF_AUTHORING) else ""
        print(f"generating {len(due_assets)} due asset(s) with {o['workers']} worker(s){_batch_tag}...")
        jobs = _generate_due(due_assets, now, o["no_render"], o["as_of"], o["workers"])
        for rec in sorted(jobs, key=lambda r: r["asset_id"]):
            print(_job_line(rec))
        manifest["jobs"] = sorted(jobs, key=lambda r: r["asset_id"])
        manifest["generated"] = sum(1 for j in jobs if j["status"] in ("generated", "forecast_only"))
        manifest["needs_brief"] = [j["ticker"] for j in jobs if j["status"] == "needs_brief"]
        manifest["brief_rejected"] = [j["ticker"] for j in jobs if j["status"] == "brief_rejected"]
        manifest["brief_stand_aside"] = [j["ticker"] for j in jobs if j["status"] == "brief_stand_aside"]
        manifest["token_cost"] = _total_token_cost(j.get("token_cost") for j in jobs)

    if o["mode"] in ("generate_only", "production"):
        tc = manifest.get("token_cost", {})
        print(f"[{run_id}] generation done — generated={manifest.get('generated', 0)} "
              f"needs_brief={len(manifest.get('needs_brief', []))} "
              f"rejected={len(manifest.get('brief_rejected', [])) + len(manifest.get('brief_stand_aside', []))} "
              f"of {len(due_assets)} due · "
              f"~{tc.get('input_tokens', 0)}in/{tc.get('output_tokens', 0)}out tok "
              f"≈${tc.get('est_cost_usd', 0)}")

    # SANDBOX backtest is one-shot: the generate step above wrote sim predictions whose window is
    # already closed (as-of past), so grade them now into the sim ledger — there is no separate
    # sandbox "Score now". (Live runs keep the deliberate generate-then-Score-now two-step.)
    if o["sandbox"] and o["mode"] in ("generate_only", "production"):
        # A backtest PREDICTS with candles trimmed to the as-of (no look-ahead); SCORING needs the
        # candles that cover the now-closed window, so re-fetch the FULL series (a real API call, no
        # --as-of, spanning as-of -> today) for each generated asset, then grade against the REAL
        # clock — the backdated window has closed by today, which is the whole point of backdating.
        real_now = datetime.now(timezone.utc)
        _hd = max(10, (real_now - now).days + 4)   # candle range must span the as-of window -> today
        print(f"refreshing full candles ({_hd}d) + scoring the backtest's closed windows...")
        for a in due_assets:
            ricmd = ["-m", "scripts.pipeline.intraday", a["provider_symbols"]["yahoo"], "--name", a["ticker"],
                     "--hrange", f"{_hd}d", "--roll-utc", str(a.get("roll_utc", 0)),
                     "--session-profile", a["session_profile"]]
            _rtd = (a.get("provider_symbols") or {}).get("twelvedata")
            if _rtd:
                ricmd += ["--td-symbol", _rtd]
            _run(ricmd, timeout=120)
        post = score_step(real_now, {a["ticker"] for a in due_assets})
        prev = manifest.get("score") or {"scored": [], "skipped": [], "errors": []}
        manifest["score"] = {
            "scored": (prev.get("scored") or []) + post.get("scored", []),
            "skipped": post.get("skipped", []),
            "errors": (prev.get("errors") or []) + post.get("errors", []),
            "memory_refresh": post.get("memory_refresh"),
        }
        for sc in post.get("scored", []):
            print(f"    + scored {sc.get('report_id') or sc.get('file')}: hit_rate={sc.get('hit_rate_pct')}")

    # storage retention: prune old reports/ + runs/ edition folders (redundant after R2 publish;
    # the ledger/track record is never touched). Skipped on dry runs. Uses wall-clock UTC today.
    if o["mode"] != "dry_run":
        manifest["retention"] = _prune_old_dated_dirs(_retention_days(),
                                                      datetime.now(timezone.utc).date())
        _r = manifest["retention"]
        if _r.get("removed"):
            print(f"retention: pruned {len(_r['removed'])} folder(s) older than "
                  f"{_r['keep_days']}d (reports/runs) — local copies; R2 + ledger untouched")

    # always write the manifest (dry_run writes the plan only)
    run_dir = ROOT / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"manifest -> runs/{run_date}/run_manifest.json")
    # Mirror the run into engine.sqlite's run-history table (best-effort; live runs only, not sandbox).
    if not o["sandbox"]:
        try:
            import ledger_db
            ledger_db.record_run(run_id, o["mode"], run_date, "ok",
                                 generated=manifest.get("generated"), manifest=manifest,
                                 db_path=ROOT / "ledger" / "engine.sqlite")
        except Exception:
            pass
    if o["mode"] == "dry_run":
        print("DRY RUN - no scoring, generation, or publish performed.")


if __name__ == "__main__":
    main()
