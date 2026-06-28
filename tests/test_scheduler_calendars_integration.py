"""Phase-2 INTEGRATION tests for scripts/scheduler/calendars/{sessions,calendar_rules}.py.

These exercise the TWO calendar modules WIRED TOGETHER against the REAL asset universe
(config/assets.json), the way the scheduler actually drives them: calendar_rules.is_due
decides WHICH assets generate on a given `now`; sessions.get_session/get_window/
get_cadence_window decide the prediction WINDOW for that asset's session_profile. The unit
suite (test_scheduler_calendars_unit.py) already covers each function in isolation — this
file asserts the CROSS-MODULE FLOW + the data contracts between the two modules are coherent:

  * due/closed agreement: an asset that is_due()=True must NOT map to a closed_weekend
    session; a weekend/holiday skip must map to a closed/forward session.
  * window coherence: every DUE asset gets a non-degenerate, forward-facing window
    (start < end, end > now) for daily/weekly/monthly cadences.
  * the holiday DATA CONTRACT across the module boundary: calendar_rules emits ISO-string
    holidays keyed by US/UK; sessions consumes a flat set of date OBJECTS — bridging
    requires a shape conversion (and the raw string set is silently ignored: see notes).
  * DST coherence: both modules agree on WHICH calendar day is a trading day while
    sessions carries the UTC offset (EST vs EDT) — checked across the DST boundary.

Imports mirror the unit suite: sys.path -> tests/ (the conftest imports `scripts`, which
applies the subpackage sys.path shim so these flat imports resolve). Everything is
deterministic (fixed `now`, explicit/computed holiday sets) — no network, DB, subprocess.

Run:  python -m pytest tests/test_scheduler_calendars_integration.py -q
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions as S            # noqa: E402
import calendar_rules as C      # noqa: E402

UTC = timezone.utc
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _at(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def _parse(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


def _need_tz():
    if not S._TZ_OK:
        pytest.skip("zoneinfo/tzdata unavailable; venue-local UTC boundary values not computable")


def _load_universe():
    """The REAL asset universe — the single source of truth the scheduler iterates."""
    with open(os.path.join(ROOT, "config", "assets.json"), encoding="utf-8-sig") as fh:
        return [a for a in json.load(fh)["assets"] if a.get("enabled", True)]


REAL_ASSETS = _load_universe()
_CLOSED_STATES = ("closed_weekend", "closed_weekend_or_holiday")


def _ids(assets):
    return [a["id"] for a in assets]


# Sanity: the universe actually pairs the two modules' driving fields (asset_class/cadence/
# timezone for calendar_rules; session_profile for sessions) — if this drifts the rest is moot.
def test_universe_pairs_both_modules_fields():
    assert REAL_ASSETS, "no enabled assets in config/assets.json"
    for a in REAL_ASSETS:
        assert a["session_profile"] in S.PROFILES, \
            f"{a['id']}: session_profile {a['session_profile']!r} not a sessions.PROFILES key"
        assert a.get("asset_class"), f"{a['id']}: missing asset_class (calendar_rules input)"


# =====================================================================================
# Cross-module: a clean trading WEEKDAY -> due AND a forward, non-degenerate window
# =====================================================================================

@pytest.mark.parametrize("asset", REAL_ASSETS, ids=_ids(REAL_ASSETS))
def test_weekday_due_implies_open_forward_window(asset):
    # Mon 2026-06-22 05:00 UTC: a normal non-holiday weekday, the 05:00 pre-session slot.
    now = _at("2026-06-22 05:00")
    due, reason = C.is_due(asset, now, holidays={})
    assert due is True, f"{asset['id']} should be due on a clean Monday ({reason})"

    # the session this asset's profile reports must NOT be a weekend-closed state...
    sess = S.get_session(asset["session_profile"], now=now)
    assert not sess["market_state"].startswith("closed"), \
        f"{asset['id']} due but session state={sess['market_state']}"

    # ...and the canonical daily cadence window must be non-degenerate + forward-facing.
    w = S.get_cadence_window(asset["session_profile"], "daily", now=now)
    ws, we = _parse(w["window_start_utc"]), _parse(w["window_end_utc"])
    assert ws < we, f"{asset['id']} degenerate window {w['window_start_utc']}..{w['window_end_utc']}"
    assert we > now, f"{asset['id']} window already closed at generation ({w['window_end_utc']})"
    assert w["scored_cadence"] == "daily"


# =====================================================================================
# Cross-module: WEEKEND skip decision agrees with a closed/forward session
# =====================================================================================

@pytest.mark.parametrize("asset", REAL_ASSETS, ids=_ids(REAL_ASSETS))
def test_weekend_skip_agrees_with_session_state(asset):
    now = _at("2026-06-20 05:00")          # Saturday
    due, reason = C.is_due(asset, now, holidays={})
    sess = S.get_session(asset["session_profile"], now=now)
    if (asset.get("asset_class") or "").lower() == "crypto":
        # crypto is 24/7 -> due on the weekend AND the session reports open
        assert due is True and sess["market_state"] == "open"
    else:
        assert due is False and "weekend" in reason
        assert sess["market_state"].startswith("closed"), \
            f"{asset['id']} skipped for weekend but session state={sess['market_state']}"
        # a closed session still hands back a forward window (the next session)
        assert _parse(sess["window_start_utc"]) > now


# =====================================================================================
# Cross-module DATA CONTRACT: calendar_rules holiday table -> sessions holiday_dates.
# calendar_rules.computed_holidays() yields ISO STRINGS keyed by US/UK; sessions wants a
# flat set of date OBJECTS. The correct bridge (strings -> date objects) makes both modules
# agree the market is closed on a holiday and target the SAME next session.
# =====================================================================================

def test_holiday_bridge_due_gate_and_session_agree():
    # US Thanksgiving 2026-11-26 (Thu) — a computed full-day closure for the US calendar.
    now = _at("2026-11-26 14:00")
    assert "2026-11-26" in C.computed_holidays("US", 2026)

    aapl = {"id": "aapl", "enabled": True, "asset_class": "equity",
            "cadence": "trading_day", "timezone": "America/New_York"}
    due, reason = C.is_due(aapl, now, holidays={})
    assert due is False and "holiday" in reason

    # bridge the calendar_rules string set -> the date-object shape sessions consumes
    hol_dates = {date.fromisoformat(s) for s in C.computed_holidays("US", 2026)}
    sess = S.get_session("us_equity_rth", now=now, holiday_dates=hol_dates)
    assert sess["market_state"] == "closed_weekend_or_holiday"
    # the targeted next session must START strictly after the holiday date
    assert _parse(sess["window_start_utc"]).date() > now.date()
    # and the holiday we applied is surfaced in the metadata (sorted ISO strings)
    assert "2026-11-26" in sess["holidays_applied"]


def test_holiday_string_shape_is_silently_ignored_by_sessions():
    # CONTRACT GUARD / footgun: passing calendar_rules' RAW string set (the natural thing to
    # reach for) into sessions does NOT close the session, because sessions compares date()
    # objects against the set. This pins the cross-module shape mismatch (see notes/bugs).
    now = _at("2026-11-26 14:00")
    raw_strings = C.computed_holidays("US", 2026)          # {'2026-11-26', ...} ISO strings
    hol_dates = {date.fromisoformat(s) for s in raw_strings}

    s_str = S.get_session("us_equity_rth", now=now, holiday_dates=raw_strings)
    s_dt = S.get_session("us_equity_rth", now=now, holiday_dates=hol_dates)

    # date-object bridge correctly closes; the raw-string set is ignored -> they DISAGREE
    assert s_dt["market_state"] == "closed_weekend_or_holiday"
    assert s_str["market_state"] != "closed_weekend_or_holiday"
    assert s_str["window_start_utc"] != s_dt["window_start_utc"]


def test_holiday_bridged_into_cadence_window_skips_the_closed_day():
    # Wed before Thanksgiving: the equity daily window targets that day's RTH; the day-after
    # (Fri 11-27) is a half-day trading day, so the NEXT-session machinery must not land the
    # window on the Thu holiday. Drive get_session for Thu directly with the bridged holidays.
    hol_dates = {date.fromisoformat(s) for s in C.computed_holidays("US", 2026)}
    thu = S.get_session("us_equity_rth", now=_at("2026-11-26 06:00"), holiday_dates=hol_dates)
    # closed on the holiday, next session is NOT the holiday date itself
    assert _parse(thu["window_start_utc"]).date() != date(2026, 11, 26)
    assert _parse(thu["window_start_utc"]) < _parse(thu["window_end_utc"])


# =====================================================================================
# Cross-module DST coherence: both modules agree which day trades; sessions carries offset
# =====================================================================================

def test_dst_due_and_session_offset_agree_equity():
    _need_tz()
    aapl = {"id": "aapl", "enabled": True, "asset_class": "equity",
            "cadence": "trading_day", "timezone": "America/New_York"}
    winter = _at("2026-01-13 12:00")     # Tue, EST
    summer = _at("2026-06-16 12:00")     # Tue, EDT
    assert C.is_due(aapl, winter, holidays={})[0] is True
    assert C.is_due(aapl, summer, holidays={})[0] is True
    sw = S.get_session("us_equity_rth", now=winter)
    ss = S.get_session("us_equity_rth", now=summer)
    # 09:30 ET -> 14:30 UTC (EST) vs 13:30 UTC (EDT): both modules agree it's a trading day,
    # sessions shifts the UTC bound by exactly the 1h DST offset.
    assert sw["market_open_utc"].startswith("2026-01-13 14:30")
    assert ss["market_open_utc"].startswith("2026-06-16 13:30")


def test_dst_futures_close_shifts_one_hour():
    _need_tz()
    es = {"id": "es", "enabled": True, "asset_class": "index",
          "cadence": "trading_day", "timezone": "America/New_York"}
    winter = _at("2026-01-13 12:00")
    summer = _at("2026-06-16 12:00")
    assert C.is_due(es, winter, holidays={})[0] is True
    assert C.is_due(es, summer, holidays={})[0] is True
    # CME 16:00 CT weekly close -> 22:00 UTC (winter) vs 21:00 UTC (summer)
    assert S.get_session("cme_futures", now=winter)["market_close_utc"].endswith("22:00")
    assert S.get_session("cme_futures", now=summer)["market_close_utc"].endswith("21:00")


# =====================================================================================
# Cross-module: monthly cadence — calendar_rules._first_trading_day picks the due day,
# sessions._month_end_close picks the window end; assert they bracket a coherent period.
# =====================================================================================

@pytest.mark.parametrize("month", [1, 2, 3, 7, 9, 11])
def test_monthly_due_day_and_window_end_bracket_the_month(month):
    _need_tz()
    aapl = {"id": "aapl", "enabled": True, "asset_class": "equity",
            "cadence": "monthly", "timezone": "America/New_York"}
    ftd = C._first_trading_day(aapl, date(2026, month, 1), {})
    now = datetime(ftd.year, ftd.month, ftd.day, 5, 0, tzinfo=UTC)
    due, _ = C.is_due(aapl, now, holidays={})
    assert due is True, f"month {month}: monthly asset should be due on its first trading day"

    w = S.get_cadence_window("us_equity_rth", "monthly", now=now)
    we = _parse(w["window_end_utc"])
    assert w["scored_cadence"] == "monthly"
    # the window must end inside the SAME month, after the first-trading-day generation moment
    assert we.month == month and we.year == 2026
    assert we > now
    # and the window-end day must itself be a trading day (last weekday of the month)
    assert we.weekday() < 5


# =====================================================================================
# Cross-module: weekly cadence anchor (calendar_rules) lines up with the weekly window
# (sessions). Due only on the anchor weekday; the window spans to that week's end close.
# =====================================================================================

def test_weekly_anchor_day_aligns_with_weekly_window():
    eq_weekly = {"id": "spx_w", "enabled": True, "asset_class": "equity",
                 "cadence": "weekly", "timezone": "America/New_York", "cadence_day": "mon"}
    mon = _at("2026-06-22 05:00")
    tue = _at("2026-06-23 05:00")
    assert C.is_due(eq_weekly, mon, holidays={})[0] is True       # anchor day -> due
    assert C.is_due(eq_weekly, tue, holidays={})[0] is False      # off-anchor -> skip

    w = S.get_cadence_window("us_equity_rth", "weekly", now=mon)
    ws, we = _parse(w["window_start_utc"]), _parse(w["window_end_utc"])
    assert w["scored_cadence"] == "weekly"
    assert ws < we and we > mon
    # the weekly window must close within the SAME Mon-Fri trading week (by Friday)
    assert we.date() <= mon.date() + timedelta(days=4)
    assert we.weekday() <= 4


# =====================================================================================
# Cross-module: consecutive DUE weekdays on a 24/5 venue own DISTINCT, non-overlapping
# daily windows (is_due selects the days; get_window/get_cadence_window owns the bounds).
# This is the calibration invariant that one outcome isn't counted by two reports.
# =====================================================================================

def test_consecutive_due_days_give_nonoverlapping_daily_windows_fx():
    _need_tz()
    gbp = {"id": "gbpusd", "enabled": True, "asset_class": "fx",
           "cadence": "weekday", "timezone": "Europe/London"}
    # a clean Mon-Fri week (2026-06-22 .. 06-26), all due, no holiday
    prev_end = None
    for i in range(5):
        now = _at("2026-06-22 08:00") + timedelta(days=i)
        assert C.is_due(gbp, now, holidays={})[0] is True
        w = S.get_cadence_window("fx_spot", "daily", now=now)
        ws, we = _parse(w["window_start_utc"]), _parse(w["window_end_utc"])
        assert ws < we
        if prev_end is not None:
            # today's window starts at/after yesterday's close -> no double-counted outcome
            assert ws >= prev_end, f"day {i}: window overlaps previous ({w['window_start_utc']})"
        prev_end = we


# =====================================================================================
# Cross-module YEAR SWEEP: the core invariant over every day of 2026 at the 05:00 slot —
# a DUE non-crypto asset is never mapped to a weekend-closed session and always gets a
# forward daily window; a skipped non-crypto day is always a weekend or a holiday.
# =====================================================================================

@pytest.mark.parametrize("asset", [
    {"id": "es", "enabled": True, "asset_class": "index",
     "cadence": "trading_day", "timezone": "America/New_York", "profile": "cme_futures"},
    {"id": "aapl", "enabled": True, "asset_class": "equity",
     "cadence": "trading_day", "timezone": "America/New_York", "profile": "us_equity_rth"},
    {"id": "gbpusd", "enabled": True, "asset_class": "fx",
     "cadence": "weekday", "timezone": "Europe/London", "profile": "fx_spot"},
], ids=["es", "aapl", "gbpusd"])
def test_year_sweep_due_implies_forward_window_else_closed_reason(asset):
    profile = asset["profile"]
    cal_key = C._calendar_key(asset)                      # 'US' for es/aapl, None for fx
    hol_dates = set()
    if cal_key:
        hol_dates = {date.fromisoformat(s) for s in C.computed_holidays(cal_key, 2026)}
    d = date(2026, 1, 1)
    for _ in range(365):
        now = datetime(d.year, d.month, d.day, 5, 0, tzinfo=UTC)
        due, reason = C.is_due(asset, now, holidays={})
        if due:
            sess = S.get_session(profile, now=now, holiday_dates=hol_dates)
            assert not sess["market_state"].startswith("closed"), \
                f"{asset['id']} {d}: due but session={sess['market_state']}"
            w = S.get_cadence_window(profile, "daily", now=now)
            assert _parse(w["window_start_utc"]) < _parse(w["window_end_utc"]), \
                f"{asset['id']} {d}: degenerate daily window"
            assert _parse(w["window_end_utc"]) > now, f"{asset['id']} {d}: window already closed"
        else:
            # the only reasons a non-crypto asset is skipped are weekend or holiday
            assert ("weekend" in reason) or ("holiday" in reason), \
                f"{asset['id']} {d}: unexpected skip reason {reason!r}"
        d += timedelta(days=1)


if __name__ == "__main__":
    sys.exit(pytest.main([os.path.abspath(__file__), "-q"]))
