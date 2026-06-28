"""Tests for run_daily storage retention (_prune_old_dated_dirs).

Temp-dir only: monkeypatches run_daily.ROOT so it never touches the real box. Asserts that old
reports/ + runs/ edition folders are pruned past the window, recent ones and non-dated folders
are kept, and the ledger / config / data working files are never touched.

Run:  python -m pytest tests/test_retention.py
"""
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_daily as R


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_root = R.ROOT
        R.ROOT = self.tmp
        for p in ("reports/2026-06-01/BTC", "reports/2026-06-20/BTC", "reports/_archive",
                  "runs/2026-05-15", "runs/2026-06-21", "ledger", "data/candles", "config"):
            (self.tmp / p).mkdir(parents=True, exist_ok=True)
        (self.tmp / "ledger/outcome_ledger.csv").write_text("scored_at_utc,report_id\n")
        (self.tmp / "data/candles/BTC_daily.csv").write_text("date,o\n")
        (self.tmp / "config/assets.json").write_text("{}")
        (self.tmp / "reports/2026-06-01/BTC/pro.pdf").write_text("x")

    def tearDown(self):
        R.ROOT = self._orig_root

    def test_prunes_old_keeps_recent(self):
        res = R._prune_old_dated_dirs(14, date(2026, 6, 21))
        self.assertIn("reports/2026-06-01", res["removed"])
        self.assertIn("runs/2026-05-15", res["removed"])
        self.assertFalse((self.tmp / "reports/2026-06-01").exists())
        self.assertTrue((self.tmp / "reports/2026-06-20").exists())   # 1 day old -> kept
        self.assertTrue((self.tmp / "runs/2026-06-21").exists())

    def test_never_touches_ledger_config_or_data(self):
        R._prune_old_dated_dirs(1, date(2026, 6, 21))                 # aggressive window
        self.assertTrue((self.tmp / "ledger/outcome_ledger.csv").exists())
        self.assertTrue((self.tmp / "data/candles/BTC_daily.csv").exists())
        self.assertTrue((self.tmp / "config/assets.json").exists())

    def test_non_dated_folders_kept(self):
        R._prune_old_dated_dirs(1, date(2026, 6, 21))
        self.assertTrue((self.tmp / "reports/_archive").exists())     # not YYYY-MM-DD -> kept

    def test_disabled_when_zero_or_negative(self):
        for k in (0, -5):
            res = R._prune_old_dated_dirs(k, date(2026, 6, 21))
            self.assertTrue(res.get("disabled"))
            self.assertEqual(res["removed"], [])
        self.assertTrue((self.tmp / "reports/2026-06-01").exists())   # nothing removed

    def test_today_never_pruned(self):
        # even with keep_days=1, today's folder survives (folder_date < today-1 is false)
        R._prune_old_dated_dirs(1, date(2026, 6, 21))
        self.assertTrue((self.tmp / "runs/2026-06-21").exists())


class TestRetentionDays(unittest.TestCase):
    def test_env_override_and_default(self):
        os.environ.pop("ASSETFRAME_RETENTION_DAYS", None)
        self.assertEqual(R._retention_days(), R._RETENTION_DEFAULT_DAYS)
        os.environ["ASSETFRAME_RETENTION_DAYS"] = "30"
        try:
            self.assertEqual(R._retention_days(), 30)
            os.environ["ASSETFRAME_RETENTION_DAYS"] = "junk"
            self.assertEqual(R._retention_days(), R._RETENTION_DEFAULT_DAYS)  # bad value -> default
        finally:
            os.environ.pop("ASSETFRAME_RETENTION_DAYS", None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
