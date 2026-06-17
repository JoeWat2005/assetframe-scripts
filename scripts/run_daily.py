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

Phase-1 note: the research brief is operator-written today. When an asset has no
data/briefs/<TICKER>_research_brief.json, generation records status "needs_brief" and
skips it (Phase 2 wires brief_writer.py + critic.py + the publish-hidden approval gate
here). Idempotent: deterministic run_id/report_id + the scorer's dedup guard mean a
re-run never double-scores or double-appends.

Usage:
  python scripts/run_daily.py [--universe config/assets.json] [--asset <id>]
        [--asset-class fx] [--mode dry_run|score_only|generate_only|production]
        [--date YYYY-MM-DD] [--as-of "YYYY-MM-DD HH:MM"] [--workers 4] [--no-render]
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import config_loader
import calendar_rules
import memory_pack as mp

MODES = ("dry_run", "score_only", "generate_only", "production")
PRED_DIR = ROOT / "data" / "predictions"
BRIEF_DIR = ROOT / "data" / "briefs"
MEMPACK_DIR = ROOT / "data" / "memory_packs"
LEDGER = ROOT / "ledger" / "outcome_ledger.csv"

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover
    LONDON = None


def _run(cmd, timeout=180):
    """Run a child script; return (ok, stdout, stderr). Never raises."""
    try:
        p = subprocess.run([sys.executable] + cmd, cwd=str(ROOT), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode == 0, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return False, "", f"timeout after {timeout}s"
    except Exception as ex:
        return False, "", str(ex)[:200]


def parse_args(argv):
    o = {"universe": "config/assets.json", "asset": None, "asset_class": None,
         "mode": "dry_run", "date": None, "as_of": None, "workers": 4, "no_render": False}
    keys = {"--universe": "universe", "--asset": "asset", "--asset-class": "asset_class",
            "--mode": "mode", "--date": "date", "--as-of": "as_of", "--workers": "workers"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--no-render":
            o["no_render"] = True
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
        return (d.replace(hour=6, tzinfo=LONDON) if LONDON else d.replace(hour=6, tzinfo=timezone.utc)
                ).astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def select_assets(o):
    assets = config_loader.load_assets(o["universe"])
    if o["asset"]:
        assets = [a for a in assets if a["id"] == o["asset"]]
        if not assets:
            print(f"ERROR: asset '{o['asset']}' not in {o['universe']}"); sys.exit(2)
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
        ok, out, err = _run(["scripts/score_report.py", str(pf.relative_to(ROOT))])
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
    # refresh ledger-derived memory (cheap; best-effort)
    refresh = {}
    for label, cmd in (("calibrate", ["scripts/calibrate.py"]),
                       ("research_memory", ["scripts/research_memory.py"])):
        ok, _o, err = _run(cmd, timeout=60)
        refresh[label] = "ok" if ok else f"failed: {(err or '')[-120:]}"
    return {"scored": scored, "skipped": skipped, "errors": errors, "memory_refresh": refresh}


# --------------------------------------------------------------- generate step
def generate_asset(asset, now, no_render):
    """Deterministic per-asset pipeline: intraday -> memory_pack -> [brief] -> scaffold
    -> confidence -> mvp_report. Returns a manifest job record (never raises)."""
    t0 = time.time()
    tk = asset["ticker"]
    rec = {"asset_id": asset["id"], "ticker": tk, "asset_class": asset["asset_class"],
           "report_id": None, "status": "error", "stages": {}, "errors": []}

    def stage(name, cmd, timeout=180):
        ok, out, err = _run(cmd, timeout=timeout)
        rec["stages"][name] = "ok" if ok else "failed"
        if not ok:
            rec["errors"].append({name: (err or out)[-240:]})
        return ok, out

    # 1. data + analysis
    icmd = ["scripts/intraday.py", asset["provider_symbols"]["yahoo"], "--name", tk,
            "--hrange", "10d", "--roll-utc", str(asset.get("roll_utc", 0))]
    if asset.get("related"):
        icmd += ["--related", asset["related"]]
    ok, _ = stage("intraday", icmd, timeout=120)
    if not ok:
        rec["status"] = "data_error"; rec["duration_s"] = round(time.time() - t0, 1); return rec

    # 2. bounded memory pack (for the brief writer / critic; written for audit)
    try:
        pack = mp.build_pack(asset, as_of=now)
        MEMPACK_DIR.mkdir(parents=True, exist_ok=True)
        (MEMPACK_DIR / f"{tk}_memory_pack.json").write_text(json.dumps(pack, indent=1), encoding="utf-8")
        rec["stages"]["memory_pack"] = "ok"
        rec["memory_pack_tokens"] = pack.get("budget", {}).get("approx_tokens")
    except Exception as ex:
        rec["stages"]["memory_pack"] = "failed"; rec["errors"].append({"memory_pack": str(ex)[:160]})

    # 3. brief — Phase 1: operator-written. Phase 2 wires brief_writer.py + critic.py here.
    brief = BRIEF_DIR / f"{tk}_research_brief.json"
    if not brief.exists():
        rec["status"] = "needs_brief"; rec["duration_s"] = round(time.time() - t0, 1); return rec

    # 4. scaffold (payload + predictions + deterministic confidence)
    ok, _ = stage("scaffold", ["scripts/scaffold_payload.py", tk,
                               "--session-profile", asset["session_profile"]])
    if not ok:
        rec["status"] = "scaffold_error"; rec["duration_s"] = round(time.time() - t0, 1); return rec

    # 5. render + QA gate (or forecast-only)
    payload = f"data/payloads/{tk}_af_payload.json"
    rcmd = ["scripts/mvp_report.py", payload] + (["--no-render"] if no_render else [])
    ok, out = stage("mvp_report", rcmd, timeout=240)
    try:
        rec["report_id"] = json.loads(Path(ROOT / payload).read_text(encoding="utf-8-sig")).get("report_id")
    except Exception:
        pass
    rec["status"] = ("generated" if not no_render else "forecast_only") if ok else "qa_failed"
    rec["duration_s"] = round(time.time() - t0, 1)
    return rec


def main():
    o = parse_args(sys.argv[1:])
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

    print(f"[{run_id}] mode={o['mode']} | selected={len(assets)} due={len(due_assets)} "
          f"({', '.join(a['id'] for a in due_assets) or 'none'})")

    if o["mode"] in ("score_only", "production"):
        print("scoring closed windows + refreshing memory...")
        manifest["score"] = score_step(now, {a["ticker"] for a in assets})
        s = manifest["score"]
        print(f"  scored={len(s['scored'])} skipped={len(s['skipped'])} errors={len(s['errors'])} "
              f"refresh={s['memory_refresh']}")

    if o["mode"] in ("generate_only", "production"):
        print(f"generating {len(due_assets)} due asset(s) with {o['workers']} worker(s)...")
        jobs = []
        with ThreadPoolExecutor(max_workers=max(1, o["workers"])) as pool:
            futs = {pool.submit(generate_asset, a, now, o["no_render"]): a for a in due_assets}
            for f in as_completed(futs):
                rec = f.result()
                jobs.append(rec)
                print(f"  {rec['ticker']:8} {rec['status']:14} "
                      f"{rec.get('report_id') or ''} ({rec.get('duration_s')}s)")
        manifest["jobs"] = sorted(jobs, key=lambda r: r["asset_id"])
        manifest["generated"] = sum(1 for j in jobs if j["status"] in ("generated", "forecast_only"))
        manifest["needs_brief"] = [j["ticker"] for j in jobs if j["status"] == "needs_brief"]

    # always write the manifest (dry_run writes the plan only)
    run_dir = ROOT / "runs" / run_date
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print(f"manifest -> runs/{run_date}/run_manifest.json")
    if o["mode"] == "dry_run":
        print("DRY RUN - no scoring, generation, or publish performed.")


if __name__ == "__main__":
    main()
