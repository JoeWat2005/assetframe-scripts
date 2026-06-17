"""Tests for ledger_context.py and research_memory.py — the NO-LOOK-AHEAD filter
(window_end strictly before as_of), graceful empty-ledger degradation, and the
taxonomy-scoped breakdowns shared with confidence.

Run:  python scripts/test_ledger_context.py
"""
import csv
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ledger_context as LC
import research_memory as RM

COLS = ["scored_at_utc", "report_id", "instrument", "view", "confidence",
        "window_end_utc", "results", "hits", "misses", "hit_rate_pct",
        "setup_filled", "setup_outcome", "partial", "conf_version", "conf_raw",
        "asset_class", "pred_type", "direction", "horizon", "market_regime"]


def _row(report_id, wend, hits, misses, instrument="Apple", asset_class="equity",
         pred_type="breakout", direction="bullish", regime="trend_up"):
    return {
        "scored_at_utc": "2026-01-01 00:00", "report_id": report_id, "instrument": instrument,
        "view": "x", "confidence": "60", "window_end_utc": wend, "results": "P1=Y",
        "hits": str(hits), "misses": str(misses), "hit_rate_pct": "50", "setup_filled": "no",
        "setup_outcome": "n/a", "partial": "no", "conf_version": "2", "conf_raw": "60",
        "asset_class": asset_class, "pred_type": pred_type, "direction": direction,
        "horizon": "next_session", "market_regime": regime,
    }


def _write_ledger(rows):
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


class TestNoLookAheadLedgerContext(unittest.TestCase):
    def setUp(self):
        self.path = _write_ledger([
            _row("AF-1-AAPL", "2026-06-10 20:00", 2, 0),   # before
            _row("AF-2-AAPL", "2026-06-12 12:00", 1, 1),   # exactly at as_of
            _row("AF-3-AAPL", "2026-06-13 20:00", 2, 0),   # after
        ])
        self.addCleanup(os.remove, self.path)

    def test_strict_before_as_of(self):
        as_of = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        rows = LC.load_rows(self.path, as_of)
        ids = [r["report_id"] for r in rows]
        # row at == as_of is excluded (strict <), row after excluded
        self.assertEqual(ids, ["AF-1-AAPL"])

    def test_all_visible_when_as_of_far_future(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        rows = LC.load_rows(self.path, as_of)
        self.assertEqual(len(rows), 3)

    def test_rows_sorted_by_window_end(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        rows = LC.load_rows(self.path, as_of)
        wends = [r["_wend"] for r in rows]
        self.assertEqual(wends, sorted(wends))

    def test_malformed_window_end_skipped(self):
        path = _write_ledger([
            _row("AF-1-AAPL", "not-a-date", 2, 0),
            _row("AF-2-AAPL", "2026-06-10 20:00", 1, 0),
        ])
        self.addCleanup(os.remove, path)
        rows = LC.load_rows(path, datetime(2030, 1, 1, tzinfo=timezone.utc))
        self.assertEqual([r["report_id"] for r in rows], ["AF-2-AAPL"])


class TestBuildContextEmpty(unittest.TestCase):
    def test_empty_ledger_neutral_context(self):
        ctx = LC.build_context("Apple", [], ticker="AAPL", asset_class="equity")
        self.assertEqual(ctx["historical_prediction_count"], 0)
        self.assertIsNone(ctx["instrument_hit_rate"])
        self.assertEqual(ctx["ledger_rows_considered"], 0)
        self.assertTrue(any("No scored history" in n for n in ctx["notes_for_ai"]))

    def test_instrument_matching_by_ticker_and_name(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        path = _write_ledger([
            _row("AF-1-AAPL", "2026-06-10 20:00", 2, 0, instrument="Apple Inc"),
            _row("AF-9-MSFT", "2026-06-10 20:00", 0, 2, instrument="Microsoft"),
        ])
        self.addCleanup(os.remove, path)
        rows = LC.load_rows(path, as_of)
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity")
        self.assertEqual(ctx["historical_prediction_count"], 1)  # only the AAPL row
        self.assertEqual(ctx["instrument_hit_rate"], 100.0)
        self.assertEqual(ctx["asset_class_count"], 2)            # both are equity

    def test_prediction_type_breakdown_shared_taxonomy(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        rows = LC.load_rows(_write_ledger([
            _row("AF-1-AAPL", "2026-06-10 20:00", 3, 1, pred_type="breakout"),
            _row("AF-2-AAPL", "2026-06-11 20:00", 1, 1, pred_type="breakout"),
        ]), as_of)
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity")
        self.assertIn("breakout", ctx["prediction_type_hit_rates"])
        self.assertEqual(ctx["prediction_type_counts"]["breakout"], 2)


class TestNoLookAheadResearchMemory(unittest.TestCase):
    def setUp(self):
        self.path = _write_ledger([
            _row("AF-1-AAPL", "2026-06-10 20:00", 2, 0),
            _row("AF-2-MSFT", "2026-06-12 12:00", 1, 1),
            _row("AF-3-TSLA", "2026-06-13 20:00", 2, 0),
        ])
        self.addCleanup(os.remove, self.path)

    def test_strict_before_as_of(self):
        as_of = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        rows = RM.load_rows(self.path, as_of)
        self.assertEqual([r["report_id"] for r in rows], ["AF-1-AAPL"])

    def test_empty_memory_object_valid(self):
        as_of = datetime(2026, 6, 12, 12, 0, tzinfo=timezone.utc)
        rows = RM.load_rows(self.path, datetime(2000, 1, 1, tzinfo=timezone.utc))
        mem = RM.build_memory(rows, as_of)
        self.assertEqual(mem["total_scored_reports"], 0)
        self.assertIsNone(mem["overall_hit_rate_pct"])
        self.assertEqual(mem["best_patterns"], [])
        self.assertTrue(any("No scored history" in n for n in mem["notes"]))

    def test_breakdowns_and_cross(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        rows = RM.load_rows(_write_ledger([
            _row("AF-1-AAPL", "2026-06-01 20:00", 4, 0, pred_type="breakout", regime="trend_up"),
            _row("AF-2-AAPL", "2026-06-02 20:00", 4, 0, pred_type="breakout", regime="trend_up"),
            _row("AF-3-AAPL", "2026-06-03 20:00", 4, 0, pred_type="breakout", regime="trend_up"),
            _row("AF-4-AAPL", "2026-06-04 20:00", 4, 0, pred_type="breakout", regime="trend_up"),
        ]), as_of)
        mem = RM.build_memory(rows, as_of, min_n=4)
        self.assertEqual(mem["by_prediction_type"]["breakout"]["hit_rate_pct"], 100.0)
        self.assertIn("breakout x trend_up", mem["by_prediction_type_x_regime"])
        # a 100% pattern at n=4 should surface as a best pattern
        self.assertTrue(any(p["pattern"] == "breakout" for p in mem["best_patterns"]))

    def test_min_n_guard_suppresses_thin_patterns(self):
        as_of = datetime(2030, 1, 1, tzinfo=timezone.utc)
        rows = RM.load_rows(_write_ledger([
            _row("AF-1-AAPL", "2026-06-01 20:00", 4, 0, pred_type="breakout"),
        ]), as_of)
        mem = RM.build_memory(rows, as_of, min_n=4)
        # only 1 report < min_n=4 -> no named patterns
        self.assertEqual(mem["best_patterns"], [])
        self.assertEqual(mem["worst_patterns"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
