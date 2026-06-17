"""Tests for score_report.py — each scoring mechanic, the setup grader, the
calibration summary, the manual-verdict validator, and the append-only ledger
write (never rewrites existing rows).

Run:  python scripts/test_score_report.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import score_report as S


def _bars(seq):
    """seq of (o, h, l, c) -> bar dicts with sequential hourly timestamps."""
    return [{"t": datetime(2026, 6, 12, h, tzinfo=timezone.utc),
             "o": o, "h": hi, "l": lo, "c": c}
            for h, (o, hi, lo, c) in enumerate(seq)]


class TestScoringMechanics(unittest.TestCase):
    def setUp(self):
        # last close = 10; high reaches 14 on bar1; low dips to 9 on bar0.
        self.bars = _bars([(10, 12, 9, 11), (11, 13, 10, 12), (12, 14, 11, 10)])

    def test_close_above(self):
        self.assertEqual(S.score_prediction({"type": "close_above", "level": 9}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "close_above", "level": 11}, self.bars), "N")

    def test_close_below(self):
        self.assertEqual(S.score_prediction({"type": "close_below", "level": 11}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "close_below", "level": 9}, self.bars), "N")

    def test_range_inside(self):
        self.assertEqual(S.score_prediction({"type": "range_inside", "lo": 8, "hi": 15}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "range_inside", "lo": 8, "hi": 13}, self.bars), "N")
        self.assertEqual(S.score_prediction({"type": "range_inside", "lo": 10, "hi": 15}, self.bars), "N")

    def test_touches(self):
        self.assertEqual(S.score_prediction({"type": "touches", "level": 13.5}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "touches", "level": 20}, self.bars), "N")

    def test_no_close_below(self):
        self.assertEqual(S.score_prediction({"type": "no_close_below", "level": 9}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "no_close_below", "level": 10.5}, self.bars), "N")

    def test_no_close_above(self):
        self.assertEqual(S.score_prediction({"type": "no_close_above", "level": 12.5}, self.bars), "Y")
        self.assertEqual(S.score_prediction({"type": "no_close_above", "level": 11.5}, self.bars), "N")

    def test_no_close_above_after_touch(self):
        # first bar with h>=13 is bar1 (h=13, c=12). c<=13.5 -> Y
        self.assertEqual(S.score_prediction(
            {"type": "no_close_above_after_touch", "touch": 13, "level": 13.5}, self.bars), "Y")
        # c=12 > 11.5 -> N
        self.assertEqual(S.score_prediction(
            {"type": "no_close_above_after_touch", "touch": 13, "level": 11.5}, self.bars), "N")
        # never touched -> NT
        self.assertEqual(S.score_prediction(
            {"type": "no_close_above_after_touch", "touch": 99, "level": 100}, self.bars), "NT")

    def test_no_close_below_after_touch(self):
        # first bar with l<=10 is bar0 (l=9, c=11). c>=10.5 -> Y
        self.assertEqual(S.score_prediction(
            {"type": "no_close_below_after_touch", "touch": 10, "level": 10.5}, self.bars), "Y")
        # c=11 < 11.5 -> N
        self.assertEqual(S.score_prediction(
            {"type": "no_close_below_after_touch", "touch": 10, "level": 11.5}, self.bars), "N")
        self.assertEqual(S.score_prediction(
            {"type": "no_close_below_after_touch", "touch": -1, "level": 0}, self.bars), "NT")

    def test_manual_is_manual(self):
        self.assertEqual(S.score_prediction({"type": "manual", "note": "x"}, self.bars), "MANUAL")

    def test_empty_bars_is_no_trigger(self):
        self.assertEqual(S.score_prediction({"type": "close_above", "level": 9}, []), "NT")

    def test_unknown_type_flagged(self):
        self.assertEqual(S.score_prediction({"type": "wat"}, self.bars), "UNKNOWN(wat)")


class TestScoreSetup(unittest.TestCase):
    def setUp(self):
        self.bars = _bars([(100, 101, 99, 100), (100, 103, 98, 102), (102, 104, 100, 103)])

    def test_long_t1_first(self):
        s = {"direction": "long", "entry_lo": 99, "entry_hi": 100, "invalidation": 97, "t1": 104}
        self.assertEqual(S.score_setup(s, self.bars), ("yes", "t1-first"))

    def test_long_invalidation_first(self):
        s = {"direction": "long", "entry_lo": 99, "entry_hi": 100, "invalidation": 99.5, "t1": 104}
        self.assertEqual(S.score_setup(s, self.bars), ("yes", "invalidation-first"))

    def test_never_fills(self):
        s = {"direction": "long", "entry_lo": 50, "entry_hi": 55, "invalidation": 40, "t1": 60}
        self.assertEqual(S.score_setup(s, self.bars), ("no", "n/a"))

    def test_open_at_window_end(self):
        # fills, but neither t1 nor invalidation reached before window end
        s = {"direction": "long", "entry_lo": 99, "entry_hi": 100, "invalidation": 90, "t1": 200}
        self.assertEqual(S.score_setup(s, self.bars), ("yes", "open-at-window-end"))

    def test_no_setup_or_bars(self):
        self.assertEqual(S.score_setup(None, self.bars), ("no", "n/a"))
        self.assertEqual(S.score_setup({"direction": "long"}, []), ("no", "n/a"))


class TestCalibrationSummary(unittest.TestCase):
    def test_none_below_10_rows(self):
        self.assertIsNone(S.calibration([{"confidence": "60", "hits": "1", "misses": "0"}] * 9))

    def test_buckets_at_10_rows(self):
        rows = [{"confidence": "55", "hits": "1", "misses": "1"}] * 5 \
            + [{"confidence": "80", "hits": "2", "misses": "0"}] * 5
        cal = S.calibration(rows)
        self.assertIsNotNone(cal)
        self.assertEqual(cal["n_reports"], 10)
        self.assertEqual(cal["buckets"]["<=60"]["reports"], 5)
        self.assertEqual(cal["buckets"][">75"]["hit_rate_pct"], 100.0)


class TestManualValidator(unittest.TestCase):
    def test_unknown_id_exits_2(self):
        preds = [{"id": "P1", "type": "close_above", "level": 1},
                 {"id": "P5", "type": "manual"}]
        with self.assertRaises(SystemExit) as cm:
            S.validate_manual({"P9": "Y"}, preds)
        self.assertEqual(cm.exception.code, 2)

    def test_non_manual_id_exits_2(self):
        preds = [{"id": "P1", "type": "close_above", "level": 1},
                 {"id": "P5", "type": "manual"}]
        with self.assertRaises(SystemExit) as cm:
            S.validate_manual({"P1": "Y"}, preds)
        self.assertEqual(cm.exception.code, 2)

    def test_valid_manual_id_passes(self):
        preds = [{"id": "P5", "type": "manual"}]
        S.validate_manual({"P5": "NT"}, preds)  # no raise


class TestParseArgsManual(unittest.TestCase):
    def test_bad_verdict_rejected(self):
        with self.assertRaises(SystemExit):
            S.parse_args(["--manual", "P5=MAYBE"])

    def test_good_verdicts_parsed(self):
        opts = S.parse_args(["--manual", "P5=Y,P6=NT", "--dry-run"])
        self.assertEqual(opts["manual"], {"P5": "Y", "P6": "NT"})
        self.assertTrue(opts["dry_run"])


class TestLoadBars(unittest.TestCase):
    def _write_csv(self, lines):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_window_filter_inclusive(self):
        path = self._write_csv([
            "2026-06-12 09:00,10,11,9,10,100",
            "2026-06-12 10:00,10,12,10,11,100",
            "2026-06-12 11:00,11,13,11,12,100",
        ])
        start = S.parse_dt("2026-06-12 10:00")
        end = S.parse_dt("2026-06-12 11:00")
        bars = S.load_bars(path, start, end)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["c"], 11)
        self.assertEqual(bars[-1]["c"], 12)


class TestAppendOnlyLedger(unittest.TestCase):
    """The ledger must NEVER be rewritten: each score appends exactly one row and
    leaves prior rows byte-for-byte intact."""

    def _make_predictions_file(self, report_id, wstart, wend, hourly_csv):
        spec = {
            "report_id": report_id, "instrument": "Test", "symbol": "TST=X", "roll_utc": 0,
            "view": "Neutral", "confidence": 60, "conf_version": 2, "conf_raw": 60,
            "window_start_utc": wstart, "window_end_utc": wend, "hourly_csv": hourly_csv,
            "taxonomy": {"asset_class": "equity", "prediction_type": "range_hold",
                         "direction": "neutral", "horizon": "next_session",
                         "market_regime": "range"},
            "predictions": [{"id": "P1", "type": "close_above", "level": 9, "expect": True}],
            "setup": {"direction": "long", "entry_lo": 9, "entry_hi": 10,
                      "invalidation": 8, "t1": 13},
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f)
        self.addCleanup(os.remove, path)
        return path

    def _make_hourly(self):
        # window 2025-01-06 09:00 .. 11:00 (well in the past so it scores without --force)
        path = os.path.join(tempfile.mkdtemp(), "TST_hourly.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("2025-01-06 09:00,10,11,9,10,100\n")
            f.write("2025-01-06 10:00,10,12,10,11,100\n")
            f.write("2025-01-06 11:00,11,13,11,12,100\n")
        self.addCleanup(os.remove, path)
        return path

    def _run(self, pred_path, ledger_path):
        env = dict(os.environ)
        # run from a temp cwd so the default ledger path is isolated
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "score_report.py"), pred_path,
             "--hourly", self._hourly],
            cwd=ledger_path[0], capture_output=True, text=True, env=env)

    def test_two_scores_append_not_rewrite(self):
        workdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(workdir, ignore_errors=True))
        ledger = os.path.join(workdir, "ledger", "outcome_ledger.csv")
        self._hourly = self._make_hourly()

        p1 = self._make_predictions_file("AF-20250106-AAA", "2025-01-06 09:00",
                                         "2025-01-06 11:00", self._hourly)
        r1 = subprocess.run([sys.executable, os.path.join(HERE, "score_report.py"), p1,
                             "--hourly", self._hourly],
                            cwd=workdir, capture_output=True, text=True)
        self.assertEqual(r1.returncode, 0, r1.stderr + r1.stdout)
        with open(ledger, encoding="utf-8") as f:
            after_first = f.read()
        rows_first = list(csv.reader(after_first.splitlines()))
        self.assertEqual(len(rows_first), 2)  # header + 1 row

        p2 = self._make_predictions_file("AF-20250106-BBB", "2025-01-06 09:00",
                                         "2025-01-06 11:00", self._hourly)
        r2 = subprocess.run([sys.executable, os.path.join(HERE, "score_report.py"), p2,
                             "--hourly", self._hourly],
                            cwd=workdir, capture_output=True, text=True)
        self.assertEqual(r2.returncode, 0, r2.stderr + r2.stdout)
        with open(ledger, encoding="utf-8") as f:
            after_second = f.read()
        rows_second = list(csv.reader(after_second.splitlines()))
        self.assertEqual(len(rows_second), 3)  # header + 2 rows
        # the first data row is preserved verbatim (append-only invariant)
        self.assertTrue(after_second.startswith(after_first))
        self.assertEqual(rows_second[1], rows_first[1])
        self.assertEqual(rows_second[0], S.LEDGER_COLS)

    def test_dry_run_writes_nothing(self):
        workdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(workdir, ignore_errors=True))
        ledger = os.path.join(workdir, "ledger", "outcome_ledger.csv")
        self._hourly = self._make_hourly()
        p1 = self._make_predictions_file("AF-20250106-AAA", "2025-01-06 09:00",
                                         "2025-01-06 11:00", self._hourly)
        r = subprocess.run([sys.executable, os.path.join(HERE, "score_report.py"), p1,
                            "--hourly", self._hourly, "--dry-run"],
                           cwd=workdir, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertFalse(os.path.exists(ledger), "dry-run must not create the ledger")


if __name__ == "__main__":
    unittest.main(verbosity=2)
