"""Tests for calendar_rules.is_due — the market-hours / closed-market GENERATION gate.

Crypto is 24/7 (always due); every other asset class rejects new-report GENERATION on
weekends + exchange holidays, even when cadence='daily'. (Scoring of closed windows is gated
in run_daily.score_step, not here, so Friday's calls are still graded over the weekend.)
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import calendar_rules as C


def _at(s):  # UTC datetime from "YYYY-MM-DD HH:MM"
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def _asset(cls, cadence="daily", tz="UTC", enabled=True):
    return {"id": "x", "enabled": enabled, "asset_class": cls, "cadence": cadence, "timezone": tz}


# 2026-06-20 Sat · 06-21 Sun · 06-22 Mon (all 05:00 UTC, the daily run time)
SAT, SUN, MON = _at("2026-06-20 05:00"), _at("2026-06-21 05:00"), _at("2026-06-22 05:00")


class TestCrypto24x7(unittest.TestCase):
    def test_crypto_always_due(self):
        for when in (SAT, SUN, MON):
            due, reason = C.is_due(_asset("crypto"), when, holidays={})
            self.assertTrue(due, reason)


class TestClosedMarkets(unittest.TestCase):
    def test_fx_rejected_on_weekend(self):
        self.assertFalse(C.is_due(_asset("fx"), SAT, holidays={})[0])
        self.assertFalse(C.is_due(_asset("fx"), SUN, holidays={})[0])

    def test_fx_due_on_weekday(self):
        self.assertTrue(C.is_due(_asset("fx"), MON, holidays={})[0])

    def test_all_closed_classes_weekend_then_weekday(self):
        for cls in ("equity", "commodity", "index", "futures"):
            self.assertFalse(C.is_due(_asset(cls), SAT, holidays={})[0], cls)
            self.assertTrue(C.is_due(_asset(cls), MON, holidays={})[0], cls)

    def test_daily_cadence_does_not_bypass_weekend_for_non_crypto(self):
        # The exact bug this fixed: cadence='daily' used to force due on weekends.
        self.assertFalse(C.is_due(_asset("fx", cadence="daily"), SAT, holidays={})[0])


class TestHolidayGate(unittest.TestCase):
    def test_equity_rejected_on_exchange_holiday(self):
        a = _asset("equity", tz="America/New_York")
        due, reason = C.is_due(a, _at("2026-06-22 15:00"), holidays={"US": ["2026-06-22"]})
        self.assertFalse(due)
        self.assertIn("holiday", reason)


class TestGuards(unittest.TestCase):
    def test_disabled_not_due(self):
        self.assertFalse(C.is_due(_asset("crypto", enabled=False), MON, holidays={})[0])

    def test_unknown_cadence_not_due(self):
        self.assertFalse(C.is_due(_asset("fx", cadence="lunar"), MON, holidays={})[0])
        self.assertFalse(C.is_due(_asset("crypto", cadence="lunar"), MON, holidays={})[0])


if __name__ == "__main__":
    unittest.main()
