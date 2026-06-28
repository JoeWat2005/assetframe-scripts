"""Bridge: Python report pipeline -> the Next.js app.

Writes the catalog + track-record as JSON the web app reads. Report files are NOT copied
into the web app: every file (free Snapshots AND Pro reports) is private in R2 (pushed by
scripts/publish.py) and served only through the auth-gated /api/report route.

Usage:
  python scripts/export_content.py [--web web] [--include-dev]

Outputs:
  web/content/catalog.json        list of editions (metadata + /api/report asset paths)
  web/content/track-record.json   { stats, open[], scored[], calibration }
"""
import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)

# Shared taxonomy (asset-class normalization). Invoked as `python -m scripts.delivery.export_content`;
# the package shim already exposes the sibling modules. Fall back to a pass-through so a missing
# module can never break the export.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from taxonomy import ASSET_CLASS_KEYS
except Exception:                       # pragma: no cover - standalone fallback
    ASSET_CLASS_KEYS = ("equity", "crypto", "fx", "futures", "index", "commodity")


def _publish_policy_by_ticker():
    """Map UPPER(ticker) -> publish_policy from config/assets.json. Editions match their
    asset by ticker (the report slug/metadata ticker is the asset's ticker, e.g. "GBPUSD").
    Prefer config_loader (validated + normalized defaults); fall back to raw JSON so a
    transient validation hiccup can't break the export — an unknown ticker is then treated
    as approval_required by the caller (fail safe, never auto-expose)."""
    out = {}
    cfg = ROOT / "config" / "assets.json"
    try:
        import config_loader
        assets = config_loader.load_assets(cfg)
    except Exception:                       # pragma: no cover - standalone fallback
        try:
            raw = json.loads(cfg.read_text(encoding="utf-8-sig"))
            assets = raw.get("assets", raw) if isinstance(raw, dict) else raw
        except Exception:
            assets = []
    for a in assets or []:
        tkr = (a.get("ticker") or "").strip().upper()
        if tkr:
            out[tkr] = a.get("publish_policy", "approval_required")
    return out


