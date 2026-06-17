"""Cadence + market-calendar resolver — decides which assets are DUE for a report.

The scheduler (run_daily.py), NOT Claude, decides what runs. `is_due()` combines the
asset's cadence with weekends and an optional holiday table. Conservative by design:
when unsure it returns due=False with a reason (skip-on-doubt); the approval gate and
the intraday freshness block are the backstops if a holiday is missing from the table.

Lightweight + dependency-free (no exchange_calendars). The interface (`is_due`,
`is_trading_day`) is stable so a heavier calendar library can be swapped in later.

Holiday table: config/holidays.json = {"US": ["2026-01-01", ...], "UK": [...]}.
Asset -> calendar key by timezone (America/* -> US, Europe/London -> UK). FX and
crypto use no exchange-holiday calendar (FX trades through most; crypto is 24/7).
"""
import json
from datetime import datetime, timezone
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


def _calendar_key(asset):
    if asset.get("asset_class") in ("fx", "crypto"):   # no exchange-holiday calendar
        return None
    return _TZ_CALENDAR.get(asset.get("timezone"))


def _target_date(asset, now):
    """Local calendar date of the session this run targets. A 06:00 Europe/London run
    maps to the same local date in US zones (NY ~01:00), so the run date in the asset's
    zone is the right basis for the daily cadence decision."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo(asset.get("timezone", "UTC"))).date()
    except Exception:
        return now.astimezone(timezone.utc).date()


def is_holiday(asset, d, holidays=None):
    key = _calendar_key(asset)
    if not key:
        return False
    holidays = holidays if holidays is not None else load_holidays()
    return d.isoformat() in holidays.get(key, set())


def is_trading_day(asset, d, holidays=None):
    """Weekday and not an exchange holiday for this asset's calendar."""
    return d.weekday() < 5 and not is_holiday(asset, d, holidays)


def is_due(asset, now=None, holidays=None):
    """(due: bool, reason: str) — whether to GENERATE a report for `asset` now.
    `now` defaults to UTC now; naive datetimes are treated as UTC."""
    if not asset.get("enabled", True):
        return False, "disabled"
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    holidays = holidays if holidays is not None else load_holidays()
    cadence = asset.get("cadence", "weekday")
    d = _target_date(asset, now)
    wd = d.weekday()  # Mon=0 .. Sun=6

    if cadence == "daily":
        return True, "daily cadence (24/7)"
    if wd >= 5:                                   # every other cadence is weekday-gated
        return False, f"weekend ({d:%a} {d.isoformat()})"
    if cadence == "weekday":
        return True, "weekday cadence"
    if cadence in ("trading_day", "weekday_or_market_open"):
        if is_holiday(asset, d, holidays):
            return False, f"market holiday ({d.isoformat()})"
        return True, f"{cadence} (open {d.isoformat()})"
    return False, f"unknown cadence '{cadence}'"
