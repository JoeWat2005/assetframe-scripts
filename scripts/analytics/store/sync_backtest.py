"""sync_backtest.py — push the SANDBOX outcome ledger to the admin-only Neon backtest_results.

A sandbox backtest (run_daily --sandbox / engine_ops run_backtest) writes its graded
predictions ONLY to ledger/sim/outcome_ledger.csv — it never touches the live track record,
R2, or the published editions/scored_results. This script is the ONE bridge that surfaces those
sandbox results to the admin console: it reads ledger/sim/outcome_ledger.csv and UPSERTS each
data row into the pre-existing Neon table backtest_results (report_id is the primary key, so a
re-run that re-grades the same backdated report updates in place — fully idempotent).

It also reads the per-prediction sidecars score_report.py drops under data/predictions/sim/scored/
(one <report_id>.json list per scored sandbox report) and UPSERTS each prediction into the
pre-existing Neon table backtest_predictions ((report_id, pred_id) PK) so the admin console can show
the full graded prediction list. A re-sync PRESERVES any manual outcome the admin entered
(outcome = COALESCE(existing, sidecar)) — it never clobbers a hand-graded verdict.

It is intentionally narrow:
  * READS ledger/sim/outcome_ledger.csv (the sandbox ledger) and data/predictions/sim/scored/*.json
    (the per-prediction sidecars). If either is missing or empty, that half exits cleanly — a
    sandbox that produced nothing is not an error.
  * WRITES only backtest_results + backtest_predictions (admin-only). It NEVER writes
    editions/scored_results or any live table, and it does NOT create the tables (the migration
    owns the schema).
  * Reuses engine_ops.connect()/database_url() (and its .env loader) so DATABASE_URL is resolved
    EXACTLY like every other box-side write — same Neon URL, same fallback contract.

CLI: python scripts/sync_backtest.py        # upsert ledger/sim -> backtest_results
"""
import csv
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import engine_ops  # reuse its DATABASE_URL resolution + .env loader + connect()
from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)
SIM_LEDGER = ROOT / "ledger" / "sim" / "outcome_ledger.csv"
# score_report.py drops one data/predictions/sim/scored/<report_id>.json per scored sandbox report:
# a JSON list of {pred_id, ptype, ptext, manual, sort, outcome}. We upsert each entry into Neon
# backtest_predictions so the admin console can show the full per-prediction list.
SCORED_DIR = ROOT / "data" / "predictions" / "sim" / "scored"

# backtest_results columns we write, in the UPSERT order. report_id is the PK (ON CONFLICT key).
TABLE_COLS = ["report_id", "ticker", "instrument", "asset_class", "view", "confidence",
              "horizon", "window_end", "results", "hits", "misses", "hit_rate", "scored_at"]


