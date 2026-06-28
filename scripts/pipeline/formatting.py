"""Presentation helpers for the report pipeline: price decimal-places + UTC->London display strings.

Extracted from scaffold_payload so the formatting can be reused without importing the whole payload
compiler. Pure functions (no engine state)."""
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover - fallback for old stdlib
    _LONDON = None


def _dp(v):
    """Sensible decimal places for a price. FX majors (~1-2, e.g. 1.3406) need 4dp or
    adjacent pivots/bands collapse onto a single value (3 levels, no setups); JPY
    crosses / indices / metals / futures (>=10) read fine at 2dp; sub-1 FX/crypto at 5dp."""
    av = abs(v)
    if av >= 10:
        return 2
    if av >= 1:
        return 4
    return 5


def _to_london_dt(utc_str):
    try:
        dt = datetime.strptime(utc_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.astimezone(_LONDON) if _LONDON else dt + timedelta(hours=1)


def to_display(utc_str):
    """'YYYY-MM-DD HH:MM' UTC -> 'Mon 15 Jun 2026 14:30 UTC (15:30 BST)' -
    UTC primary (standard) with the London local time + abbrev alongside."""
    try:
        u = datetime.strptime(utc_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    loc = _to_london_dt(utc_str)
    base = f"{u:%a} {u.day} {u:%b %Y %H:%M} UTC"
    if loc is None:
        return base
    if _LONDON:
        ld = f"{loc:%H:%M %Z}"  # zoneinfo gives the correct BST/GMT abbrev
    else:
        # no tz database: approximate UK clock and label by season so winter never gets BST
        abbr = "BST" if 3 <= u.month <= 10 else "GMT"
        ld = f"{loc:%H:%M} {abbr}"
    return f"{base} ({ld})"


def _ld_short(utc_str):
    loc = _to_london_dt(utc_str)
    return f"{loc:%a %H:%M} UK" if loc else utc_str
