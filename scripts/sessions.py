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

PROFILES = {
    # CME Globex metals/energy/equity-index: Sun 22:00 UTC open, Fri 21:00 UTC
    # weekly close, daily maintenance 21:00-22:00 UTC Mon-Thu.
    "cme_futures": {
        "label": "CME Globex futures (23h sessions, daily 21:00-22:00 UTC maintenance)",
        "type": "futures_23h",
        "weekly_close": ("FRI", "21:00"),
        "weekly_open": ("SUN", "22:00"),
        "daily_break": ("21:00", "22:00"),
        "prose": [
            "CME Globex: ~23h/day Sun 22:00 UTC -> Fri 21:00 UTC, daily maintenance break 21:00-22:00 UTC (Mon-Thu).",
            "Weekly close Friday 21:00 UTC; weekend headlines land while the market is shut - gap risk realises at the Sunday 22:00 UTC reopen.",
            "Front-month continuous series used; contract month labelled in metadata. Roll risk flagged when within ~1 week of expiry.",
        ],
    },
    # Spot FX: ~24/5, Sun ~21:05 UTC open to Fri ~21:00 UTC close (June DST).
    "fx_spot": {
        "label": "Spot FX 24/5 (Sun ~21:05 UTC -> Fri ~21:00 UTC)",
        "type": "fx_24_5",
        "weekly_close": ("FRI", "21:00"),
        "weekly_open": ("SUN", "21:05"),
        "daily_break": None,
        "prose": [
            "Spot FX trades ~24/5: Sun ~21:05 UTC -> Fri ~21:00 UTC. Sessions: Asia ~00:00-08:00, London ~07:00-16:00, New York ~12:00-21:00 UTC.",
            "Rollover/value-date window ~21:00-22:15 UTC is illiquid - no fresh entries there unless explicitly labelled.",
            "Weekend gaps are routine around geopolitical headlines; windows end at the weekly close unless modelling the gap.",
        ],
    },
    # US single stock / ETF (Nasdaq/NYSE): pre-market 08:00-13:30 UTC, regular
    # 13:30-20:00 UTC, after-hours 20:00-00:00 UTC (EDT regime, June). The
    # prediction window targets the NEXT REGULAR session only.
    "us_equity_rth": {
        "label": "US equity (Nasdaq) - pre-market 08:00-13:30, regular 13:30-20:00, after-hours 20:00-00:00 UTC (EDT regime)",
        "type": "equity_rth",
        "rth": ("13:30", "20:00"),
        "prose": [
            "Nasdaq regular session 13:30-20:00 UTC (14:30-21:00 UK in June); pre-market 08:00-13:30 UTC and after-hours 20:00-00:00 UTC trade thin.",
            "Tradable levels in this report are REGULAR-SESSION levels (unadjusted prices); extended-hours prints are labelled separately and must not be mixed with them.",
            "The prediction window is the next REGULAR session only; pre-market gaps realise at the 14:30 UK open. Weekends/holidays skipped via the exchange calendar.",
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


def _next_weekday_time(now, wd_name, hhmm, forward=True):
    wd, (h, m) = _WD[wd_name], (int(hhmm[:2]), int(hhmm[3:5]))
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (wd - now.weekday()) % 7
    cand = cand + timedelta(days=delta)
    if forward and cand <= now:
        cand += timedelta(days=7)
    return cand


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
        (oh, om), (ch, cm) = ((int(p["rth"][0][:2]), int(p["rth"][0][3:5])),
                              (int(p["rth"][1][:2]), int(p["rth"][1][3:5])))
        rth_open = now.replace(hour=oh, minute=om, second=0, microsecond=0)
        rth_close = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
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
            start = nxt.replace(hour=oh, minute=om, second=0, microsecond=0)
            end = nxt.replace(hour=ch, minute=cm, second=0, microsecond=0)
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

    wc = _next_weekday_time(now, *p["weekly_close"])
    wo = _next_weekday_time(now, *p["weekly_open"])
    # weekend = between the most recent Friday close and the Sunday open after it
    prev_close = wc - timedelta(days=7) if wc > now else wc
    sun_open_after_prev = _next_weekday_time(prev_close + timedelta(minutes=1),
                                             *p["weekly_open"])
    in_weekend = prev_close <= now < sun_open_after_prev
    remaining = (wc - now).total_seconds() / 60 if not in_weekend else 0
    is_friday = now.weekday() == 4

    def _skip_holidays(d):
        while d.date() in holiday_dates:
            d += timedelta(days=1)
        return d

    if in_weekend or remaining < min_remaining_min or (is_friday and remaining < friday_cutoff_min):
        # NEXT full session: Sunday open -> Monday close (futures: 21:00 UTC
        # Monday; FX: Monday 21:00 UTC NY close)
        start = wo if wo > now else _next_weekday_time(now, *p["weekly_open"])
        start = _skip_holidays(start)
        end = start.replace(hour=21, minute=0) + timedelta(days=1)
        out["market_state"] = "closed_weekend" if in_weekend else "open_closing_soon"
        out["window_label"] = "next session (Sun reopen -> Mon close)"
    else:
        start, end = now, wc
        out["market_state"] = "open"
        out["window_label"] = "remainder of current session"

    nb = "none before window end"
    if p.get("daily_break") and end.weekday() in (0, 1, 2, 3):
        b0 = end.replace(hour=int(p["daily_break"][0][:2]), minute=int(p["daily_break"][0][3:5]))
        if start < b0 < end:
            nb = f"{_fmt(b0)} -> {p['daily_break'][1]} UTC daily maintenance"
        else:
            nb = f"next daily break {_fmt(b0)} UTC (at/after window end)"
    out.update({
        "market_open_utc": _fmt(wo - timedelta(days=7)) if not in_weekend and wo - timedelta(days=7) <= now else _fmt(wo),
        "market_close_utc": _fmt(wc),
        "next_maintenance_break": nb,
        "window_start_utc": _fmt(start),
        "window_end_utc": _fmt(end),
    })
    return out


if __name__ == "__main__":
    import json, sys
    key = sys.argv[1] if len(sys.argv) > 1 else "cme_futures"
    print(json.dumps(get_session(key), indent=1))
