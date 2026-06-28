"""Offline unit tests for scripts/scheduler/calendars/{sessions,calendar_rules}.py.

These modules were relocated into the scheduler.calendars second-level subgroup by the
phase-5 refactor. They are stdlib-only (datetime/json/pathlib/calendar/zoneinfo) and have
NO intra-package imports, so the sys.path subgroup shim cannot break them; the imports below
resolve via tests/conftest.py (which imports `scripts` to apply the shim).

Coverage targets the GAPS left by the existing suites (test_sessions_intraday,
test_cadence_window, test_calendar_rules, test_scheduler, test_audit_fixes,
test_scoring_fixes): the calendar/easter/observance helpers, the load/parse guards,
cadence-day parsing, first-trading-day, and the session boundary / window edge branches.
Everything is deterministic (fixed `now`, explicit holiday sets, tmp files) — no network,
DB, subprocess or zoneinfo-dependent value unless _TZ_OK (the env ships tzdata, as the
existing DST suite already relies on).

Run:  python -m pytest tests/test_scheduler_calendars_unit.py -q
"""
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions as S            # noqa: E402
import calendar_rules as C      # noqa: E402

UTC = timezone.utc


def _at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def _asset(cls, cadence="daily", tz="UTC", enabled=True, **extra):
    a = {"id": "x", "enabled": enabled, "asset_class": cls, "cadence": cadence, "timezone": tz}
    a.update(extra)
    return a


# =====================================================================================
# calendar_rules: load_holidays — file-missing / malformed / filtering / BOM
# =====================================================================================

def test_load_holidays_missing_file_returns_empty(tmp_path):
    assert C.load_holidays(tmp_path / "does_not_exist.json") == {}


def test_load_holidays_malformed_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json,,,", encoding="utf-8")
    assert C.load_holidays(p) == {}


def test_load_holidays_parses_and_drops_non_list_values(tmp_path):
    p = tmp_path / "h.json"
    p.write_text('{"US": ["2026-01-01"], "UK": ["2026-12-25"], "note": "ignore me"}',
                 encoding="utf-8")
    out = C.load_holidays(p)
    assert out == {"US": {"2026-01-01"}, "UK": {"2026-12-25"}}
    assert isinstance(out["US"], set)          # values coerced to sets
    assert "note" not in out                   # non-list value filtered out


def test_load_holidays_handles_utf8_bom(tmp_path):
    p = tmp_path / "bom.json"
    p.write_text('﻿{"US": ["2026-07-03"]}', encoding="utf-8")
    assert C.load_holidays(p) == {"US": {"2026-07-03"}}


# =====================================================================================
# calendar_rules: date primitives — easter / nth-weekday / last-weekday / observance
# =====================================================================================

@pytest.mark.parametrize("year,expected", [
    (2024, date(2024, 3, 31)),
    (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)),
    (2027, date(2027, 3, 28)),
])
def test_easter_known_dates(year, expected):
    assert C._easter(year) == expected


def test_nth_weekday():
    # 3rd Monday of Jan 2028 = 2028-01-17 ; 1st Monday of Sep 2028 = 2028-09-04
    assert C._nth_weekday(2028, 1, 0, 3) == date(2028, 1, 17)
    assert C._nth_weekday(2028, 9, 0, 1) == date(2028, 9, 4)
    # 4th Thursday of Nov 2028 (Thanksgiving) = 2028-11-23
    assert C._nth_weekday(2028, 11, 3, 4) == date(2028, 11, 23)


def test_last_weekday():
    assert C._last_weekday(2028, 5, 0) == date(2028, 5, 29)   # last Mon May 2028
    assert C._last_weekday(2027, 8, 0) == date(2027, 8, 30)   # last Mon Aug 2027
    # December wrap path (month==12 takes the year+1 branch)
    assert C._last_weekday(2026, 12, 4) == date(2026, 12, 25)  # last Friday Dec 2026


def test_us_observed_rules():
    assert C._us_observed(date(2026, 7, 4)) == date(2026, 7, 3)    # Sat -> preceding Fri
    assert C._us_observed(date(2027, 7, 4)) == date(2027, 7, 5)    # Sun -> following Mon
    assert C._us_observed(date(2026, 7, 6)) == date(2026, 7, 6)    # weekday unchanged


