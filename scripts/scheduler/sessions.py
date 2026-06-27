"""Asset-specific session rules for AssetFrame reports.

Profiles encode venue session logic (UTC, valid for the current DST regime —
June 2026: US on EDT, so CME closes 21:00 UTC; re-check at DST changes).
`get_session(profile, now)` returns market state, session bounds, the next
maintenance break, and a NEXT-SESSION prediction window when the current
session is closed or nearly over — the product is next-session intelligence.

Holiday handling: pass `holiday_dates` (set of date objects, web-verified at
run time) to skip closed days. Metadata must record what was applied.
"""
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

# Session boundaries are defined in the venue's LOCAL time (*_local + tz) and converted to UTC
# per-date via zoneinfo, so DST is handled automatically forever. The *_local times are the real
# exchange clock (CME 16:00 CT close, NYSE 09:30-16:00 ET, FX 17:00 ET close). The plain UTC
# tuples are a no-zoneinfo fallback (current-regime values) and are otherwise unused.
PROFILES = {
    # CME Globex metals/energy/equity-index: Sun 17:00 CT open, Fri 16:00 CT weekly close,
    # daily maintenance 16:00-17:00 CT Mon-Thu.
    "cme_futures": {
        "label": "CME Globex futures (23h sessions; daily maintenance 16:00-17:00 CT)",
        "type": "futures_23h",
        "tz": "America/Chicago",
        "weekly_close": ("FRI", "21:00"), "weekly_close_local": ("FRI", "16:00"),
        "weekly_open": ("SUN", "22:00"), "weekly_open_local": ("SUN", "17:00"),
        "daily_break": ("21:00", "22:00"), "daily_break_local": ("16:00", "17:00"),
        "prose": [
            "CME Globex: ~23h/day, Sun 17:00 CT -> Fri 16:00 CT, daily maintenance 16:00-17:00 CT (Mon-Thu). UTC equivalents shift ~1h with US daylight saving.",
            "Weekly close Friday 16:00 CT; weekend headlines land while the market is shut - gap risk realises at the Sunday 17:00 CT reopen.",
            "Front-month continuous series used; contract month labelled in metadata. Roll risk flagged when within ~1 week of expiry.",
        ],
    },
    # Spot FX: ~24/5, Sun ~17:05 ET open to Fri 17:00 ET (5pm New York) close.
    "fx_spot": {
        "label": "Spot FX 24/5 (Sun ~17:05 ET -> Fri 17:00 ET, the 5pm New York close)",
        "type": "fx_24_5",
        "tz": "America/New_York",
        "weekly_close": ("FRI", "21:00"), "weekly_close_local": ("FRI", "17:00"),
        "weekly_open": ("SUN", "21:05"), "weekly_open_local": ("SUN", "17:05"),
        "daily_break": None,
        "prose": [
            "Spot FX trades ~24/5: Sun ~17:05 ET -> Fri 17:00 ET (UTC shifts with US DST). Sessions: Asia, London and New York roll continuously around the clock.",
            "The rollover/value-date window around the 17:00 ET close is illiquid - no fresh entries there unless explicitly labelled.",
            "Weekend gaps are routine around geopolitical headlines; windows end at the weekly close unless modelling the gap.",
        ],
    },
    # US single stock / ETF (Nasdaq/NYSE): regular session 09:30-16:00 ET; pre/after-hours
    # trade thin. The prediction window targets the NEXT REGULAR session only.
    "us_equity_rth": {
        "label": "US equity (Nasdaq/NYSE) - regular session 09:30-16:00 ET",
        "type": "equity_rth",
        "tz": "America/New_York",
        "rth": ("13:30", "20:00"), "rth_local": ("09:30", "16:00"),
        "prose": [
            "US regular session 09:30-16:00 ET (UTC shifts with US daylight saving); pre-market and after-hours trade thin.",
            "Tradable levels in this report are REGULAR-SESSION levels (unadjusted prices); extended-hours prints are labelled separately and must not be mixed with them.",
            "The prediction window is the next REGULAR session only; pre-market gaps realise at the opening bell. Weekends/holidays skipped via the exchange calendar.",
            "Earnings before the open or after the close define their own risk window - flagged on the timeline when within ~3 weeks.",
        ],
    },
    # Crypto spot: 24/7, no close language; artificial sessions = UTC day or
    # rolling 24h; perp funding windows 00/08/16 UTC noted for context.
    "crypto_24_7": {
        "label": "Crypto spot 24/7 (rolling windows; no market close)",
        "type": "crypto_24_7",
        "weekly_close": None,
        "weekly_open": None,
        "daily_break": None,
        "prose": [
            "Crypto trades 24/7 - there is no market close; windows are rolling (this report uses a rolling 24h window from generation).",
            "Perp funding windows (00:00/08:00/16:00 UTC on major venues) often cluster volatility; weekend liquidity is thinner and venue outages are a real risk.",
            "Spot pricing here is an aggregate (Yahoo BTC-USD) cross-checked against CoinGecko; venue-specific prints can differ.",
        ],
    },
}

