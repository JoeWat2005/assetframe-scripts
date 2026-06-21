"""Rebuildable SQLite MIRROR of the outcome ledger — fast queries without changing the
source of truth.

ledger/outcome_ledger.csv stays the append-only, human-readable source of truth (the engine
still writes ONLY to the CSV; see score_report.py). This module derives ledger/outcome_ledger.sqlite
from it for cheap indexed analytics (per-instrument / per-pred-type / per-regime roll-ups, the
admin dashboard, ad-hoc SQL) where scanning a growing CSV in Python would get slow.

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
DEFAULT_DB = Path("ledger/outcome_ledger.sqlite")

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
