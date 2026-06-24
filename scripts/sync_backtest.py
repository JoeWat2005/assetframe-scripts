"""sync_backtest.py — push the SANDBOX outcome ledger to the admin-only Neon backtest_results.

A sandbox backtest (run_daily --sandbox / engine_ops run_backtest) writes its graded
predictions ONLY to ledger/sim/outcome_ledger.csv — it never touches the live track record,
R2, or the published editions/scored_results. This script is the ONE bridge that surfaces those
sandbox results to the admin console: it reads ledger/sim/outcome_ledger.csv and UPSERTS each
data row into the pre-existing Neon table backtest_results (report_id is the primary key, so a
re-run that re-grades the same backdated report updates in place — fully idempotent).

It is intentionally narrow:
  * READS ledger/sim/outcome_ledger.csv only (the sandbox ledger). If it is missing or has no
    data rows, this exits 0 cleanly ("no sandbox results to sync") — a sandbox that produced
    nothing is not an error.
  * WRITES only backtest_results (admin-only). It NEVER writes editions/scored_results or any
    live table, and it does NOT create the table (the migration owns the schema).
  * Reuses engine_ops.connect()/database_url() (and its .env loader) so DATABASE_URL is resolved
    EXACTLY like every other box-side write — same Neon URL, same fallback contract.

CLI: python scripts/sync_backtest.py        # upsert ledger/sim -> backtest_results
"""
import csv
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import engine_ops  # reuse its DATABASE_URL resolution + .env loader + connect()

ROOT = HERE.parent
SIM_LEDGER = ROOT / "ledger" / "sim" / "outcome_ledger.csv"

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


def main(argv=None):
    if not SIM_LEDGER.exists():
        print("no sandbox results to sync (ledger/sim/outcome_ledger.csv not found)")
        return 0
    n = sync()
    if n == 0:
        print("no sandbox results to sync (ledger/sim is empty)")
        return 0
    print(f"synced {n} sandbox row(s) -> backtest_results")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