def _norm_asset_class(value):
    """Best-effort map a free-text/ledger asset_class onto a taxonomy key, else "".
    The ledger already stores taxonomy keys in `asset_class`; this only guards typos
    and the empty-ledger case (never raises)."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v in ASSET_CLASS_KEYS:
        return v
    # Coarse aliasing so older free-text rows still bucket.
    alias = {"equities": "equity", "stock": "equity", "stocks": "equity", "forex": "fx",
             "indices": "index", "commodities": "commodity", "future": "futures"}
    return alias.get(v, v)


def cadence_of(report_id):
    """Derive the scoring cadence from the report_id period stamp (the segment between the leading
    'AF' and the trailing ticker): AF-YYYYWww -> weekly, AF-YYYYMM (6 digits) -> monthly, else daily
    (AF-YYYYMMDD / AF-YYYYMMDDHHMM). Mirror of content.ts cadenceOf()."""
    parts = (report_id or "").split("-")
    stamp = parts[1] if len(parts) >= 2 else ""
    if "W" in stamp.upper():
        return "weekly"
    if stamp.isdigit() and len(stamp) == 6:
        return "monthly"
    return "daily"


def _parse_results(packed):
    """'P1=Y P2=N P3=NT' -> {'P1': 'Y', 'P2': 'N', 'P3': 'NT'}. Tolerant of blanks."""
    out = {}
    for tok in (packed or "").split():
        k, sep, val = tok.partition("=")
        if sep and k:
            out[k.strip()] = val.strip()
    return out


def _agg_rows(rows, key_fn):
    """Group ledger rows by key_fn(row) -> aggregate hits/misses into a stable list.
    Each entry: {key, reportsScored, hits, misses, hitRate}. Skips empty keys."""
    groups = defaultdict(lambda: {"reportsScored": 0, "hits": 0, "misses": 0})
    for r in rows:
        key = key_fn(r)
        if not key:
            continue
        g = groups[key]
        g["reportsScored"] += 1
        g["hits"] += int(r.get("hits", 0) or 0)
        g["misses"] += int(r.get("misses", 0) or 0)
    out = []
    for key, g in groups.items():
        graded = g["hits"] + g["misses"]
        out.append({"key": key, "reportsScored": g["reportsScored"], "hits": g["hits"],
                    "misses": g["misses"],
                    "hitRate": round(100 * g["hits"] / graded, 1) if graded else None})
    out.sort(key=lambda e: (-(e["hitRate"] if e["hitRate"] is not None else -1),
                            -e["reportsScored"], e["key"]))
    return out


def load_catalog(reports_dir, include_dev, since=None):
    # Approval gate: an edition lands hidden=true unless its asset opts into publish_policy
    # "auto". Match by ticker (fall back to slug); an asset the config doesn't know stays
    # hidden (fail safe — never auto-expose an unknown).
    policy_by_ticker = _publish_policy_by_ticker()
    editions = []
    for meta_path in sorted(reports_dir.glob("*/*/metadata.json")):
        date, slug = meta_path.parent.parent.name, meta_path.parent.name
        if not include_dev and date.startswith("_"):
            continue
        if since and date < since:          # scope out stale/old editions (republish guard)
            continue
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = meta_path.parent
        base = f"/api/report/{date}/{slug}"
        ticker = m.get("ticker", "")
        policy = policy_by_ticker.get((ticker or slug).strip().upper(), "approval_required")
        editions.append({
            "date": date, "slug": slug,
            "instrument": m.get("instrument", slug),
            "ticker": m.get("ticker", ""),
            "assetClass": m.get("asset_class", ""),
            "status": m.get("status", ""),
            "risk": m.get("risk_rating", ""),
            "bias": m.get("primary_bias") or m.get("research_view", ""),
            "lastPrice": m.get("last_price", ""),
            "dataQuality": m.get("data_quality_score", ""),
            "windowEnd": m.get("prediction_window_end_report_tz", ""),
            "reportDate": m.get("report_date", date),
            "catalystStatus": m.get("catalyst_status", ""),
            # report_id (the cadence-aware ledger/edition key) + cadence/intervals for display + joins
            "reportId": m.get("report_id", ""),
            "scoredCadence": m.get("scored_cadence") or cadence_of(m.get("report_id")),
            "chartIntervals": m.get("chart_intervals") or [],
            "forecastWindow": m.get("forecast_window", ""),
            # data-source provenance (for the report-page licensing badge + commercial-mode audit)
            "dataProvider": m.get("data_provider", ""),
            "dataLicense": m.get("data_license_mode", "personal"),
            "dataLicenseDegraded": bool(m.get("data_license_degraded")),
            # Approval gate: hidden until an admin un-hides (auto-publish assets ship visible).
            "hidden": policy != "auto",
            "freeHtml": f"{base}/free.html",
            "freePdf": f"{base}/free.pdf",
            "preview": f"{base}/preview.png",
            "hasPro": (d / "pro.html").exists(),
            "_dir": d,
        })
    editions.sort(key=lambda e: (e["date"], e["slug"]), reverse=True)
    return editions


def _build_aggregates(rows):
    """Derived track-record analytics from the scored ledger rows (additive; an empty
    ledger yields empty arrays / empty object). All counts come from the per-report
    hits/misses already graded into the ledger — no new scoring here."""
    if not rows:
        return {"byInstrument": [], "byAssetClass": [], "byPredictionType": [],
                "byRegime": [], "byCadence": [], "timeline": [], "calibrationCurve": [],
                "componentVsOutcome": []}

    # byInstrument carries ticker + normalized assetClass; the others are flat groupings.
    inst = defaultdict(lambda: {"ticker": "", "assetClass": "", "reportsScored": 0,
                                "hits": 0, "misses": 0})
    for r in rows:
        name = (r.get("instrument") or "").strip()
        if not name:
            continue
        g = inst[name]
        g["reportsScored"] += 1
        g["hits"] += int(r.get("hits", 0) or 0)
        g["misses"] += int(r.get("misses", 0) or 0)
        ac = _norm_asset_class(r.get("asset_class"))
        if ac and not g["assetClass"]:
            g["assetClass"] = ac
    by_instrument = []
    for name, g in inst.items():
        graded = g["hits"] + g["misses"]
        by_instrument.append({
            "instrument": name, "ticker": g["ticker"], "assetClass": g["assetClass"],
            "reportsScored": g["reportsScored"], "hits": g["hits"], "misses": g["misses"],
            "hitRate": round(100 * g["hits"] / graded, 1) if graded else None})
    by_instrument.sort(key=lambda e: (-(e["hitRate"] if e["hitRate"] is not None else -1),
                                      -e["reportsScored"], e["instrument"]))

    def _rename(entries, label):
        return [{label: e["key"], "reportsScored": e["reportsScored"], "hits": e["hits"],
                 "misses": e["misses"], "hitRate": e["hitRate"]} for e in entries]

    by_asset_class = _rename(_agg_rows(rows, lambda r: _norm_asset_class(r.get("asset_class"))),
                             "assetClass")
    by_pred_type = _rename(_agg_rows(rows, lambda r: (r.get("pred_type") or "").strip()),
                           "predType")
    by_regime = _rename(_agg_rows(rows, lambda r: (r.get("market_regime") or "").strip()),
                        "regime")
    by_cadence = _rename(_agg_rows(rows, lambda r: cadence_of(r.get("report_id"))), "cadence")

    # timeline: chronological (by window_end), cumulative + per-report hit rate.
    timeline = []
    ordered = sorted(rows, key=lambda r: (r.get("window_end_utc") or "", r.get("report_id") or ""))
    cum_h = cum_m = 0
    for r in ordered:
        h, m = int(r.get("hits", 0) or 0), int(r.get("misses", 0) or 0)
        cum_h += h
        cum_m += m
        cg, pg = cum_h + cum_m, h + m
        timeline.append({
            "reportId": r.get("report_id", ""), "instrument": r.get("instrument", ""),
            "windowEnd": r.get("window_end_utc", ""),
            "perReportHitRate": round(100 * h / pg, 1) if pg else None,
            "cumulativeHitRate": round(100 * cum_h / cg, 1) if cg else None})

    # calibrationCurve: finer than the 3 display buckets (10-point confidence bins),
    # gated to overall n>=10 like the coarse calibration. Each bin reports realised hit
    # rate (predictions hit / graded) and the report count.
    calibration_curve = []
    if len(rows) >= 10:
        bins = defaultdict(lambda: {"reports": 0, "hits": 0, "misses": 0})
        for r in rows:
            try:
                c = float(r.get("confidence"))
            except (TypeError, ValueError):
                continue
            lo = max(0, min(90, int(c // 10) * 10))
            b = bins[lo]
            b["reports"] += 1
            b["hits"] += int(r.get("hits", 0) or 0)
            b["misses"] += int(r.get("misses", 0) or 0)
        for lo in sorted(bins):
            b = bins[lo]
            graded = b["hits"] + b["misses"]
            calibration_curve.append({
                "bucket": f"{lo}-{lo + 9}", "confLo": lo, "confHi": lo + 9,
                "reports": b["reports"], "hits": b["hits"], "misses": b["misses"],
                "hitRate": round(100 * b["hits"] / graded, 1) if graded else None})

    # componentVsOutcome: group by confidence band (the existing `confidence` column, no
    # component columns required) and report realised hit rate vs the band's mean stated
    # confidence — surfaces over/under-confidence. Uses taxonomy's display bands.
    band_order = ["Low", "Moderate", "Elevated", "High"]

    def _band(score):
        try:
            s = float(score)
        except (TypeError, ValueError):
            return None
        if s < 50:
            return "Low"
        if s < 65:
            return "Moderate"
        if s < 80:
            return "Elevated"
        return "High"

    cb = defaultdict(lambda: {"reports": 0, "hits": 0, "misses": 0, "confSum": 0.0, "confN": 0})
    for r in rows:
        band = _band(r.get("confidence"))
        if band is None:
            continue
        g = cb[band]
        g["reports"] += 1
        g["hits"] += int(r.get("hits", 0) or 0)
        g["misses"] += int(r.get("misses", 0) or 0)
        try:
            g["confSum"] += float(r.get("confidence"))
            g["confN"] += 1
        except (TypeError, ValueError):
            pass
    component_vs_outcome = []
    for band in band_order:
        if band not in cb:
            continue
        g = cb[band]
        graded = g["hits"] + g["misses"]
        component_vs_outcome.append({
            "band": band, "reports": g["reports"],
            "avgConfidence": round(g["confSum"] / g["confN"], 1) if g["confN"] else None,
            "hitRate": round(100 * g["hits"] / graded, 1) if graded else None})

    return {"byInstrument": by_instrument, "byAssetClass": by_asset_class,
            "byPredictionType": by_pred_type, "byRegime": by_regime, "byCadence": by_cadence,
            "timeline": timeline, "calibrationCurve": calibration_curve,
            "componentVsOutcome": component_vs_outcome}


def load_track_record(ledger_csv, pred_dir, scored_ids):
    scored, calib = [], None
    hits_by_id = {}
    rows = []
    # verdict map: report_id -> {pred_id -> 'Y'|'N'|'NT'|...}, parsed from the packed
    # `results` string so per-call outcomes need no migration / extra ledger columns.
    verdicts_by_id = {}
    if ledger_csv.exists() and ledger_csv.stat().st_size > 0:
        rows = list(csv.DictReader(ledger_csv.open(encoding="utf-8")))
        for r in rows:
            rid = r.get("report_id", "")
            if rid:
                hits_by_id[rid] = int(r.get("hits", 0) or 0)
                verdicts_by_id[rid] = _parse_results(r.get("results", ""))
            scored.append({
                "reportId": r.get("report_id", ""),
                "instrument": r.get("instrument", ""), "view": r.get("view", ""),
                "confidence": r.get("confidence", ""), "results": r.get("results", ""),
                "hits": r.get("hits", ""), "misses": r.get("misses", ""),
                "hitRate": r.get("hit_rate_pct", ""), "windowEnd": r.get("window_end_utc", ""),
                # Normalized taxonomy fields (carried where the ledger has them) so the DB
                # path and JSON fallback expose the same shape.
                "assetClass": _norm_asset_class(r.get("asset_class")),
                "predType": r.get("pred_type", ""),
                "scoredCadence": cadence_of(r.get("report_id")),
            })
        if len(rows) >= 10:
            buckets = {"<=60": [], "61-75": [], ">75": []}
            for r in rows:
                try:
                    c, hr = float(r["confidence"]), float(r["hit_rate_pct"])
                except (ValueError, KeyError, TypeError):
                    continue                  # TypeError: a truncated final ledger row yields None
                buckets["<=60" if c <= 60 else ("61-75" if c <= 75 else ">75")].append(hr)
            calib = {k: {"hitRate": round(sum(v) / len(v), 1) if v else None, "n": len(v)}
                     for k, v in buckets.items()}

    open_calls = []
    for pf in sorted(pred_dir.glob("*_predictions.json")):
        try:
            p = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        # Keep scored reports in the list too — their tracker flips from 0/n to hits/n.
        preds = p.get("predictions", [])
        rid = p.get("report_id", pf.stem)
        verdicts = verdicts_by_id.get(rid, {})
        tax = p.get("taxonomy") or {}
        sub = [{
            "id": x.get("id", ""),
            "type": x.get("type", ""),
            "text": x.get("text") or x.get("note") or "",
            "manual": x.get("type") == "manual",
            # Force bool-or-null so this matches sync-db's coercion (DB == JSON fallback).
            "expect": x.get("expect") if isinstance(x.get("expect"), bool) else None,
            # Per-prediction verdict merged from the ledger's packed results, "" until scored.
            "verdict": verdicts.get(x.get("id", ""), ""),
            # Edition-level prediction archetype (one per report); useful in the UI rows.
            "predType": tax.get("prediction_type", ""),
        } for x in preds]
        open_calls.append({
            "reportId": rid, "instrument": p.get("instrument", ""),
            "symbol": p.get("symbol", ""), "view": p.get("view", ""),
            "confidence": p.get("confidence", ""), "windowEnd": p.get("window_end_utc", ""),
            "n": len(preds), "nManual": sum(1 for x in preds if x.get("type") == "manual"),
            "hits": hits_by_id.get(rid, 0), "scored": rid in scored_ids,
            "predictions": sub,
        })
    open_calls.sort(key=lambda c: c["windowEnd"])
    aggregates = _build_aggregates(rows)
    return scored, open_calls, calib, aggregates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", default=".")   # split repo: content/ lives at the repo root, not web/
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--include-dev", action="store_true")
    ap.add_argument("--since", default=None,
                    help="only include editions dated >= YYYY-MM-DD (scopes out stale/old reports)")
    a = ap.parse_args()

    web = (ROOT / a.web) if not Path(a.web).is_absolute() else Path(a.web)
    reports_dir = (ROOT / a.reports) if not Path(a.reports).is_absolute() else Path(a.reports)
    content = web / "content"
    content.mkdir(parents=True, exist_ok=True)

    catalog = load_catalog(reports_dir, a.include_dev, since=a.since)

    # ledger stats from the raw CSV (hits/misses) for the headline numbers
    ledger_csv = ROOT / "ledger" / "outcome_ledger.csv"
    scored_ids = set()
    hits = misses = total = 0
    if ledger_csv.exists() and ledger_csv.stat().st_size > 0:
        for r in csv.DictReader(ledger_csv.open(encoding="utf-8")):
            total += 1
            scored_ids.add(r.get("report_id"))
            hits += int(r.get("hits", 0) or 0)
            misses += int(r.get("misses", 0) or 0)

    scored, open_calls, calib, aggregates = load_track_record(
        ledger_csv, ROOT / "data" / "predictions", scored_ids)

    # Backfill byInstrument tickers + assetClass from the open calls / catalog (the ledger
    # carries neither the symbol nor a guaranteed asset_class). Match on instrument name.
    ticker_by_name = {c["instrument"]: c.get("symbol", "") for c in open_calls if c.get("instrument")}
    asset_by_name = {}
    for e in catalog:
        if e.get("instrument"):
            asset_by_name.setdefault(e["instrument"], _norm_asset_class(e.get("assetClass")))
    for row in aggregates.get("byInstrument", []):
        if not row.get("ticker"):
            row["ticker"] = ticker_by_name.get(row["instrument"], "")
        if not row.get("assetClass"):
            row["assetClass"] = asset_by_name.get(row["instrument"], "")

    graded = hits + misses
    track = {
        "stats": {
            "reportsScored": total, "openCalls": len(open_calls),
            "predictionsGraded": graded,
            "hitRate": round(100 * hits / graded, 1) if graded else None,
        },
        "open": open_calls, "scored": scored, "calibration": calib,
        # Derived analytics (Task T12). Additive — readers tolerate their absence.
        "byInstrument": aggregates["byInstrument"],
        "byAssetClass": aggregates["byAssetClass"],
        "byPredictionType": aggregates["byPredictionType"],
        "byRegime": aggregates["byRegime"],
        "byCadence": aggregates["byCadence"],
        "timeline": aggregates["timeline"],
        "calibrationCurve": aggregates["calibrationCurve"],
        "componentVsOutcome": aggregates["componentVsOutcome"],
    }

    # Report files live in private R2 (run scripts/publish.py), not in the web app.
    for e in catalog:
        del e["_dir"]

    (content / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    (content / "track-record.json").write_text(json.dumps(track, indent=2), encoding="utf-8")

    print(f"Exported -> {content}")
    print(f"  editions:    {len(catalog)}")
    print(f"  open calls:  {len(open_calls)}")
    print(f"  scored rows: {total}" + ("  (calibration ready)" if calib else ""))
    print("All report files (free + Pro) are private in R2 - run scripts/publish.py to push them.")


if __name__ == "__main__":
    main()