def _int_or_none(val):
    """'' / None / garbage -> None; otherwise int (tolerates '2', '2.0')."""
    s = (val or "").strip()
    if s == "":
        return None
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _num_or_none(val):
    """'' / None / garbage -> None; otherwise float (for the numeric hit_rate column)."""
    s = (val or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _ticker_from_report_id(report_id):
    """ticker is the trailing '-' segment of the report_id (AF-YYYYMMDDHHMM-TICKER -> TICKER).
    Matches scaffold_payload's identity contract; rsplit so embedded '-' in the stamp is safe."""
    rid = (report_id or "").strip()
    return rid.rsplit("-", 1)[-1] if rid else ""


def map_row(row):
    """Map one outcome-ledger CSV row (DictReader, score_report.LEDGER_COLS) -> the ordered tuple
    of backtest_results values (TABLE_COLS order). Pure — no DB, so it is unit-testable in isolation.

    Returns None for a row with no report_id (the PK): such a row can't be upserted and is skipped."""
    report_id = (row.get("report_id") or "").strip()
    if not report_id:
        return None
    return (
        report_id,                                   # report_id  (PK)
        _ticker_from_report_id(report_id),           # ticker     = report_id.rsplit('-',1)[-1]
        (row.get("instrument") or "").strip(),       # instrument
        (row.get("asset_class") or "").strip(),      # asset_class
        (row.get("view") or "").strip(),             # view
        _int_or_none(row.get("confidence")),         # confidence (int, '' -> null)
        (row.get("horizon") or "").strip(),          # horizon
        (row.get("window_end_utc") or "").strip(),   # window_end
        (row.get("results") or "").strip(),          # results
        _int_or_none(row.get("hits")),               # hits  (int)
        _int_or_none(row.get("misses")),             # misses (int)
        _num_or_none(row.get("hit_rate_pct")),       # hit_rate (numeric, '' -> null)
        (row.get("scored_at_utc") or "").strip(),    # scored_at
    )


def read_sim_rows(path=SIM_LEDGER):
    """Read the sandbox ledger and return the list of mapped value-tuples (skipping any row with
    no report_id). Missing file -> []. utf-8-sig to match the ledger writer's BOM tolerance."""
    p = Path(path)
    if not p.exists():
        return []
    mapped = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = map_row(r)
            if t is not None:
                mapped.append(t)
    return mapped


_UPSERT_SQL = (
    "INSERT INTO backtest_results "
    "  (report_id, ticker, instrument, asset_class, view, confidence, horizon, "
    "   window_end, results, hits, misses, hit_rate, scored_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (report_id) DO UPDATE SET "
    "  ticker = excluded.ticker, instrument = excluded.instrument, "
    "  asset_class = excluded.asset_class, view = excluded.view, "
    "  confidence = excluded.confidence, horizon = excluded.horizon, "
    "  window_end = excluded.window_end, results = excluded.results, "
    "  hits = excluded.hits, misses = excluded.misses, "
    "  hit_rate = excluded.hit_rate, scored_at = excluded.scored_at")


# backtest_predictions columns we write, in the UPSERT order. (report_id, pred_id) is the PK.
PRED_COLS = ["report_id", "pred_id", "ptype", "ptext", "manual", "outcome", "sort"]

# UPSERT one prediction row. On a re-sync we refresh the editable shape fields (ptype/ptext/manual/
# sort) from the sidecar, but PRESERVE a manual outcome the admin entered: outcome stays whatever is
# already in the table unless it's NULL, in which case we take the sidecar's value (COALESCE old,new).
_PRED_UPSERT_SQL = (
    "INSERT INTO backtest_predictions "
    "  (report_id, pred_id, ptype, ptext, manual, outcome, sort) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
    "ON CONFLICT (report_id, pred_id) DO UPDATE SET "
    "  ptype = excluded.ptype, ptext = excluded.ptext, "
    "  manual = excluded.manual, sort = excluded.sort, "
    "  outcome = COALESCE(backtest_predictions.outcome, excluded.outcome)")


def _bool_or_false(val):
    """JSON true/'true'/1 -> True; everything else -> False (the `manual` column is NOT NULL-ish)."""
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("true", "1", "yes")


def _int_or_zero(val):
    """sort -> int; missing/garbage -> 0 so a malformed sidecar entry still upserts deterministically."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def map_pred(report_id, entry):
    """Map one sidecar entry -> the ordered backtest_predictions value-tuple (PRED_COLS order). Pure.
    Returns None if it has no pred_id (the second half of the PK) — such an entry can't be upserted."""
    pred_id = (entry.get("pred_id") or "")
    pred_id = str(pred_id).strip() if pred_id is not None else ""
    if not pred_id:
        return None
    outcome = entry.get("outcome")
    outcome = str(outcome).strip() if isinstance(outcome, str) else outcome
    return (
        report_id,                                   # report_id (PK 1)
        pred_id,                                     # pred_id   (PK 2)
        (entry.get("ptype") or ""),                  # ptype
        (entry.get("ptext") or ""),                  # ptext
        _bool_or_false(entry.get("manual")),         # manual
        outcome,                                     # outcome (Y|N|NT|MANUAL|null)
        _int_or_zero(entry.get("sort")),             # sort
    )


def read_pred_rows(scored_dir=SCORED_DIR):
    """Read every scored/<report_id>.json sidecar and return the list of mapped value-tuples
    (skipping any entry with no pred_id). Missing/empty dir -> []. report_id comes from the filename
    stem so it's authoritative even if a sidecar entry omits it. A malformed file is skipped, never
    fatal — one bad sidecar must not block the rest of the sync."""
    d = Path(scored_dir)
    if not d.is_dir():
        return []
    mapped = []
    for fp in sorted(d.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        report_id = fp.stem
        for entry in data:
            if not isinstance(entry, dict):
                continue
            t = map_pred(report_id, entry)
            if t is not None:
                mapped.append(t)
    return mapped


def sync(path=SIM_LEDGER):
    """Read ledger/sim and UPSERT every data row into Neon backtest_results. Returns the count of
    rows upserted. Empty/missing sim ledger -> 0 (caller prints the clean 'nothing to sync' note)."""
    rows = read_sim_rows(path)
    if not rows:
        return 0
    with engine_ops.connect() as conn:
        for values in rows:
            conn.execute(_UPSERT_SQL, values)
    return len(rows)


def sync_predictions(scored_dir=SCORED_DIR):
    """Read every scored/*.json sidecar and UPSERT each entry into Neon backtest_predictions.
    Returns the count of prediction rows upserted. Empty/missing scored/ dir -> 0 (clean no-op,
    no DB connection opened). A manual outcome the admin already entered is preserved by the
    COALESCE in _PRED_UPSERT_SQL — a re-sync never clobbers it."""
    rows = read_pred_rows(scored_dir)
    if not rows:
        return 0
    with engine_ops.connect() as conn:
        for values in rows:
            conn.execute(_PRED_UPSERT_SQL, values)
    return len(rows)


def main(argv=None):
    if not SIM_LEDGER.exists():
        print("no sandbox results to sync (ledger/sim/outcome_ledger.csv not found)")
    else:
        n = sync()
        if n == 0:
            print("no sandbox results to sync (ledger/sim is empty)")
        else:
            print(f"synced {n} sandbox row(s) -> backtest_results")
    # Per-prediction sidecars are independent of the ledger: sync them too (clean no-op if the
    # scored/ dir is empty/absent — a backtest that produced no sidecars is not an error).
    np = sync_predictions()
    if np == 0:
        print("no per-prediction detail to sync (data/predictions/sim/scored is empty)")
    else:
        print(f"synced {np} prediction row(s) -> backtest_predictions")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