def test_uk_substitute_rolls_weekend_forward():
    assert C._uk_substitute(date(2027, 12, 25)) == date(2027, 12, 27)  # Sat -> Mon
    assert C._uk_substitute(date(2027, 12, 26)) == date(2027, 12, 27)  # Sun -> Mon
    assert C._uk_substitute(date(2026, 1, 1)) == date(2026, 1, 1)      # Thu unchanged


# =====================================================================================
# calendar_rules: computed_holidays — Juneteenth gating / NY observance / caching / UK
# =====================================================================================

def test_us_juneteenth_only_from_2022():
    assert "2021-06-19" not in C.computed_holidays("US", 2021) and \
           "2021-06-18" not in C.computed_holidays("US", 2021)
    assert "2022-06-20" in C.computed_holidays("US", 2022)   # 19th is Sun -> observed Mon 20th


def test_us_new_year_sunday_observed_monday():
    # 2023-01-01 is a Sunday -> observed Monday 2023-01-02 (Sunday is observed forward)
    assert "2023-01-02" in C.computed_holidays("US", 2023)


def test_us_good_friday_present():
    # Easter 2026 = 2026-04-05 -> Good Friday 2026-04-03
    assert "2026-04-03" in C.computed_holidays("US", 2026)


def test_computed_holidays_unknown_key_is_empty():
    assert C.computed_holidays("ZZ", 2026) == set()


def test_computed_holidays_is_cached_same_object():
    a = C.computed_holidays("US", 2031)
    b = C.computed_holidays("US", 2031)
    assert a is b                       # cached per (key, year)


def test_uk_christmas_boxing_collision_resolves_to_distinct_days():
    # 2021-12-25 Sat, 2021-12-26 Sun -> both substitute to Mon 27 -> Boxing bumps to Tue 28
    uk = C.computed_holidays("UK", 2021)
    assert "2021-12-27" in uk
    assert "2021-12-28" in uk


# =====================================================================================
# calendar_rules: _calendar_key / _target_date / is_holiday / is_trading_day
# =====================================================================================

def test_calendar_key_fx_and_crypto_have_no_calendar():
    assert C._calendar_key(_asset("fx", tz="America/New_York")) is None
    assert C._calendar_key(_asset("crypto", tz="America/New_York")) is None


def test_calendar_key_tz_mapping():
    assert C._calendar_key(_asset("equity", tz="America/Chicago")) == "US"
    assert C._calendar_key(_asset("equity", tz="America/Toronto")) == "US"
    assert C._calendar_key(_asset("equity", tz="Europe/London")) == "UK"


def test_calendar_key_unknown_tz_is_none():
    assert C._calendar_key(_asset("equity", tz="Asia/Tokyo")) is None
    assert C._calendar_key(_asset("equity", tz=None)) is None


def test_target_date_naive_treated_as_utc():
    naive = datetime(2026, 6, 22, 4, 0)         # no tzinfo
    assert C._target_date(_asset("equity"), naive) == date(2026, 6, 22)


