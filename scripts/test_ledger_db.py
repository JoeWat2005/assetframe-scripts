"""Tests for ledger_db.py — the rebuildable SQLite mirror of outcome_ledger.csv.

Offline + temp-dir only: never touches the real ledger. Asserts the mirror faithfully
reflects the CSV, is idempotent (rebuild == rebuild), coerces numerics, and never mutates
the source CSV.

Run:  python scripts/test_ledger_db.py
"""
import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_db as L


def _write_csv(path, rows):
    cols = [c for c, _ in L.COLUMNS]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _row(rid, instrument, hits, misses, hit_rate, **extra):
    base = {"report_id": rid, "instrument": instrument, "hits": hits, "misses": misses,
            "hit_rate_pct": hit_rate, "asset_class": "fx", "pred_type": "range_hold",
            "horizon": "next_session", "window_end_utc": "2026-06-16 21:00",
            "conf_version": "2", "conf_raw": "60.0", "confidence": "60"}
    base.update(extra)
    return base


class TestLedgerDB(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "outcome_ledger.csv"
        self.db = self.tmp / "outcome_ledger.sqlite"

    def test_rebuild_reflects_csv(self):
        _write_csv(self.csv, [_row("AF-1", "GBP/USD", 2, 3, 40.0),
                              _row("AF-2", "BTC/USD", 4, 1, 80.0, asset_class="crypto")])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["csv_rows"], 2)
        self.assertEqual(summ["db_rows"], 2)
        con = L.connect(self.db)
        try:
            avg = con.execute("SELECT ROUND(AVG(hit_rate_pct),1) a FROM ledger").fetchone()["a"]
            crypto = con.execute("SELECT COUNT(*) n FROM ledger WHERE asset_class='crypto'").fetchone()["n"]
        finally:
            con.close()
        self.assertEqual(avg, 60.0)
        self.assertEqual(crypto, 1)

    def test_numeric_coercion_and_bad_cells(self):
        _write_csv(self.csv, [_row("AF-1", "GBP/USD", "notanint", 3, "", conf_raw="oops")])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["db_rows"], 1)
        self.assertGreaterEqual(summ["bad_numeric_cells"], 2)  # hits + conf_raw unparseable
        con = L.connect(self.db)
        try:
            r = con.execute("SELECT hits, hit_rate_pct, conf_raw FROM ledger").fetchone()
        finally:
            con.close()
        self.assertIsNone(r["hits"])
        self.assertIsNone(r["hit_rate_pct"])
        self.assertIsNone(r["conf_raw"])

    def test_idempotent_rebuild(self):
        _write_csv(self.csv, [_row("AF-1", "GBP/USD", 2, 3, 40.0)])
        L.rebuild(self.csv, self.db)
        s2 = L.rebuild(self.csv, self.db)            # second rebuild = same result, no growth
        self.assertEqual(s2["db_rows"], 1)

    def test_duplicate_report_id_deduped(self):
        _write_csv(self.csv, [_row("AF-DUP", "GBP/USD", 2, 3, 40.0),
                              _row("AF-DUP", "GBP/USD", 9, 9, 50.0)])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["db_rows"], 1)
        self.assertEqual(summ["duplicates_dropped"], 1)

    def test_missing_csv_yields_empty_mirror(self):
        summ = L.rebuild(self.tmp / "nope.csv", self.db)
        self.assertEqual(summ["db_rows"], 0)

    def test_csv_not_mutated(self):
        rows = [_row("AF-1", "GBP/USD", 2, 3, 40.0)]
        _write_csv(self.csv, rows)
        before = self.csv.read_bytes()
        L.rebuild(self.csv, self.db)
        self.assertEqual(before, self.csv.read_bytes())


if __name__ == "__main__":
    unittest.main(verbosity=2)