_WD = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _zone_ok():
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        return True
    except Exception:           # bare box without an IANA tz database (the OCI Linux box has one)
        return False


_TZ_OK = _zone_ok()


def _local_to_utc(d, hhmm, zone):
    """UTC datetime for local time hhmm (HH:MM) on calendar date `d` in IANA `zone`,
    DST-correct via zoneinfo."""
    from zoneinfo import ZoneInfo
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return datetime(d.year, d.month, d.day, h, m, tzinfo=ZoneInfo(zone)).astimezone(UTC)


def _next_weekday_time(now, wd_name, hhmm, forward=True):
    """Next occurrence (>= now if forward) of weekday wd_name at UTC time hhmm. The
    no-zoneinfo fallback path (the times are then interpreted as UTC)."""
    wd, (h, m) = _WD[wd_name], (int(hhmm[:2]), int(hhmm[3:5]))
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (wd - now.weekday()) % 7
    cand = cand + timedelta(days=delta)
    if forward and cand <= now:
        cand += timedelta(days=7)
    return cand


def _profile_weekday_time(now, profile_key, key, forward=True):
    """UTC instant of the next occurrence of a profile boundary (profile[key] = (WEEKDAY,'HH:MM'))
    — interpreted in the venue's LOCAL zone (DST-correct) when zoneinfo is available, else as the
    stored UTC fallback. `key` is 'weekly_close' or 'weekly_open'."""
    p = PROFILES[profile_key]
    local, zone = p.get(key + "_local"), p.get("tz")
    if not (_TZ_OK and local and zone):
        return _next_weekday_time(now, *p[key], forward=forward)
    wd_name, hhmm = local
    base = now.date()
    d = base + timedelta(days=(_WD[wd_name] - base.weekday()) % 7)
    cand = _local_to_utc(d, hhmm, zone)
    if forward and cand <= now:
        cand = _local_to_utc(d + timedelta(days=7), hhmm, zone)
    return cand


def _local_close_on(profile_key, d, hhmm_key="weekly_close"):
    """UTC instant of the venue's local daily/weekly close time on calendar date `d`."""
    p = PROFILES[profile_key]
    local, zone = p.get(hhmm_key + "_local"), p.get("tz")
    if _TZ_OK and local and zone:
        return _local_to_utc(d, local[1], zone)
    return datetime(d.year, d.month, d.day, int(p[hhmm_key][1][:2]),
                    int(p[hhmm_key][1][3:5]), tzinfo=UTC)