def test_target_date_converts_aware_to_utc_date():
    # 2026-06-22 23:00 at UTC-5 == 2026-06-23 04:00 UTC -> UTC date is the 23rd
    aware = datetime(2026, 6, 22, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert C._target_date(_asset("equity"), aware) == date(2026, 6, 23)


def test_is_holiday_fx_never_holiday_even_on_us_closure():
    # 2026-01-01 is a US market holiday, but FX has no exchange calendar
    assert C.is_holiday(_asset("fx", tz="America/New_York"), date(2026, 1, 1), {}) is False


def test_is_holiday_uses_computed_calendar_without_json():
    # MLK 2030 = 3rd Mon Jan = 2030-01-21
    assert C.is_holiday(_asset("equity", tz="America/New_York"), date(2030, 1, 21), {}) is True


def test_is_holiday_json_override_supplements_computed():
    a = _asset("equity", tz="America/New_York")
    one_off = date(2026, 6, 23)                  # not a computed holiday
    assert C.is_holiday(a, one_off, {}) is False
    assert C.is_holiday(a, one_off, {"US": {"2026-06-23"}}) is True


def test_is_trading_day_weekend_holiday_weekday():
    a = _asset("equity", tz="America/New_York")
    assert C.is_trading_day(a, date(2026, 6, 20), {}) is False   # Saturday
    assert C.is_trading_day(a, date(2026, 6, 22), {}) is True    # Monday
    assert C.is_trading_day(a, date(2026, 1, 1), {}) is False    # New Year's holiday


# =====================================================================================
# calendar_rules: _cadence_weekday parsing
# =====================================================================================

def test_cadence_weekday_bool_is_monday():
    # bool is an int subclass; must be rejected before the int branch
    assert C._cadence_weekday({"cadence_day": True}) == 0
    assert C._cadence_weekday({"cadence_day": False}) == 0


def test_cadence_weekday_int_in_range():
    assert C._cadence_weekday({"cadence_day": 3}) == 3
    assert C._cadence_weekday({"cadence_day": 6}) == 6


def test_cadence_weekday_int_out_of_range_defaults_monday():
    assert C._cadence_weekday({"cadence_day": 7}) == 0
    assert C._cadence_weekday({"cadence_day": -1}) == 0


@pytest.mark.parametrize("name,expected", [
    ("mon", 0), ("Tue", 1), ("WED", 2), ("thursday", 3), ("Fri", 4), ("sat", 5), ("sun", 6)])
def test_cadence_weekday_string_names(name, expected):
    assert C._cadence_weekday({"cadence_day": name}) == expected


def test_cadence_weekday_invalid_or_missing_defaults_monday():
    assert C._cadence_weekday({"cadence_day": "lunarday"}) == 0
    assert C._cadence_weekday({}) == 0


# =====================================================================================
# calendar_rules: _first_trading_day
# =====================================================================================

def test_first_trading_day_crypto_is_the_first():
    # crypto trades every day -> literally the 1st even if it is a weekend
    assert C._first_trading_day(_asset("crypto"), date(2026, 8, 15), {}) == date(2026, 8, 1)


def test_first_trading_day_equity_skips_weekend():
    # 2026-08-01 is a Saturday -> first trading day is Mon 2026-08-03
    a = _asset("equity", tz="America/New_York")
    assert C._first_trading_day(a, date(2026, 8, 20), {}) == date(2026, 8, 3)


def test_first_trading_day_equity_skips_new_year_holiday():
    # 2026-01-01 (Thu) is a holiday -> first trading day is Fri 2026-01-02
    a = _asset("equity", tz="America/New_York")
    assert C._first_trading_day(a, date(2026, 1, 15), {}) == date(2026, 1, 2)


# =====================================================================================
# calendar_rules: is_due — gaps (naive dt, defaults, crypto monthly, weekend anchor)
# =====================================================================================

def test_is_due_naive_datetime_treated_as_utc():
    # naive Monday 05:00 must be accepted as UTC and resolve due for an open-cadence asset
    due, _ = C.is_due(_asset("fx"), datetime(2026, 6, 22, 5, 0), holidays={})
    assert due is True


def test_is_due_default_cadence_is_open_weekday():
    a = {"id": "x", "enabled": True, "asset_class": "equity", "timezone": "America/New_York"}
    assert C.is_due(a, _at("2026-06-22 05:00"), holidays={})[0] is True   # Monday, no cadence key
    assert C.is_due(a, _at("2026-06-20 05:00"), holidays={})[0] is False  # Saturday


def test_is_due_crypto_monthly_only_on_first():
    a = _asset("crypto", cadence="monthly")
    assert C.is_due(a, _at("2026-06-01 05:00"), holidays={})[0] is True
    assert C.is_due(a, _at("2026-06-02 05:00"), holidays={})[0] is False


def test_is_due_weekly_weekend_cadence_day_anchors_to_monday_for_non_crypto():
    # a closed-market weekly must never anchor to a weekend cadence_day -> forced to Monday
    a = _asset("equity", cadence="weekly", tz="America/New_York", cadence_day="sat")
    assert C.is_due(a, _at("2026-06-22 05:00"), holidays={})[0] is True    # Monday
    assert C.is_due(a, _at("2026-06-20 05:00"), holidays={})[0] is False   # Saturday


def test_is_due_unknown_cadence_non_crypto_not_due_with_reason():
    due, reason = C.is_due(_asset("equity", cadence="biweekly", tz="America/New_York"),
                           _at("2026-06-22 05:00"), holidays={})
    assert due is False
    assert "unknown cadence" in reason


def test_is_due_returns_reason_for_weekend_rejection():
    due, reason = C.is_due(_asset("fx"), _at("2026-06-20 05:00"), holidays={})
    assert due is False
    assert "weekend" in reason


# =====================================================================================
# sessions: environment / structural invariants
# =====================================================================================

def test_zone_ok_returns_bool_and_matches_module_flag():
    assert isinstance(S._zone_ok(), bool)
    assert S._TZ_OK == S._zone_ok()


def test_get_session_unknown_profile_raises_keyerror():
    with pytest.raises(KeyError):
        S.get_session("nope_not_a_profile", now=_at("2026-06-22 05:00"))


def test_get_session_session_prose_is_a_copy():
    s = S.get_session("fx_spot", now=_at("2026-06-17 12:00"))
    s["session_prose"].append("MUTATED")
    assert "MUTATED" not in S.PROFILES["fx_spot"]["prose"]   # output must not alias the profile


def test_get_session_holidays_applied_is_sorted_strings():
    hol = {date(2026, 6, 18), date(2026, 6, 17)}
    s = S.get_session("us_equity_rth", now=_at("2026-06-16 14:00"), holiday_dates=hol)
    assert s["holidays_applied"] == ["2026-06-17", "2026-06-18"]


def test_get_session_default_now_returns_well_formed_dict():
    s = S.get_session("crypto_24_7")   # now defaults to datetime.now(UTC)
    for k in ("profile", "market_session_type", "market_state",
              "window_start_utc", "window_end_utc"):
        assert k in s
    assert s["profile"] == "crypto_24_7"


# =====================================================================================
# sessions: equity get_session edge branches (min_remaining boundary, after-hours)
# =====================================================================================

def _need_tz():
    if not S._TZ_OK:
        pytest.skip("zoneinfo/tzdata unavailable; venue-local boundary values not computable")


def test_equity_exactly_at_min_remaining_keeps_current_session():
    _need_tz()
    # Tue 2026-06-16, RTH 13:30-20:00 UTC (EDT). 18:30 -> exactly 90 min left -> stay current.
    s = S.get_session("us_equity_rth", now=_at("2026-06-16 18:30"), min_remaining_min=90)
    assert s["market_state"] == "open_regular_session"
    assert s["window_start_utc"] == "2026-06-16 18:30"


def test_equity_one_minute_under_threshold_targets_next_session():
    _need_tz()
    s = S.get_session("us_equity_rth", now=_at("2026-06-16 18:31"), min_remaining_min=90)
    assert s["market_state"] == "open_closing_soon"
    assert s["window_start_utc"] == "2026-06-17 13:30"     # next regular session opens Wed


def test_equity_after_hours_state_targets_next_day():
    _need_tz()
    s = S.get_session("us_equity_rth", now=_at("2026-06-16 21:00"))  # after the 20:00 close
    assert s["market_state"] == "after_hours"
    assert s["window_start_utc"] == "2026-06-17 13:30"


# =====================================================================================
# sessions: futures friday-cutoff branch + maintenance-break field behaviour
# =====================================================================================

def test_futures_friday_within_cutoff_targets_next_session():
    _need_tz()
    # Fri 2026-06-19 18:00 UTC -> 180 min to the 21:00 close < friday_cutoff (240) -> next session
    s = S.get_session("cme_futures", now=_at("2026-06-19 18:00"))
    assert s["market_state"] == "open_closing_soon"
    assert s["window_label"] == "next session (Sun reopen -> Mon close)"
    assert s["window_start_utc"] == "2026-06-21 22:00"
    assert s["window_end_utc"] == "2026-06-22 21:00"


def test_futures_open_session_surfaces_in_window_daily_maintenance():
    # FIXED: an OPEN futures session now SCANS its multi-day window for the next nightly maintenance
    # break (instead of only checking the window-end date, which is always the Fri weekly close). A
    # Wednesday-open session surfaces the Wednesday-evening break.
    _need_tz()
    s = S.get_session("cme_futures", now=_at("2026-06-17 12:00"))   # open Wednesday
    assert s["market_state"] == "open"
    nb = s["next_maintenance_break"]
    assert "daily maintenance" in nb and "2026-06-17" in nb and nb != "none before window end"


def test_fx_spot_has_no_maintenance_break():
    s = S.get_session("fx_spot", now=_at("2026-06-17 12:00"))
    assert s["next_maintenance_break"] == "none before window end"


# =====================================================================================
# sessions: get_window — normalization / long horizons / forecast_window tag
# =====================================================================================

def test_get_window_none_and_empty_forecast_return_base_session():
    m = _at("2026-06-17 14:00")
    base = S.get_session("fx_spot", now=m)
    assert S.get_window("fx_spot", now=m, forecast_window=None) == base
    assert S.get_window("fx_spot", now=m, forecast_window="") == base


def test_get_window_forecast_window_is_normalized_case_and_whitespace():
    m = _at("2026-06-15 05:00")
    w = S.get_window("crypto_24_7", now=m, forecast_window="  Next_Week ")
    assert w["forecast_window"] == "next_week"
    assert w["window_end_utc"] == "2026-06-22 05:00"     # +7 days from the rolling start


def test_get_window_equity_next_5_sessions_ends_on_fifth_rth_close():
    _need_tz()
    m = _at("2026-06-17 14:00")   # open Wednesday
    w = S.get_window("us_equity_rth", now=m, forecast_window="next_5_sessions")
    assert w["window_start_utc"] == "2026-06-17 14:00"
    # 4 trading days on from Wed 06-17 -> Tue 06-23, RTH close 20:00 UTC (EDT)
    assert w["window_end_utc"] == "2026-06-23 20:00"
    assert w["forecast_window"] == "next_5_sessions"


def test_get_window_next_liquid_session_non_overlapping_for_futures():
    _need_tz()
    mon = S.get_window("cme_futures", now=_at("2026-06-15 08:00"),
                       forecast_window="next_liquid_session")
    tue = S.get_window("cme_futures", now=_at("2026-06-16 08:00"),
                       forecast_window="next_liquid_session")
    assert mon["window_end_utc"] != tue["window_end_utc"]
    assert mon["window_end_utc"] <= tue["window_start_utc"]   # distinct, non-overlapping


# =====================================================================================
# sessions: private window helpers (_add_trading_days / _next_daily_close / _month_end_close)
# =====================================================================================

def test_add_trading_days_skips_weekend():
    # Fri 2026-06-19 + 1 trading day -> Mon 2026-06-22 (skips Sat/Sun)
    assert S._add_trading_days(date(2026, 6, 19), 1, set()) == date(2026, 6, 22)


def test_add_trading_days_skips_holidays():
    hol = {date(2026, 6, 18)}     # Thursday holiday
    # Wed 06-17 + 2 trading days: skip Thu(holiday) -> Fri 06-19 (1) -> Mon 06-22 (2)
    assert S._add_trading_days(date(2026, 6, 17), 2, hol) == date(2026, 6, 22)


def test_next_daily_close_is_after_start_with_guard():
    _need_tz()
    start = _at("2026-06-15 08:00")   # Monday
    close = S._next_daily_close("fx_spot", start, 90, set())
    assert close > start + timedelta(minutes=90)
    assert close == _at("2026-06-15 21:00")   # FX 17:00 ET == 21:00 UTC (EDT)


def test_month_end_close_equity_lands_on_last_weekday_of_month():
    _need_tz()
    # June 2026 ends Tue 30th (a weekday) -> equity RTH close 20:00 UTC
    end = S._month_end_close("us_equity_rth", _at("2026-06-10 14:00"), set())
    assert end == _at("2026-06-30 20:00")


def test_month_end_close_rolls_to_next_month_when_generated_at_month_end():
    _need_tz()
    # generated after June's last close -> must roll into July's last trading day
    end = S._month_end_close("fx_spot", _at("2026-06-30 23:00"), set())
    assert end.month == 7


# =====================================================================================
# sessions: get_cadence_window — crypto daily 21:00 retarget + month rollback behaviour
# =====================================================================================

def test_cadence_window_crypto_daily_retargets_to_2100_utc():
    w = S.get_cadence_window("crypto_24_7", "daily", now=_at("2026-06-27 04:00"))
    assert w["window_end_utc"] == "2026-06-27 21:00"
    assert w["scored_cadence"] == "daily"
    assert w["window_label"] == "to next 21:00 UTC daily close"


def test_cadence_window_crypto_daily_rolls_when_past_2100_minus_guard():
    # exactly 21:00 -> 21:00 <= now+90min -> roll to next day's 21:00
    w = S.get_cadence_window("crypto_24_7", "daily", now=_at("2026-06-27 21:00"))
    assert w["window_end_utc"] == "2026-06-28 21:00"


def test_cadence_window_crypto_monthly_ends_on_last_weekday_2359():
    # DOCUMENTS CURRENT behaviour: although crypto trades 24/7, _month_end_close rolls the
    # month-end back off the weekend -> May 2026 ends Sun 31, window lands Fri 29 23:59 UTC.
    w = S.get_cadence_window("crypto_24_7", "monthly", now=_at("2026-05-15 06:00"))
    assert w["window_end_utc"] == "2026-05-29 23:59"
    assert w["scored_cadence"] == "monthly"


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
