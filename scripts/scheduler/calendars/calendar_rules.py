"""Cadence + market-calendar resolver — decides which assets are DUE for a report.

The scheduler (run_daily.py), NOT Claude, decides what runs. `is_due()` combines the
asset's cadence with weekends and an optional holiday table. Conservative by design:
when unsure it returns due=False with a reason (skip-on-doubt); the approval gate and
the intraday freshness block are the backstops if a holiday is missing from the table.

Lightweight + dependency-free (no exchange_calendars). The interface (`is_due`,
`is_trading_day`) is stable so a heavier calendar library can be swapped in later.

Holidays are COMPUTED for any year (`computed_holidays`) from the standard market-holiday
rules — fixed dates, nth-weekday rules, Good Friday/Easter (computus), and the NYSE/LSE
weekend-observance rules — so the calendar never needs a hand-maintained table and stays
correct forever. config/holidays.json remains an OPTIONAL supplement/override for one-off
closures (e.g. an unscheduled exchange closure). Asset -> calendar key by timezone
(America/* -> US, Europe/London -> UK). FX and crypto use no exchange-holiday calendar
(FX trades through most; crypto is 24/7).
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_HOLIDAYS = Path("config/holidays.json")

_TZ_CALENDAR = {"America/New_York": "US", "America/Chicago": "US", "America/Los_Angeles": "US",
                "America/Toronto": "US", "Europe/London": "UK"}


def load_holidays(path=DEFAULT_HOLIDAYS):
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return {k: set(v) for k, v in data.items() if isinstance(v, list)}


# --- computed market holidays (US NYSE/CME, UK LSE) — generated for ANY year, so the holiday
# calendar never needs hand-maintaining and never silently lapses. ---------------------------

def _easter(year):
    """Gregorian Easter Sunday (Anonymous Gregorian / Meeus algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = ((h + ll - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (Mon=0 .. Sun=6) of month (n >= 1)."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _us_observed(d):
    """NYSE/CME observance: a holiday on Saturday is observed the preceding Friday; on Sunday,
    the following Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _uk_substitute(d):
    """UK bank-holiday substitute day: a weekend holiday rolls to the next weekday."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


_HOLIDAY_CACHE = {}


def computed_holidays(key, year):
    """Set of ISO date strings of full-day market closures for calendar `key` ('US' | 'UK')
    in `year`, generated from the standard rules. Half-days (early closes) are NOT included —
    those sessions still open. Cached per (key, year)."""
    ck = (key, year)
    if ck in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[ck]
    easter = _easter(year)
    out = set()
    if key == "US":
        days = [date(year, 1, 1),                       # New Year's Day
                _nth_weekday(year, 1, 0, 3),            # MLK Day (3rd Mon Jan)
                _nth_weekday(year, 2, 0, 3),            # Washington's Birthday (3rd Mon Feb)
                easter - timedelta(days=2),            # Good Friday
                _last_weekday(year, 5, 0),             # Memorial Day (last Mon May)
                date(year, 7, 4),                      # Independence Day
                _nth_weekday(year, 9, 0, 1),           # Labor Day (1st Mon Sep)
                _nth_weekday(year, 11, 3, 4),          # Thanksgiving (4th Thu Nov)
                date(year, 12, 25)]                    # Christmas
        if year >= 2022:
            days.append(date(year, 6, 19))             # Juneteenth (NYSE observed from 2022)
        for h in days:
            # New Year's Day falling on a Saturday is NOT observed (no preceding-Friday close).
            if h.month == 1 and h.day == 1 and h.weekday() == 5:
                continue
            out.add(_us_observed(h).isoformat())
    elif key == "UK":
        xmas = _uk_substitute(date(year, 12, 25))
        boxing = _uk_substitute(date(year, 12, 26))
        if boxing == xmas:                              # collision (e.g. 25th Sat, 26th Sun)
            boxing = _uk_substitute(xmas + timedelta(days=1))
        days = [_uk_substitute(date(year, 1, 1)),       # New Year's Day
                easter - timedelta(days=2),            # Good Friday
                easter + timedelta(days=1),            # Easter Monday
                _nth_weekday(year, 5, 0, 1),           # Early May Bank Holiday (1st Mon)
                _last_weekday(year, 5, 0),             # Spring Bank Holiday (last Mon May)
                _last_weekday(year, 8, 0),             # Summer Bank Holiday (last Mon Aug)
                xmas, boxing]
        out = {h.isoformat() for h in days}
    _HOLIDAY_CACHE[ck] = out
    return out


def _calendar_key(asset):
    if asset.get("asset_class") in ("fx", "crypto"):   # no exchange-holiday calendar
        return None
    return _TZ_CALENDAR.get(asset.get("timezone"))


def _target_date(asset, now):
    """Calendar date of the session this run targets = the run's UTC date.

    The daily timer fires at 04:00 UTC, BEFORE every supported venue opens that same UTC day
    (UK ~08:00 UTC, US ~14:30 UTC; crypto/FX have no exchange calendar), so the run's UTC date is
    the date of the session being prepared. Converting to the asset's LOCAL zone instead was a
    DST bug: at 04:00 UTC in EST winter a New York asset reads 23:00 the PREVIOUS day, so Mondays
    resolved to Sunday (rejected as weekend) and Saturdays to Friday (a spurious weekend report).
    `asset` is kept in the signature for callers; a venue that opens before ~04:00 UTC (e.g. Tokyo)
    would need session-open-aware targeting rather than the plain UTC date."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date()


def is_holiday(asset, d, holidays=None):
    key = _calendar_key(asset)
    if not key:
        return False
    iso = d.isoformat()
    if iso in computed_holidays(key, d.year):          # the standing, always-current calendar
        return True
    # config/holidays.json is an OPTIONAL supplement/override for one-off closures.
    holidays = holidays if holidays is not None else load_holidays()
    return iso in holidays.get(key, set())


def is_trading_day(asset, d, holidays=None):
    """Weekday and not an exchange holiday for this asset's calendar."""
    return d.weekday() < 5 and not is_holiday(asset, d, holidays)


_OPEN_CADENCES = ("daily", "weekday", "trading_day", "weekday_or_market_open")
_WD_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _cadence_weekday(asset):
    """Target weekday for a 'weekly' asset (Mon=0 .. Sun=6). Reads the optional `cadence_day`
    field (int 0-6 or a weekday name like 'mon'); defaults to Monday."""
    cd = asset.get("cadence_day")
    if isinstance(cd, bool):
        return 0
    if isinstance(cd, int) and 0 <= cd <= 6:
        return cd
    if isinstance(cd, str) and cd.strip().lower()[:3] in _WD_NAMES:
        return _WD_NAMES[cd.strip().lower()[:3]]
    return 0


def _first_trading_day(asset, d, holidays):
    """Date of the first trading day of d's month for this asset's calendar. Crypto: the 1st
    (it trades every day); everything else: the first weekday that isn't an exchange holiday."""
    first = d.replace(day=1)
    if (asset.get("asset_class") or "").lower() == "crypto":
        return first
    probe = first
    for _ in range(10):  # at most a handful of weekend/holiday hops into the new month
        if probe.weekday() < 5 and not is_holiday(asset, probe, holidays):
            return probe
        probe = probe + timedelta(days=1)
    return probe


def is_due(asset, now=None, holidays=None):
    """(due: bool, reason: str) — whether to GENERATE a report for `asset` now. `now` defaults
    to UTC now; naive datetimes are treated as UTC.

    GENERATION gate ONLY — scoring of already-closed prediction windows runs separately
    (run_daily.score_step) and is NEVER gated here, so Friday's calls are still graded over the
    weekend. Crypto trades 24/7 and is always due. EVERY other asset class (fx, equity, index,
    commodity, futures) has closed sessions — forex closes Fri night→Sun night; equities,
    commodities and futures close weekends + exchange holidays — so a CLOSED market produces no
    new report until it reopens, EVEN when cadence is 'daily'. The 05:00 UTC pre-session run
    targets that day's local session; weekends/holidays are rejected until the market reopens."""
    if not asset.get("enabled", True):
        return False, "disabled"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    holidays = holidays if holidays is not None else load_holidays()
    cls = (asset.get("asset_class") or "").lower()
    cadence = asset.get("cadence", "weekday")
    d = _target_date(asset, now)
    wd = d.weekday()  # Mon=0 .. Sun=6

    # Crypto: 24/7/365, always due for open cadences; scheduled cadences fire on their slot.
    if cls == "crypto":
        if cadence in _OPEN_CADENCES:
            return True, "crypto 24/7"
        if cadence == "weekly":
            twd = _cadence_weekday(asset)
            return (wd == twd, f"crypto weekly ({d.isoformat()})" if wd == twd
                    else f"weekly: due {list(_WD_NAMES)[twd]}, today {d:%a}")
        if cadence == "monthly":
            ftd = _first_trading_day(asset, d, holidays)
            return (d == ftd, f"crypto monthly ({d.isoformat()})" if d == ftd
                    else f"monthly: due {ftd.isoformat()}")
        return False, f"unknown cadence '{cadence}'"

    # Every other class closes — reject GENERATION when the market is shut (weekend / holiday).
    if wd >= 5:
        return False, f"market closed - weekend ({d:%a} {d.isoformat()})"
    if is_holiday(asset, d, holidays):
        return False, f"market closed - holiday ({d.isoformat()})"
    if cadence in _OPEN_CADENCES:
        return True, f"{cls or 'market'} session ({d.isoformat()})"
    if cadence == "weekly":
        twd = _cadence_weekday(asset)
        if twd >= 5:                       # never anchor a closed-market weekly to the weekend
            twd = 0
        return (wd == twd, f"{cls or 'market'} weekly ({d.isoformat()})" if wd == twd
                else f"weekly: due {list(_WD_NAMES)[twd]}, today {d:%a}")
    if cadence == "monthly":
        ftd = _first_trading_day(asset, d, holidays)
        return (d == ftd, f"{cls or 'market'} monthly ({d.isoformat()})" if d == ftd
                else f"monthly: due {ftd.isoformat()}")
    return False, f"unknown cadence '{cadence}'"


def next_due_at(asset, now=None, holidays=None, horizon_days=62):
    """The next UTC datetime `asset` is scheduled to GENERATE — the 05:00 UTC pre-session slot on the
    next day it is due (per is_due: cadence + weekends + holidays + market-closed) — or None if it is
    not due within horizon_days. A forward PROJECTION of is_due (the same gate the daily timer runs at
    05:00); it does NOT consider whether a report already exists for a slot. Disabled asset -> None."""
    if not asset.get("enabled", True):
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    holidays = holidays if holidays is not None else load_holidays()
    base = now.date()
    for i in range(horizon_days + 1):
        d = base + timedelta(days=i)
        slot = datetime(d.year, d.month, d.day, 5, 0, tzinfo=timezone.utc)  # 05:00 UTC pre-session run
        if slot <= now:
            continue
        if is_due(asset, slot, holidays)[0]:
            return slot
    return None
