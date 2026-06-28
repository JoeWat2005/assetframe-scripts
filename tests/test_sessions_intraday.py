"""Tests for sessions.py (weekend/holiday/crypto window logic) and intraday.py's
shared anchor math compute_pivots_bands (golden values, division/None guards).

These are offline: no network. The intraday tests exercise only the pure math
helper that the --anchor path reuses, so the live fetch path is never touched.

Run:  python -m pytest tests/test_sessions_intraday.py
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


class TestDSTSessionBoundaries(unittest.TestCase):
    """Session boundaries are derived from the venue's local time via zoneinfo, so they shift
    automatically with DST. Summer (EDT/CDT) must match the legacy hardcoded UTC; winter
    (EST/CST) must move +1h."""
    def _w(self, prof, y, mo, d, h):
        s = SS.get_session(prof, now=datetime(y, mo, d, h, 0, tzinfo=timezone.utc))
        return s["window_start_utc"], s["window_end_utc"]

    def test_equity_summer_unchanged(self):
        self.assertEqual(self._w("us_equity_rth", 2026, 6, 15, 5), ("2026-06-15 13:30", "2026-06-15 20:00"))

    def test_equity_winter_shifts_one_hour(self):
        self.assertEqual(self._w("us_equity_rth", 2026, 12, 14, 5), ("2026-12-14 14:30", "2026-12-14 21:00"))

    def test_cme_winter_next_session(self):
        # Sat 2026-12-12 -> next session Sun 17:00 CST (23:00 UTC) -> Mon 16:00 CST (22:00 UTC)
        self.assertEqual(self._w("cme_futures", 2026, 12, 12, 10), ("2026-12-13 23:00", "2026-12-14 22:00"))

    def test_fx_winter_weekly_close(self):
        s = SS.get_session("fx_spot", now=datetime(2026, 12, 18, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(s["market_close_utc"], "2026-12-18 22:00")  # Fri 17:00 EST


class TestGetWindow(unittest.TestCase):
    PROFILES = ("cme_futures", "fx_spot", "us_equity_rth", "crypto_24_7")
    MOMENTS = (datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc),
               datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc),
               datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc))

    def test_standard_windows_identical_to_get_session(self):
        # SAFETY: the live universe must be byte-identical. get_window with any standard /
        # empty / unknown forecast window returns exactly get_session().
        # NB: next_liquid_session is NO LONGER identical for 24/5 venues (it now targets the next
        # daily close, not the weekly close) — that divergence is the fix, asserted separately below.
        for p in self.PROFILES:
            for m in self.MOMENTS:
                base = SS.get_session(p, now=m)
                for fw in (None, "next_session", "next_regular_session",
                           "rolling_24h", "garbage"):
                    self.assertEqual(SS.get_window(p, now=m, forecast_window=fw), base,
                                     f"{p} {fw} {m} diverged from get_session")

    def test_long_windows_extend_end_keep_start(self):
        m = datetime(2026, 6, 17, 14, 0, tzinfo=timezone.utc)  # Wednesday, mid-week
        for p in self.PROFILES:
            base = SS.get_session(p, now=m)
            for fw in ("next_week", "next_5_sessions"):
                w = SS.get_window(p, now=m, forecast_window=fw)
                self.assertEqual(w["window_start_utc"], base["window_start_utc"])
                self.assertGreaterEqual(w["window_end_utc"], base["window_end_utc"])
                self.assertEqual(w["forecast_window"], fw)
                # window must be non-degenerate
                self.assertGreater(w["window_end_utc"], w["window_start_utc"])

    def test_crypto_next_week_is_seven_days(self):
        m = datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc)
        w = SS.get_window("crypto_24_7", now=m, forecast_window="next_week")
        self.assertEqual(w["window_start_utc"], "2026-06-15 05:00")
        self.assertEqual(w["window_end_utc"], "2026-06-22 05:00")

    def test_next_liquid_session_is_daily_for_24_5(self):
        # THE FX BUG FIX: next_liquid_session must target the NEXT DAILY close (~1 session), NOT
        # the Friday weekly close that made Mon-Thu reports overlap and double-count in calibration.
        mon = datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc)   # Monday morning
        for p in ("fx_spot", "cme_futures"):
            base = SS.get_session(p, now=mon)
            w = SS.get_window(p, now=mon, forecast_window="next_liquid_session")
            self.assertEqual(w["window_start_utc"], base["window_start_utc"])
            self.assertLess(w["window_end_utc"], base["window_end_utc"], f"{p} still weekly")
            span_h = ((datetime.strptime(w["window_end_utc"], "%Y-%m-%d %H:%M")
                       - datetime.strptime(w["window_start_utc"], "%Y-%m-%d %H:%M")
                       ).total_seconds() / 3600)
            self.assertLessEqual(span_h, 30, f"{p} window {span_h}h is not ~daily")
            self.assertEqual(w["forecast_window"], "next_liquid_session")

    def test_next_liquid_session_non_overlapping_across_days(self):
        mon = SS.get_window("fx_spot", now=datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc),
                            forecast_window="next_liquid_session")
        tue = SS.get_window("fx_spot", now=datetime(2026, 6, 16, 8, 0, tzinfo=timezone.utc),
                            forecast_window="next_liquid_session")
        self.assertNotEqual(mon["window_end_utc"], tue["window_end_utc"])   # the overlap bug
        self.assertLessEqual(mon["window_end_utc"], tue["window_start_utc"])  # no overlap

    def test_next_liquid_session_unchanged_for_equity_and_crypto(self):
        m = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
        for p in ("us_equity_rth", "crypto_24_7"):
            self.assertEqual(SS.get_window(p, now=m, forecast_window="next_liquid_session"),
                             SS.get_session(p, now=m))


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