def _equity_rth(profile_key, d):
    """(open_utc, close_utc) of the regular session on calendar date `d`, DST-correct."""
    p = PROFILES[profile_key]
    local, zone = p.get("rth_local"), p.get("tz")
    if _TZ_OK and local and zone:
        return _local_to_utc(d, local[0], zone), _local_to_utc(d, local[1], zone)
    base = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return (base.replace(hour=int(p["rth"][0][:2]), minute=int(p["rth"][0][3:5])),
            base.replace(hour=int(p["rth"][1][:2]), minute=int(p["rth"][1][3:5])))


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def get_session(profile_key, now=None, min_remaining_min=90, friday_cutoff_min=240,
                holiday_dates=None):
    """Return session info + the prediction window the report should target.

    Window policy: target the CURRENT session only if it is open with
    comfortably more than `min_remaining_min` left (and more than
    `friday_cutoff_min` before a weekly close); otherwise target the NEXT full
    session. Crypto: rolling 24h from now.
    """
    p = PROFILES[profile_key]
    now = (now or datetime.now(UTC)).astimezone(UTC)
    holiday_dates = holiday_dates or set()
    out = {
        "profile": profile_key,
        "market_session_type": p["label"],
        "session_prose": list(p["prose"]),
        "holidays_applied": sorted(str(d) for d in holiday_dates),
    }

    if p["type"] == "equity_rth":
        rth_open, rth_close = _equity_rth(profile_key, now.date())
        in_rth = (now.weekday() < 5 and now.date() not in holiday_dates
                  and rth_open <= now < rth_close)
        remaining = (rth_close - now).total_seconds() / 60 if in_rth else 0
        if in_rth and remaining >= min_remaining_min:
            start, end = now, rth_close
            state, label = "open_regular_session", "remainder of current regular session"
        else:
            nxt = now + timedelta(days=1)
            while nxt.weekday() >= 5 or nxt.date() in holiday_dates:
                nxt += timedelta(days=1)
            if not in_rth and now.weekday() < 5 and now < rth_open \
                    and now.date() not in holiday_dates:
                nxt = now  # later today
            start, end = _equity_rth(profile_key, nxt.date())
            if now.weekday() >= 5 or now.date() in holiday_dates:
                state = "closed_weekend_or_holiday"
            elif now < rth_open:
                state = "pre_market"
            elif in_rth:
                state = "open_closing_soon"
            else:
                state = "after_hours"
            label = "next regular session"
        out.update({
            "market_state": state,
            "market_open_utc": _fmt(start) + " (regular session open)",
            "market_close_utc": _fmt(end),
            "next_maintenance_break": "n/a - no maintenance breaks; pre/after-hours trade thin and are labelled",
            "window_label": label,
            "window_start_utc": _fmt(start),
            "window_end_utc": _fmt(end),
        })
        return out

    if p["type"] == "crypto_24_7":
        out.update({
            "market_state": "open",
            "market_open_utc": "continuous (24/7)",
            "market_close_utc": "none - market does not close",
            "next_maintenance_break": "none scheduled (venue-dependent)",
            "window_label": "rolling 24h from generation",
            "window_start_utc": _fmt(now),
            "window_end_utc": _fmt(now + timedelta(hours=24)),
        })
        return out

    wc = _profile_weekday_time(now, profile_key, "weekly_close")
    wo = _profile_weekday_time(now, profile_key, "weekly_open")
    # weekend = between the most recent Friday close and the Sunday open after it
    prev_close = wc - timedelta(days=7) if wc > now else wc
    sun_open_after_prev = _profile_weekday_time(prev_close + timedelta(minutes=1),
                                                profile_key, "weekly_open")
    in_weekend = prev_close <= now < sun_open_after_prev
    remaining = (wc - now).total_seconds() / 60 if not in_weekend else 0
    is_friday = now.weekday() == 4

    def _skip_holidays(d):
        while d.date() in holiday_dates:
            d += timedelta(days=1)
        return d

    if in_weekend or remaining < min_remaining_min or (is_friday and remaining < friday_cutoff_min):
        # NEXT full session: Sunday open -> the venue's daily close on the Monday after it
        # (CME 16:00 CT / FX 17:00 ET — DST-correct via _local_close_on).
        start = wo if wo > now else _profile_weekday_time(now, profile_key, "weekly_open")
        start = _skip_holidays(start)
        end = _local_close_on(profile_key, (start + timedelta(days=1)).date(), "weekly_close")
        out["market_state"] = "closed_weekend" if in_weekend else "open_closing_soon"
        out["window_label"] = "next session (Sun reopen -> Mon close)"
    else:
        start, end = now, wc
        out["market_state"] = "open"
        out["window_label"] = "remainder of current session"

    nb = "none before window end"
    if p.get("daily_break") and end.weekday() in (0, 1, 2, 3):
        if _TZ_OK and p.get("daily_break_local") and p.get("tz"):
            b0 = _local_to_utc(end.date(), p["daily_break_local"][0], p["tz"])
            brk = f"{p['daily_break_local'][0]}-{p['daily_break_local'][1]} CT"
        else:
            b0 = end.replace(hour=int(p["daily_break"][0][:2]), minute=int(p["daily_break"][0][3:5]))
            brk = f"{p['daily_break'][0]}-{p['daily_break'][1]} UTC"
        if start < b0 < end:
            nb = f"{_fmt(b0)} UTC daily maintenance ({brk})"
        else:
            nb = f"next daily break {_fmt(b0)} UTC ({brk}, at/after window end)"
    out.update({
        "market_open_utc": _fmt(wo - timedelta(days=7)) if not in_weekend and wo - timedelta(days=7) <= now else _fmt(wo),
        "market_close_utc": _fmt(wc),
        "next_maintenance_break": nb,
        "window_start_utc": _fmt(start),
        "window_end_utc": _fmt(end),
    })
    return out


