"""manifest.py — run-args scoping + run-manifest read/summary (split out of engine_ops)."""
import json
from datetime import datetime

from _paths import ROOT
from db import _utcnow

LOG_EXCERPT_BYTES = 24 * 1024           # last ~24KB of combined stdout/stderr (richer dashboard log)


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
