"""Tests for the Phase-1 scheduler layer: config_loader validation, calendar_rules
due-logic, memory_pack token budget, and the score_report idempotency (dedup) guard."""
import csv
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import config_loader as C
import calendar_rules as CAL
import memory_pack as MP

VALID = {"id": "x", "name": "X", "instrument": "X", "ticker": "X",
         "provider_symbols": {"yahoo": "X=X"}, "asset_class": "fx",
         "session_profile": "fx_spot", "cadence": "weekday", "timezone": "UTC"}


def _cfg(d, assets):
    p = Path(d) / "a.json"
    p.write_text(json.dumps({"assets": assets}))
    return p


class ConfigLoader(unittest.TestCase):
    def test_valid_loads(self):
        with tempfile.TemporaryDirectory() as d:
            a = C.load_assets(_cfg(d, [VALID]))
            self.assertEqual(len(a), 1)
            self.assertTrue(a[0]["enabled"])
            self.assertEqual(a[0]["publish_policy"], "approval_required")  # default applied

    def test_duplicate_id(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(C.ConfigError):
            C.load_assets(_cfg(d, [VALID, dict(VALID)]))

    def test_bad_asset_class(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(C.ConfigError):
            C.load_assets(_cfg(d, [{**VALID, "id": "y", "asset_class": "forex"}]))

    def test_missing_yahoo_symbol(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(C.ConfigError):
            C.load_assets(_cfg(d, [{**VALID, "id": "z", "provider_symbols": {}}]))

    def test_bad_cadence_and_timezone(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(C.ConfigError):
            C.load_assets(_cfg(d, [{**VALID, "id": "w", "cadence": "hourly",
                                    "timezone": "Mars/Phobos"}]))


class Calendar(unittest.TestCase):
    fx = {**VALID, "cadence": "weekday", "asset_class": "fx"}
    crypto = {**VALID, "id": "c", "cadence": "daily", "asset_class": "crypto", "timezone": "UTC"}
    eq = {**VALID, "id": "e", "cadence": "trading_day", "asset_class": "equity",
          "timezone": "America/New_York"}
    TUE = datetime(2026, 6, 16, 6, tzinfo=timezone.utc)
    SAT = datetime(2026, 6, 20, 6, tzinfo=timezone.utc)
    FRI_HOL = datetime(2026, 7, 3, 6, tzinfo=timezone.utc)

    def test_fx_weekday_due(self):
        self.assertTrue(CAL.is_due(self.fx, self.TUE, {})[0])

    def test_fx_weekend_not_due(self):
        self.assertFalse(CAL.is_due(self.fx, self.SAT, {})[0])

    def test_crypto_weekend_due(self):
        self.assertTrue(CAL.is_due(self.crypto, self.SAT, {})[0])

    def test_equity_holiday_not_due(self):
        self.assertFalse(CAL.is_due(self.eq, self.FRI_HOL, {"US": {"2026-07-03"}})[0])

    def test_equity_weekday_due(self):
        self.assertTrue(CAL.is_due(self.eq, self.TUE, {})[0])

    def test_disabled_not_due(self):
        self.assertFalse(CAL.is_due({**self.fx, "enabled": False}, self.TUE, {})[0])


class MemoryPackBudget(unittest.TestCase):
    def test_bounded_and_neutral_without_history(self):
        # as_of in the far past -> no ledger rows in window -> neutral + bounded
        pack = MP.build_pack({**VALID, "instrument": "X", "ticker": "X", "asset_class": "fx"},
                             as_of=datetime(2000, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(pack["budget"]["within_budget"])
        self.assertLessEqual(pack["budget"]["approx_tokens"], pack["budget"]["limit"])
        self.assertEqual(pack["global"]["total_scored_reports"], 0)


class Idempotency(unittest.TestCase):
    def test_double_score_appends_one_row(self):
        """Scoring the same report_id twice must leave exactly one ledger row."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "ledger").mkdir()
            csvp = d / "h.csv"
            with open(csvp, "w", newline="") as f:
                w = csv.writer(f)
                for hh in range(6, 22):     # bars covering a closed past window
                    w.writerow([f"2026-06-16 {hh:02d}:00", "1.00", "1.02", "0.99", "1.01", "0"])
            pred = {"report_id": "AF-20260616-TEST", "instrument": "Test", "symbol": "TEST",
                    "window_start_utc": "2026-06-16 06:00", "window_end_utc": "2026-06-16 21:00",
                    "hourly_csv": str(csvp),
                    "predictions": [{"id": "P1", "type": "close_above", "level": 1.00, "expect": True}]}
            predp = d / "p_predictions.json"
            predp.write_text(json.dumps(pred))
            sr = str(HERE / "score_report.py")
            for _ in range(2):
                subprocess.run([sys.executable, sr, str(predp)], cwd=str(d),
                               capture_output=True, text=True)
            with open(d / "ledger" / "outcome_ledger.csv") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
