"""Tests for sessions.py (weekend/holiday/crypto window logic) and intraday.py's
shared anchor math compute_pivots_bands (golden values, division/None guards).

These are offline: no network. The intraday tests exercise only the pure math
helper that the --anchor path reuses, so the live fetch path is never touched.

Run:  python scripts/test_sessions_intraday.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sessions as SS
import intraday as I


class TestCrypto247(unittest.TestCase):
    def test_rolling_24h_on_weekend(self):
        sat = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)  # Saturday
        s = SS.get_session("crypto_24_7", now=sat)
        self.assertEqual(s["market_state"], "open")
        self.assertEqual(s["window_start_utc"], "2026-06-13 10:00")
        self.assertEqual(s["window_end_utc"], "2026-06-14 10:00")
        self.assertEqual(s["market_close_utc"], "none - market does not close")

    def test_anchor_is_noop_for_crypto(self):
        # crypto has no maintenance break / weekly close — window is purely rolling
        s = SS.get_session("crypto_24_7", now=datetime(2026, 6, 17, 3, 0, tzinfo=timezone.utc))
        self.assertEqual(s["next_maintenance_break"], "none scheduled (venue-dependent)")


class TestEquitySessions(unittest.TestCase):
    def test_weekend_targets_next_session(self):
        sat = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)
        s = SS.get_session("us_equity_rth", now=sat)
        self.assertEqual(s["market_state"], "closed_weekend_or_holiday")
        # next regular session is Monday 2026-06-15 13:30 UTC
        self.assertEqual(s["window_start_utc"], "2026-06-15 13:30")
        self.assertEqual(s["window_end_utc"], "2026-06-15 20:00")

    def test_holiday_skipped(self):
        hol = {datetime(2026, 6, 18).date()}  # Thursday holiday
        thu = datetime(2026, 6, 18, 15, 0, tzinfo=timezone.utc)
        s = SS.get_session("us_equity_rth", now=thu, holiday_dates=hol)
        self.assertEqual(s["market_state"], "closed_weekend_or_holiday")
        self.assertIn("2026-06-18", s["holidays_applied"])
        # the window must NOT start on the holiday
        self.assertNotEqual(s["window_start_utc"][:10], "2026-06-18")

    def test_pre_market_targets_today(self):
        pre = datetime(2026, 6, 16, 9, 0, tzinfo=timezone.utc)  # Tuesday pre-market
        s = SS.get_session("us_equity_rth", now=pre)
        self.assertEqual(s["market_state"], "pre_market")
        self.assertEqual(s["window_start_utc"], "2026-06-16 13:30")

    def test_open_with_time_left_targets_current(self):
        mid = datetime(2026, 6, 16, 14, 0, tzinfo=timezone.utc)  # well inside RTH
        s = SS.get_session("us_equity_rth", now=mid)
        self.assertEqual(s["market_state"], "open_regular_session")
        self.assertEqual(s["window_start_utc"], "2026-06-16 14:00")


class TestFuturesSessions(unittest.TestCase):
    def test_friday_evening_after_close_next_session(self):
        fri_eve = datetime(2026, 6, 12, 21, 30, tzinfo=timezone.utc)
        s = SS.get_session("cme_futures", now=fri_eve)
        self.assertEqual(s["market_state"], "closed_weekend")
        # next session: Sunday 22:00 UTC reopen -> Monday 21:00 UTC close
        self.assertEqual(s["window_start_utc"], "2026-06-14 22:00")
        self.assertEqual(s["window_end_utc"], "2026-06-15 21:00")

    def test_midweek_open_targets_current_session(self):
        wed = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
        s = SS.get_session("cme_futures", now=wed)
        self.assertEqual(s["market_state"], "open")
        self.assertEqual(s["window_label"], "remainder of current session")


class TestComputePivotsBands(unittest.TestCase):
    def test_golden_values(self):
        piv, bands = I.compute_pivots_bands({"h": 100, "l": 90, "c": 95}, 96, 4.0)
        self.assertAlmostEqual(piv["PP"], 95.0, places=6)       # (100+90+95)/3
        self.assertAlmostEqual(piv["R1"], 100.0, places=6)      # 2*PP - low
        self.assertAlmostEqual(piv["S1"], 90.0, places=6)       # 2*PP - high
        self.assertAlmostEqual(piv["R2"], 105.0, places=6)      # PP + range
        self.assertAlmostEqual(piv["S2"], 85.0, places=6)       # PP - range
        self.assertAlmostEqual(bands["inner_hi"], 98.0, places=6)   # 96 + 0.5*4
        self.assertAlmostEqual(bands["inner_lo"], 94.0, places=6)
        self.assertAlmostEqual(bands["outer_hi"], 100.0, places=6)  # 96 + 1.0*4
        self.assertAlmostEqual(bands["outer_lo"], 92.0, places=6)
        self.assertEqual(bands["open"], 96)

    def test_no_atr_means_no_bands(self):
        _, bands = I.compute_pivots_bands({"h": 100, "l": 90, "c": 95}, 96, None)
        self.assertIsNone(bands)
        _, bands0 = I.compute_pivots_bands({"h": 100, "l": 90, "c": 95}, 96, 0)
        self.assertIsNone(bands0)

    def test_no_prior_means_no_pivots(self):
        piv, _ = I.compute_pivots_bands(None, 96, 4.0)
        self.assertIsNone(piv)

    def test_none_anchor_close_means_no_bands(self):
        _, bands = I.compute_pivots_bands({"h": 100, "l": 90, "c": 95}, None, 4.0)
        self.assertIsNone(bands)


if __name__ == "__main__":
    unittest.main(verbosity=2)