# --- longer-horizon windows -------------------------------------------------
# get_window() is the horizon-aware front door to get_session(). For the standard next-session
# forecast windows it returns get_session() UNCHANGED (so the live universe is byte-identical);
# for the longer horizons it only EXTENDS window_end_utc to span multiple sessions. Scoring
# (score_report.py) is generic on the window bounds, so a longer window scores with no other
# change. start / market_state / prose are left as the base session's.
LONG_WINDOWS = ("next_week", "next_5_sessions")


def _add_trading_days(d, n, holiday_dates):
    """Date n trading days after date d (skipping weekends + holidays). n >= 1."""
    count = 0
    while count < n:
        d = d + timedelta(days=1)
        if d.weekday() >= 5 or d in holiday_dates:
            continue
        count += 1
    return d


def _next_daily_close(profile_key, start, min_remaining_min, holiday_dates):
    """UTC instant of the next DAILY liquidity close at/after `start` (+ a min_remaining guard),
    skipping weekends and holidays. Uses the profile's local close TIME (spot FX 17:00 ET, CME
    futures 16:00 CT) applied to each candidate trading date, DST-correct via _local_close_on."""
    zone = PROFILES[profile_key].get("tz")
    if _TZ_OK and zone:
        from zoneinfo import ZoneInfo
        d = start.astimezone(ZoneInfo(zone)).date()
    else:
        d = start.date()
    guard = timedelta(minutes=min_remaining_min)
    for _ in range(14):                      # bounded scan; weekend/holiday runs are short
        if d.weekday() < 5 and d not in holiday_dates:
            cand = _local_close_on(profile_key, d, "weekly_close")
            if cand > start + guard:
                return cand
        d += timedelta(days=1)
    return start + timedelta(days=1)         # pathological fallback (never hit in practice)


