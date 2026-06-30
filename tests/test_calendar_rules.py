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


class TestNextDueAt(unittest.TestCase):
    def test_crypto_next_is_the_next_0500_slot(self):
        # crypto is always due -> next_due is simply the next 05:00 UTC slot after `now`
        self.assertEqual(C.next_due_at(_asset("crypto"), _at("2026-06-20 03:00"), holidays={}), SAT)
        self.assertEqual(C.next_due_at(_asset("crypto"), _at("2026-06-20 05:00"), holidays={}), SUN)  # slot passed

    def test_fx_skips_the_weekend(self):
        # from Saturday, an FX asset's next generation is Monday 05:00 (markets shut Sat/Sun)
        self.assertEqual(C.next_due_at(_asset("fx"), _at("2026-06-20 03:00"), holidays={}), MON)

    def test_disabled_is_none(self):
        self.assertIsNone(C.next_due_at(_asset("fx", enabled=False), MON, holidays={}))

    def test_slot_is_future_and_0500(self):
        nd = C.next_due_at(_asset("equity", tz="America/New_York"), _at("2026-06-22 12:00"), holidays={})
        self.assertIsNotNone(nd)
        self.assertGreater(nd, _at("2026-06-22 12:00"))
        self.assertEqual((nd.hour, nd.minute), (5, 0))


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


class TestScheduledCadences(unittest.TestCase):
    # 2026-06-22 is a Monday; 06-23 Tue; 06-20 Sat.
    def test_weekly_defaults_to_monday(self):
        a = _asset("equity", cadence="weekly", tz="America/New_York")
        self.assertTrue(C.is_due(a, MON, holidays={})[0])
        self.assertFalse(C.is_due(a, _at("2026-06-23 05:00"), holidays={})[0])

    def test_weekly_cadence_day_override(self):
        a = _asset("equity", cadence="weekly", tz="America/New_York")
        a["cadence_day"] = "wed"
        self.assertTrue(C.is_due(a, _at("2026-06-24 05:00"), holidays={})[0])   # Wed
        self.assertFalse(C.is_due(a, MON, holidays={})[0])

    def test_weekly_crypto_anchors_to_weekday_not_weekend(self):
        a = _asset("crypto", cadence="weekly")
        self.assertTrue(C.is_due(a, MON, holidays={})[0])
        self.assertFalse(C.is_due(a, SAT, holidays={})[0])

    def test_monthly_first_trading_day(self):
        a = _asset("equity", cadence="monthly", tz="America/New_York")
        # 2026-06-01 is a Monday -> first trading day of June
        self.assertTrue(C.is_due(a, _at("2026-06-01 05:00"), holidays={})[0])
        self.assertFalse(C.is_due(a, _at("2026-06-02 05:00"), holidays={})[0])

    def test_monthly_skips_weekend_start(self):
        # 2026-08-01 is a Saturday -> first trading day is Mon 2026-08-03
        a = _asset("equity", cadence="monthly", tz="America/New_York")
        self.assertFalse(C.is_due(a, _at("2026-08-01 05:00"), holidays={})[0])
        self.assertTrue(C.is_due(a, _at("2026-08-03 05:00"), holidays={})[0])


class TestGuards(unittest.TestCase):
    def test_disabled_not_due(self):
        self.assertFalse(C.is_due(_asset("crypto", enabled=False), MON, holidays={})[0])

    def test_unknown_cadence_not_due(self):
        self.assertFalse(C.is_due(_asset("fx", cadence="lunar"), MON, holidays={})[0])
        self.assertFalse(C.is_due(_asset("crypto", cadence="lunar"), MON, holidays={})[0])


class TestComputedHolidays(unittest.TestCase):
    def test_us_known_dates_any_year(self):
        us28 = C.computed_holidays("US", 2028)
        self.assertIn("2028-01-17", us28)   # MLK (3rd Mon Jan)
        self.assertIn("2028-05-29", us28)   # Memorial (last Mon May)
        self.assertIn("2028-09-04", us28)   # Labor (1st Mon Sep)
        self.assertIn("2028-11-23", us28)   # Thanksgiving (4th Thu Nov)
        self.assertIn("2028-04-14", us28)   # Good Friday (Easter 2028-04-16)
        self.assertIn("2028-06-19", us28)   # Juneteenth (Mon, 19th is a Mon in 2028? observed)

    def test_us_independence_observed_when_saturday(self):
        # 2026-07-04 is a Saturday -> NYSE observes Friday 2026-07-03
        us26 = C.computed_holidays("US", 2026)
        self.assertIn("2026-07-03", us26)
        self.assertNotIn("2026-07-04", us26)

    def test_us_new_year_saturday_not_observed(self):
        # 2028-01-01 falls on a Saturday -> NO observed closure (special NYSE rule)
        self.assertNotIn("2028-01-01", C.computed_holidays("US", 2028))
        self.assertNotIn("2027-12-31", C.computed_holidays("US", 2028))

    def test_uk_known_dates_and_substitute(self):
        uk27 = C.computed_holidays("UK", 2027)
        self.assertIn("2027-03-26", uk27)   # Good Friday
        self.assertIn("2027-03-29", uk27)   # Easter Monday
        self.assertIn("2027-05-03", uk27)   # Early May (1st Mon)
        self.assertIn("2027-08-30", uk27)   # Summer (last Mon Aug)
        # 2027-12-25 is a Saturday -> substitutes Mon 27 + Tue 28
        self.assertIn("2027-12-27", uk27)
        self.assertIn("2027-12-28", uk27)

    def test_is_holiday_uses_computed_without_json(self):
        a = _asset("equity", tz="America/New_York")
        # MLK 2030 (3rd Mon Jan = 2030-01-21) — empty json, must still be a holiday
        due, reason = C.is_due(a, _at("2030-01-21 15:00"), holidays={})
        self.assertFalse(due)
        self.assertIn("holiday", reason)


if __name__ == "__main__":
    unittest.main()
