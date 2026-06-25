"""Rebuildable SQLite MIRROR of the outcome ledger — fast queries without changing the
source of truth.

ledger/outcome_ledger.csv stays the append-only, human-readable source of truth (the engine
still writes ONLY to the CSV; see score_report.py). This module derives ledger/engine.sqlite — the
single local engine DB — from it for cheap indexed analytics (per-instrument / per-pred-type /
per-regime roll-ups, the admin dashboard, ad-hoc SQL) where scanning a growing CSV in Python would
get slow. The same DB also holds append-only engine-history tables (runs, calibration_history,
asset_cache) so growing run/calibration history lives in a DB rather than loose JSON.

Design guarantees (why this is safe):
  * Mirror only. The .sqlite is DROP+CREATE rebuilt from the CSV every time, so it can never
    diverge: lose it, corrupt it, delete it — `rebuild()` reproduces it exactly. It is a derived
    product (gitignored), like calibration_map.json.
  * No writer changes. Nothing in the scoring path reads the mirror, so a mirror failure can
    never block a run (run_daily calls it best-effort, after scoring).
  * Numeric coercion is lossless-or-NULL: an unparseable cell becomes NULL and is counted, never
    crashes the rebuild.

CLI:
  python scripts/ledger_db.py rebuild                 # (re)build the mirror from the CSV
  python scripts/ledger_db.py stats                   # quick row/instrument summary
  python scripts/ledger_db.py query "SELECT ..."      # run read-only SQL, print rows as JSON
"""
import csv
import json
import sqlite3
import sys
from pathlib import Path

DEFAULT_CSV = Path("ledger/outcome_ledger.csv")
# The single local engine DB (gitignored, derived): the ledger mirror PLUS append-only history
# tables (runs, calibration_history, asset_cache). The CSV stays the canonical ledger writer.
DEFAULT_DB = Path("ledger/engine.sqlite")

# Mirrors score_report.LEDGER_COLS exactly (first 13 original + 7 taxonomy). Order is the CSV
# header order. (col_name, sqlite_type). REAL/INTEGER columns are coerced; bad cells -> NULL.
COLUMNS = [
    ("scored_at_utc", "TEXT"), ("report_id", "TEXT"), ("instrument", "TEXT"), ("view", "TEXT"),
    ("confidence", "REAL"), ("window_end_utc", "TEXT"), ("results", "TEXT"),
    ("hits", "INTEGER"), ("misses", "INTEGER"), ("hit_rate_pct", "REAL"),
    ("setup_filled", "TEXT"), ("setup_outcome", "TEXT"), ("partial", "TEXT"),
    ("conf_version", "TEXT"), ("conf_raw", "REAL"), ("asset_class", "TEXT"),
    ("pred_type", "TEXT"), ("direction", "TEXT"), ("horizon", "TEXT"), ("market_regime", "TEXT"),
]
_INDEXES = ["report_id", "instrument", "asset_class", "pred_type", "horizon", "window_end_utc"]


def ensure_aux_tables(con):
    """Create the append-only engine-history tables if absent (idempotent). These live alongside
    the ledger mirror in engine.sqlite; rebuild() never drops them (it only DROP+CREATEs `ledger`)."""
    con.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, mode TEXT, run_date TEXT, "
                "status TEXT, generated INTEGER, manifest_json TEXT, recorded_at TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS calibration_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "fitted_at TEXT, conf_version TEXT, n_rows INTEGER, map_json TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS asset_cache (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "synced_at TEXT, n_assets INTEGER, assets_json TEXT)")