def get_window(profile_key, now=None, forecast_window=None, holiday_dates=None,
               min_remaining_min=90, friday_cutoff_min=240):
    """Resolve the prediction window for a given forecast horizon. Standard windows delegate
    unchanged to get_session(); 'next_week' / 'next_5_sessions' extend the END to a multi-session
    horizon. Unknown / empty forecast_window -> the base next-session window."""
    base = get_session(profile_key, now=now, min_remaining_min=min_remaining_min,
                       friday_cutoff_min=friday_cutoff_min, holiday_dates=holiday_dates)
    fw = (forecast_window or "").strip().lower()
    holiday_dates = holiday_dates or set()
    ptype = PROFILES[profile_key]["type"]

    # next_liquid_session on a 24/5 venue (spot FX, futures): get_session targets the WEEKLY close,
    # so EVERY daily report in a week ends on the same Friday -> overlapping windows that count the
    # same outcome multiple times in calibration. Re-target the end to the next DAILY liquidity
    # close (FX 17:00 ET / futures 16:00 CT) so each day's report owns a distinct ~1-session,
    # non-overlapping window. Equity (next_regular_session) and crypto (rolling_24h) already get a
    # correct ~1-day window from get_session, so they fall through unchanged.
    if fw == "next_liquid_session" and ptype in ("fx_24_5", "futures_23h"):
        start = datetime.strptime(base["window_start_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        end = _next_daily_close(profile_key, start, min_remaining_min, holiday_dates)
        out = dict(base)
        out["window_end_utc"] = _fmt(end)
        out["window_label"] = "next liquid session (to daily close)"
        out["forecast_window"] = fw
        return out

    if fw not in LONG_WINDOWS:
        return base
    start = datetime.strptime(base["window_start_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    end = datetime.strptime(base["window_end_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
    if ptype == "crypto_24_7":
        end = start + timedelta(days=7 if fw == "next_week" else 5)
    elif ptype == "equity_rth":
        # a trading week = 5 regular sessions; end at the 5th session's RTH close (DST-correct)
        close_date = _add_trading_days(start.date(), 4, holiday_dates)
        end = _equity_rth(profile_key, close_date)[1]
    else:  # 24h-ish futures / FX -> the venue's daily close (DST-correct via _local_close_on)
        if fw == "next_week":
            fri = start.date() + timedelta(days=(4 - start.weekday()) % 7)
            end = _local_close_on(profile_key, fri, "weekly_close")
            if end <= start:
                end = _local_close_on(profile_key, fri + timedelta(days=7), "weekly_close")
        else:
            close_date = _add_trading_days(start.date(), 4, holiday_dates)
            end = _local_close_on(profile_key, close_date, "weekly_close")
    if end <= start:                      # never emit a degenerate window — and surface it
        import sys as _sys
        print(f"WARNING: degenerate {fw} window for {profile_key} (end<=start); extended +1 day",
              file=_sys.stderr)
        end = start + timedelta(days=1)
    out = dict(base)
    out["window_end_utc"] = _fmt(end)
    out["window_label"] = f"{fw.replace('_', ' ')} horizon (multi-session)"
    out["forecast_window"] = fw
    return out


# --- cadence windows --------------------------------------------------------
# One prediction set per cadence PERIOD: a daily report scores at the next day/session
# close, weekly at the week-end close, monthly at the month-end close. This is the canonical
# window the ledger row is keyed to. Built on the same get_session/get_window primitives so
# the existing scoring (generic on window bounds) and holiday/DST handling are reused.
CADENCE_WINDOWS = ("daily", "weekly", "monthly")


def _month_end_close(profile_key, start, holiday_dates):
    """UTC instant of the close on the last trading day of start's month; if that is already
    at/before `start` (generated near month-end), roll to next month's end."""
    from calendar import monthrange
    ptype = PROFILES[profile_key]["type"]
    holiday_dates = holiday_dates or set()
    d = start.date()
    for _ in range(2):                       # this month, else next month
        md = d.replace(day=monthrange(d.year, d.month)[1])
        while md.weekday() >= 5 or md in holiday_dates:
            md -= timedelta(days=1)
        if ptype == "equity_rth":
            end = _equity_rth(profile_key, md)[1]
        elif ptype == "crypto_24_7":
            end = datetime(md.year, md.month, md.day, 23, 59, tzinfo=UTC)
        else:
            end = _local_close_on(profile_key, md, "weekly_close")
        if end > start:
            return end
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)   # first of next month
    return start + timedelta(days=30)        # pathological fallback


def get_cadence_window(profile_key, cadence, now=None, holiday_dates=None,
                       min_remaining_min=90, friday_cutoff_min=240):
    """Resolve the canonical per-period prediction window for an asset's generation cadence.
    daily -> next session/daily-liquidity close; weekly -> week-end; monthly -> month-end.
    Returns the get_session dict with window_end_utc set to the period close + a 'scored_cadence' tag."""
    cadence = (cadence or "daily").strip().lower()
    ptype = PROFILES[profile_key]["type"]
    kw = dict(now=now, holiday_dates=holiday_dates, min_remaining_min=min_remaining_min,
              friday_cutoff_min=friday_cutoff_min)
    if cadence == "weekly":
        out = get_window(profile_key, forecast_window="next_week", **kw)
        out["window_label"] = "this week (to week-end close)"
        out["scored_cadence"] = "weekly"
        return out
    if cadence == "monthly":
        out = get_window(profile_key, forecast_window="next_week", **kw)
        start = datetime.strptime(out["window_start_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
        out["window_end_utc"] = _fmt(_month_end_close(profile_key, start, holiday_dates))
        out["window_label"] = "this month (to month-end close)"
        out["scored_cadence"] = "monthly"
        return out
    # daily (and any unknown cadence): a single-session window. 24/5 venues re-target to the next
    # DAILY liquidity close so each day owns a distinct, non-overlapping window; equity/crypto
    # already get a ~1-day window from get_session.
    fw = "next_liquid_session" if ptype in ("fx_24_5", "futures_23h") else None
    out = get_window(profile_key, forecast_window=fw, **kw)
    # Crypto is 24/7 with no session close, so get_session hands back a rolling now+24h window — which
    # closes ~24h after generation (AFTER the next morning run), so the prediction can never be scored
    # the next day (and the per-ticker file is overwritten first). Re-target the DAILY crypto window to
    # the next fixed 21:00 UTC close (aligned with the FX/commodity daily close) so every daily asset
    # owns a distinct window that closes BEFORE the next run and grades cleanly the following morning.
    if ptype == "crypto_24_7" and cadence == "daily":
        _now = now or datetime.now(UTC)
        _close = _now.replace(hour=21, minute=0, second=0, microsecond=0)
        if _close <= _now + timedelta(minutes=min_remaining_min):
            _close = _close + timedelta(days=1)
        out["window_start_utc"] = _fmt(_now)
        out["window_end_utc"] = _fmt(_close)
        out["window_label"] = "to next 21:00 UTC daily close"
    out["scored_cadence"] = "daily"
    return out


if __name__ == "__main__":
    import json, sys
    key = sys.argv[1] if len(sys.argv) > 1 else "cme_futures"
    fw = sys.argv[2] if len(sys.argv) > 2 else None
    print(json.dumps(get_window(key, forecast_window=fw), indent=1))