def record_run(run_id, mode, run_date, status, generated=None, manifest=None, db_path=DEFAULT_DB):
    """Append/replace a run-history row in engine.sqlite. Best-effort: never raises (an audit
    table must never block or fail a run). Returns True on success."""
    try:
        con = sqlite3.connect(str(Path(db_path)))
        try:
            ensure_aux_tables(con)
            con.execute(
                "INSERT OR REPLACE INTO runs (run_id, mode, run_date, status, generated, manifest_json, "
                "recorded_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                (run_id, mode, run_date, status, generated,
                 json.dumps(manifest) if manifest is not None else None))
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def record_calibration(conf_version, n_rows, calibration_map, db_path=DEFAULT_DB):
    """Append a calibration snapshot to engine.sqlite (history/audit). Best-effort; never raises."""
    try:
        con = sqlite3.connect(str(Path(db_path)))
        try:
            ensure_aux_tables(con)
            con.execute(
                "INSERT INTO calibration_history (fitted_at, conf_version, n_rows, map_json) "
                "VALUES (datetime('now'), ?, ?, ?)",
                (str(conf_version), int(n_rows or 0),
                 json.dumps(calibration_map) if calibration_map is not None else None))
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def cache_assets(assets, db_path=DEFAULT_DB):
    """Store the last-synced asset universe snapshot in engine.sqlite (diagnostics only — NOT the
    source of truth, which stays Neon engine_assets / config/assets.json). Best-effort; never raises."""
    try:
        con = sqlite3.connect(str(Path(db_path)))
        try:
            ensure_aux_tables(con)
            con.execute(
                "INSERT INTO asset_cache (id, synced_at, n_assets, assets_json) "
                "VALUES (1, datetime('now'), ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "synced_at=excluded.synced_at, n_assets=excluded.n_assets, assets_json=excluded.assets_json",
                (len(assets or []), json.dumps(assets or [])))
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def _coerce(sqltype, val):
    s = (val or "").strip()
    if s == "":
        return None
    if sqltype == "INTEGER":
        try:
            return int(float(s))
        except ValueError:
            return None
    if sqltype == "REAL":
        try:
            return float(s)
        except ValueError:
            return None
    return s


def rebuild(csv_path=DEFAULT_CSV, db_path=DEFAULT_DB):
    """DROP+CREATE the mirror table from the CSV. Returns a summary dict. Never edits the CSV."""
    csv_path, db_path = Path(csv_path), Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    rows, bad_cells = [], 0
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                rec = []
                for name, sqltype in COLUMNS:
                    raw = r.get(name)
                    v = _coerce(sqltype, raw)
                    if v is None and sqltype in ("INTEGER", "REAL") and (raw or "").strip() not in ("", None):
                        bad_cells += 1
                    rec.append(v)
                rows.append(rec)
    cols_ddl = ", ".join(
        f'"{n}" {t}' + (" PRIMARY KEY" if n == "report_id" else "") for n, t in COLUMNS)
    placeholders = ", ".join("?" for _ in COLUMNS)
    con = sqlite3.connect(str(db_path))
    try:
        cur = con.cursor()
        cur.execute("DROP TABLE IF EXISTS ledger")
        cur.execute(f"CREATE TABLE ledger ({cols_ddl})")
        # INSERT OR IGNORE so a (malformed) duplicate report_id can't abort the rebuild.
        cur.executemany(f"INSERT OR IGNORE INTO ledger VALUES ({placeholders})", rows)
        for col in _INDEXES:
            if col == "report_id":
                continue  # already the PK
            cur.execute(f'CREATE INDEX IF NOT EXISTS idx_ledger_{col} ON ledger("{col}")')
        ensure_aux_tables(con)            # keep the engine-history tables alongside the mirror
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
    finally:
        con.close()
    return {"db": str(db_path), "csv_rows": len(rows), "db_rows": n,
            "duplicates_dropped": len(rows) - n, "bad_numeric_cells": bad_cells}


def connect(db_path=DEFAULT_DB):
    """Open the mirror read-only-ish with dict rows. Rebuild first if it's missing/stale."""
    db_path = Path(db_path)
    if not db_path.exists():
        rebuild(db_path=db_path)
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def _stats(db_path=DEFAULT_DB):
    con = connect(db_path)
    try:
        total = con.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"]
        by_inst = con.execute(
            "SELECT instrument, COUNT(*) n, ROUND(AVG(hit_rate_pct),1) avg_hit "
            "FROM ledger GROUP BY instrument ORDER BY n DESC").fetchall()
        return {"total_rows": total,
                "by_instrument": [dict(r) for r in by_inst]}
    finally:
        con.close()


def main(argv):
    cmd = argv[0] if argv else "rebuild"
    if cmd == "rebuild":
        print(json.dumps(rebuild(), indent=1))
    elif cmd == "stats":
        print(json.dumps(_stats(), indent=1))
    elif cmd == "query":
        if len(argv) < 2:
            print("usage: ledger_db.py query \"SELECT ...\"", file=sys.stderr)
            return 2
        sql = argv[1]
        low = sql.lstrip().lower()
        if not (low.startswith("select") or low.startswith("with")):
            print("only read-only SELECT/WITH queries are allowed on the mirror", file=sys.stderr)
            return 2
        con = connect()
        try:
            rows = [dict(r) for r in con.execute(sql).fetchall()]
        finally:
            con.close()
        print(json.dumps(rows, indent=1, default=str))
    else:
        print(f"unknown command {cmd!r} (rebuild|stats|query)", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
