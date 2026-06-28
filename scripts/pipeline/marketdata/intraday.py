"""Intraday data fetch + analysis for the advisor — stdlib only.

Usage:
  python -m scripts.pipeline.marketdata.intraday SYMBOL [--name NAME] [--datadir data]
         [--hrange 10d] [--drange 1y] [--roll-utc 22] [--related "SYM1,SYM2,SYM3"]
         [--provider yahoo|eodhd|twelvedata] [--anchor live|prior-completed|friday]
         [--as-of "YYYY-MM-DD HH:MM"] [--session-profile fx_spot|us_equity_rth|...]
         [--td-symbol XAU/USD] [--fundamentals 1] [--fundamentals-source auto|twelvedata|none]

--as-of TRIMS every fetched series (hourly/daily/related) to bars at/<= that UTC
moment BEFORE any indicator/pivot/session/freshness computation or CSV write, so the
whole analysis reproduces the information state at that past instant (retroactive /
backtest generation). Freshness is measured as of the cutoff (not wall-clock), and
fetched_utc + an "as_of" marker record it. Combine with the natural live anchoring to
get a pre-session read: pivots from the last completed session before the cutoff,
bands on the in-progress session's open, last_price = the last bar at/<= the cutoff.

--anchor re-derives floor pivots + ATR day-bands on a CHOSEN COMPLETED daily session
instead of the live/in-progress one (the pre-market case), replacing the old hand-built
*_anchored.json step:
  live (default)   current behavior: pivots from the prior completed session, bands
                   anchored on TODAY'S session open.
  prior-completed  pivots from the last COMPLETED daily session's HLC, bands anchored
                   on that session's CLOSE (even if a live session is forming).
  friday           like prior-completed but the most recent completed Friday session
                   (weekend / Monday pre-market). Falls back to last completed if none.
When != live, pivots_classic / atr_day_bands are OVERWRITTEN with the anchored values
(so scaffold_payload.py uses them transparently), the live values are kept under
pivots_classic_live / atr_day_bands_live, and an "anchor" block documents the choice.

NAME defaults to the symbol stripped of '=' and '^' (GC=F -> GCF). Pass --name
explicitly for the canonical instrument prefixes (XAUUSD, GBPJPY, BTC, ES, ...).

Symbols are always given in YAHOO format: BP.L, TSCO.L, ^FTSE, GBPUSD=X, BZ=F, GC=F,
BTC-USD, AAPL ... The provider layer maps them per provider.

Data providers:
  yahoo (default)  Yahoo chart API, no key. Unofficial: fine for personal/dev use,
                   NOT licensed for a commercial product.
  twelvedata       Set ADVISOR_DATA_PROVIDER=twelvedata + TWELVEDATA_API_KEY=... (or pass
                   --provider twelvedata). Licensed self-serve feed covering US equities/
                   ETFs, forex (incl. XAU/USD spot gold), and crypto. Futures (=F) and
                   indices (^) are NOT requested from it and come from Yahoo; any failed/
                   empty fetch also falls back to Yahoo per-fetch. Basic plan: 8 req/min,
                   800 credits/day (1 credit per series).
  eodhd            Set ADVISOR_DATA_PROVIDER=eodhd + EODHD_API_KEY=... (or pass
                   --provider eodhd). Licensed feed; LSE 15-min delayed. Futures (=F)
                   are not covered by EODHD and always come from Yahoo; any failed
                   EODHD fetch also falls back to Yahoo per-fetch — see the JSON's
                   "provider" block for what actually served each series.

Writes:
  <datadir>/candles/<NAME>_hourly.csv      datetime_utc,open,high,low,close,volume
  <datadir>/candles/<NAME>_daily.csv       date,open,high,low,close,volume
  <datadir>/analysis/<NAME>_analysis.json  indicators, pivots, ATR day-range bands,
                          trend reads, freshness (staleness + market state),
                          degraded flag, provider, files (the csv paths above).

Indicator warm-up: the CSVs contain the requested display window PLUS extra lookback
history (hourly: +21 calendar days; daily: one standard range up, e.g. 1y -> 2y) so
chart SMAs/RSI are fully warmed BEFORE the display window starts; report_pdf.py
computes indicators on the full series and crops each chart back to its
"display_days". The JSON's "windows" block records display vs fetched ranges and
per-SMA warm-up sufficiency at the display-window start. score_report.py is
unaffected (it filters bars to the prediction window).

Degraded mode: with fewer than 24 hourly bars but usable daily data, the run still
succeeds with "degraded": "daily_only" — hourly block null, prior/today sessions and
pivots rebuilt from the last two DAILY bars (tagged "basis": "daily_bars_fallback"),
ATR bands anchored on the last daily close (tagged "anchor": "prior_close_fallback").
If daily fails too: clear error on stderr, exit 2.

Methodology (industry standard):
  - Daily = regime/context: ATR(14) Wilder, SMA20/50/100/200, realized vol, swing levels.
  - Hourly = today's trend: SMA20/50, EMA9/21, RSI(14), MACD(12,26,9), swing structure,
    session VWAP when volume exists.
  - Day-range projection: classic floor pivots from PRIOR session OHLC (PP, R1-R3, S1-S3)
    plus ATR bands anchored on TODAY'S session open (open +/- 0.5*ATRd inner, +/- 1.0*ATRd outer).
"""
import concurrent.futures
import csv, json, math, os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
# The provider/fetch layer lives in data_providers.py now; re-exported so intraday.<fn> and
# market_context (intraday.fetch_chart) are unchanged. Explicit (not *) so privates carry over.
from data_providers import (  # noqa: F401
    UA, PROVIDER_DEFAULT, DATA_LICENSE, PROVIDER_REGISTRY,
    provider_is_commercial, series_license, license_fields,
    _http_json, _YAHOO_HOSTS, yahoo_chart, range_to_timedelta,
    EODHD_EXCH_MAP, CRYPTO_QUOTES, map_symbol_eodhd, eodhd_chart,
    map_symbol_coingecko, coingecko_chart,
    map_symbol_twelvedata, twelvedata_chart, twelvedata_fundamentals,
    _TD_LOCK, _TD_STATE, _td_interval, _td_throttle, _td_get,
    fetch_chart,
)


def freshness_block(meta, rows, now=None, granularity="hourly", session_profile=None):
    """Staleness + market-state read on the freshest bar of `rows`.
    Only unambiguous problems set stale=true; marginal calls are left to the skill
    via market_state + age_minutes:
      crypto (24/7): stale > 3h.
      FX/futures (24/5): weekend = Fri 22:00 -> Sun 22:00 UTC (lenient across DST,
        never false-stale); inside it, stale only if the last bar predates the
        Friday 22:00 close by > 3h.
      equities/ETFs/indices: Yahoo's currentTradingPeriod (exchange-aware, included
        in the chart response) -> in-session: stale > 90 min; out of session:
        stale > 96h (dead feed / delisting) so weekends and bank-holiday Mondays
        never false-flag. Missing meta (e.g. eodhd): unknown + the 96h rule.
    granularity="daily": daily bars are stamped at session OPEN, so intra-session
    age measures time-since-open, not feed lag — staleness becomes the 96h
    dead-feed rule only; market_state stays informative.
    """
    meta = meta or {}
    now = now or datetime.now(timezone.utc)
    last = datetime.fromtimestamp(rows[-1]["ts"], tz=timezone.utc)
    age_min = int((now - last).total_seconds() // 60)
    itype = (meta.get("instrumentType") or "UNKNOWN").upper()
    state, stale = "unknown", age_min > 96 * 60
    if itype == "CRYPTOCURRENCY":
        state, stale = "open", age_min > 180
    elif itype in ("CURRENCY", "FUTURE", "FUTURES"):
        wd = now.weekday()  # Mon=0 .. Sun=6
        if (wd == 4 and now.hour >= 22) or wd == 5 or (wd == 6 and now.hour < 22):
            state = "closed_weekend"
            fri_close = (now - timedelta(days=(wd - 4) % 7)).replace(
                hour=22, minute=0, second=0, microsecond=0)
            stale = last < fri_close - timedelta(minutes=180)
        else:
            state, stale = "open", age_min > 180
    elif itype in ("EQUITY", "ETF", "INDEX", "MUTUALFUND"):
        reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
        start, end = reg.get("start"), reg.get("end")
        if start and end:
            if start <= now.timestamp() <= end + 1800:
                state, stale = "open", age_min > 90
            else:
                state, stale = "closed_offhours", age_min > 96 * 60
        elif session_profile:
            # Provider feeds (eodhd / twelvedata) don't return exchange session bounds, so without
            # this an in-session-but-stale equity would fall through to the lax 96h dead-feed rule
            # and publish at full confidence. Derive in-session state from the session profile and
            # apply the strict 90-min in-session staleness instead. (Holidays aren't passed here, so
            # a holiday is treated as in-session -> over-flags stale: conservative/safe, and rare
            # since the scheduler skips holidays anyway.)
            try:
                _d = os.path.dirname(os.path.abspath(__file__))   # robust import: scripts/ on path
                if _d not in sys.path:
                    sys.path.insert(0, _d)
                from sessions import get_session as _gs
                st = _gs(session_profile, now=now).get("market_state", "")
                if st in ("open_regular_session", "open_closing_soon"):
                    state, stale = "open", age_min > 90
                else:
                    state, stale = (st or "closed_offhours"), age_min > 96 * 60
            except Exception:
                pass
    if granularity == "daily":
        stale = age_min > 96 * 60
    return {"last_bar_utc": last.strftime("%Y-%m-%d %H:%M"), "age_minutes": age_min,
            "instrument_type": itype, "market_state": state, "stale": bool(stale),
            "stale_reason": (f"last bar {age_min} min old with market_state={state}"
                             if stale else None), "bar_granularity": granularity}


# Indicator + level math now lives in indicators.py; re-exported so intraday.<fn> and the
# internal analysis path are unchanged (market_context uses these via intraday.X).
from indicators import (  # noqa: E402
    sma, ema_series, rsi14, atr14, macd, classify_long_term, classify_intraday_trend,
    alignment_verdict, level_stats, swings, compute_pivots_bands)


# --- selectable analysis intervals (chart_intervals) -----------------------
# An asset may analyse more than the canonical 60m + 1d pair. Sub-daily extras
# (2h/4h/8h) are RESAMPLED from the 60m series and weekly/monthly from the daily
# series, so NO extra provider calls are made (rate-limit safe) and the derived
# bars are always consistent with the canonical pair the rest of the pipeline reads.
SUPPORTED_INTERVALS = ("60m", "2h", "4h", "8h", "1d", "1week", "1month")
CANONICAL_INTERVALS = ("60m", "1d")
_INTRADAY_HOURS = {"2h": 2, "4h": 4, "8h": 8}


def parse_chart_intervals(raw):
    """Validate a comma list against SUPPORTED_INTERVALS. Skip+warn unknowns, NEVER raise.
    Always returns a deduped list that includes the canonical 60m + 1d pair (so the rest of
    the pipeline — sessions, pivots, freshness — always has its inputs)."""
    requested = [i.strip().lower() for i in (raw or "").split(",") if i.strip()]
    valid, dropped = [], []
    for iv in requested:
        (valid if iv in SUPPORTED_INTERVALS else dropped).append(iv)
    for iv in dropped:
        print(f"WARNING: skipping unsupported chart interval {iv!r} "
              f"(allowed: {', '.join(SUPPORTED_INTERVALS)})", file=sys.stderr)
    out = []
    for iv in list(CANONICAL_INTERVALS) + valid:   # canonical first, then extras
        if iv not in out:
            out.append(iv)
    return out


def _resample_hours(rows, hours):
    """Aggregate 60m bars into fixed N-hour UTC-aligned buckets. rows ascending UTC."""
    if not rows or hours <= 1:
        return list(rows)
    span = hours * 3600
    buckets, order = {}, []
    for r in rows:
        b = (int(r["ts"]) // span) * span
        s = buckets.get(b)
        if s is None:
            buckets[b] = {"ts": b, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
            order.append(b)
        else:
            s["h"] = max(s["h"], r["h"]); s["l"] = min(s["l"], r["l"])
            s["c"] = r["c"]; s["v"] += r["v"]
    return [buckets[b] for b in order]


def _resample_calendar(rows, period):
    """Aggregate daily bars into weekly (ISO week) or monthly buckets. rows ascending UTC."""
    if not rows:
        return []
    buckets, order = {}, []
    for r in rows:
        d = datetime.fromtimestamp(r["ts"], tz=timezone.utc).date()
        key = d.isocalendar()[:2] if period == "1week" else (d.year, d.month)
        s = buckets.get(key)
        if s is None:
            buckets[key] = {"ts": int(r["ts"]), "o": r["o"], "h": r["h"], "l": r["l"],
                            "c": r["c"], "v": r["v"]}
            order.append(key)
        else:
            s["h"] = max(s["h"], r["h"]); s["l"] = min(s["l"], r["l"])
            s["c"] = r["c"]; s["v"] += r["v"]
    return [buckets[k] for k in order]


def build_interval_series(interval, hourly, daily):
    """OHLCV rows for a requested interval, derived from the canonical pair (no extra fetch)."""
    if interval == "60m":
        return list(hourly)
    if interval == "1d":
        return list(daily)
    if interval in _INTRADAY_HOURS:
        return _resample_hours(hourly, _INTRADAY_HOURS[interval])
    if interval in ("1week", "1month"):
        return _resample_calendar(daily, interval)
    return []


def _write_candles(path, rows, intraday=True):
    fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime(fmt),
                        f'{r["o"]:.6f}', f'{r["h"]:.6f}', f'{r["l"]:.6f}', f'{r["c"]:.6f}', r["v"]])


def interval_block(interval, rows):
    """Compact, additive indicator block for one analysis interval (metadata only)."""
    closes = [r["c"] for r in rows]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    trend = (classify_long_term(closes, s50, sma(closes, 200))
             if len(closes) >= 50 else "Insufficient data")
    return {"bars": len(rows), "last_close": round(closes[-1], 6) if closes else None,
            "sma20": s20, "sma50": s50, "rsi14": rsi14(closes), "atr14": atr14(rows),
            "trend": trend}


def main():
    symbol = sys.argv[1]
    args = dict(zip(sys.argv[2::2], sys.argv[3::2]))
    name = args.get("--name", symbol.replace("=", "").replace("^", ""))
    datadir = Path(args.get("--datadir", "data"))
    candles_dir, analysis_dir = datadir / "candles", datadir / "analysis"
    hrange, drange = args.get("--hrange", "10d"), args.get("--drange", "1y")
    roll = int(args.get("--roll-utc", "0"))
    provider = args.get("--provider")
    session_profile = args.get("--session-profile")  # enables in-session staleness for provider equities
    td_symbol = args.get("--td-symbol")  # explicit Twelve Data symbol override (e.g. XAU/USD for gold)
    rel_syms = [s.strip() for s in args.get("--related", "").split(",") if s.strip()]
    anchor_mode = args.get("--anchor", "live")
    if anchor_mode not in ("live", "prior-completed", "friday"):
        print(f"ERROR: --anchor must be one of live|prior-completed|friday (got {anchor_mode!r})",
              file=sys.stderr)
        sys.exit(2)
    as_of = args.get("--as-of")
    cutoff_ts = cutoff_dt = None
    if as_of:
        s = as_of.strip()
        try:
            cutoff_dt = datetime.strptime(s, "%Y-%m-%d %H:%M" if len(s) > 10 else "%Y-%m-%d") \
                .replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: --as-of must be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' UTC (got {as_of!r})",
                  file=sys.stderr)
            sys.exit(2)
        cutoff_ts = cutoff_dt.timestamp()

    # Warm-up extension: fetch extra lookback BEFORE the display window so the
    # largest chart indicators (hourly SMA50, daily SMA200) are fully warmed at the
    # display-window start. Charts crop back to the display window downstream.
    DFETCH = {"1mo": "6mo", "3mo": "1y", "6mo": "2y", "1y": "2y", "2y": "5y",
              "5y": "10y", "10y": "max", "max": "max"}
    try:
        hdisp_days = int(hrange.strip().lower().rstrip("d"))
    except ValueError:
        hdisp_days = 10
    hfetch = f"{hdisp_days + 21}d"  # >=50 warm-up hourly bars even at ~7 bars/day
    dfetch = DFETCH.get(drange.strip().lower(), "2y")

    # all network fetches run concurrently (network-bound; stdlib threads)
    errors = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        fut_h = pool.submit(fetch_chart, symbol, "60m", hfetch, provider, None, td_symbol)
        fut_d = pool.submit(fetch_chart, symbol, "1d", dfetch, provider, None, td_symbol)
        fut_rel = [(rs, pool.submit(fetch_chart, rs, "1d", "10d", provider)) for rs in rel_syms]
        try:
            meta_h, hourly = fut_h.result()
        except Exception as ex:
            meta_h, hourly = None, []
            errors["hourly"] = str(ex)[:120]
        try:
            meta_d, daily = fut_d.result()
        except Exception as ex:
            meta_d, daily = None, []
            errors["daily"] = str(ex)[:120]
        related = []
        for rs, fut in fut_rel:
            try:
                _, rrows = fut.result()
                if cutoff_ts is not None:
                    rrows = [r for r in rrows if r["ts"] <= cutoff_ts]
                rc = [r["c"] for r in rrows]
                related.append({"symbol": rs, "last": round(rc[-1], 6),
                                "chg_1d_pct": round(100 * (rc[-1] / rc[-2] - 1), 2) if len(rc) > 1 else None,
                                "chg_5d_pct": round(100 * (rc[-1] / rc[-6] - 1), 2) if len(rc) > 5 else None})
            except Exception as ex:
                related.append({"symbol": rs, "error": str(ex)[:80]})

    if cutoff_ts is not None:
        hourly = [r for r in hourly if r["ts"] <= cutoff_ts]
        daily = [r for r in daily if r["ts"] <= cutoff_ts]

    if not daily:
        print(f"ERROR: no usable data for {symbol} "
              f"(hourly: {errors.get('hourly', 'ok')}; daily: {errors.get('daily', 'no bars')}). "
              f"Check the symbol format (BP.L, ^FTSE, GBPUSD=X, GC=F, BTC-USD) and network.",
              file=sys.stderr)
        sys.exit(2)

    degraded = None
    if len(hourly) < 24:
        degraded = "daily_only"
        errors.setdefault("hourly", f"only {len(hourly)} hourly bars (<24 minimum)")
        print(f"WARNING: degraded daily_only for {symbol} - {errors['hourly']}", file=sys.stderr)

    candles_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    with open(candles_dir / f"{name}_hourly.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in hourly:
            w.writerow([datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                        f'{r["o"]:.6f}', f'{r["h"]:.6f}', f'{r["l"]:.6f}', f'{r["c"]:.6f}', r["v"]])
    with open(candles_dir / f"{name}_daily.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in daily:
            w.writerow([datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%Y-%m-%d"),
                        f'{r["o"]:.6f}', f'{r["h"]:.6f}', f'{r["l"]:.6f}', f'{r["c"]:.6f}', r["v"]])

    # --- additional analysis intervals (chart_intervals): derived from the canonical pair,
    # written as data/candles/{name}_{interval}.csv with an additive analysis block each.
    chart_intervals = parse_chart_intervals(args.get("--chart-intervals", "60m,1d"))
    intervals_meta = {}
    for iv in chart_intervals:
        if iv == "60m":
            ipath, irows, intra = candles_dir / f"{name}_hourly.csv", hourly, True
        elif iv == "1d":
            ipath, irows, intra = candles_dir / f"{name}_daily.csv", daily, False
        else:
            irows = build_interval_series(iv, hourly, daily)
            intra = iv in _INTRADAY_HOURS
            ipath = candles_dir / f"{name}_{iv}.csv"
            if irows:
                _write_candles(ipath, irows, intraday=intra)
        blk = interval_block(iv, irows)
        if irows:                              # only advertise a CSV path that was actually written
            blk["csv"] = ipath.as_posix()
        intervals_meta[iv] = blk

    dc = [r["c"] for r in daily]
    hc = [r["c"] for r in hourly]
    atr_d = atr14(daily)
    atr_h = atr14(hourly)

    # Prior completed session + today's session. Normal path: rebuilt from HOURLY bars so
    # boundaries are correct per asset class (--roll-utc N rolls the session day at N:00 UTC;
    # FX/crypto: 22, matching the New York close). Degraded path: last two DAILY bars.
    prior = today = None
    basis = None
    anchor = anchor_tag = None
    if not degraded:
        sessions = {}
        for r in hourly:
            day = datetime.fromtimestamp(r["ts"] - roll * 3600, tz=timezone.utc).date()
            s = sessions.setdefault(day, {"o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "ts": r["ts"]})
            s["h"] = max(s["h"], r["h"]); s["l"] = min(s["l"], r["l"]); s["c"] = r["c"]
        skeys = sorted(sessions)
        if len(skeys) >= 2:
            prior, today = sessions[skeys[-2]], sessions[skeys[-1]]
            prior["date_label"], today["date_label"] = str(skeys[-2]), str(skeys[-1])
            anchor = today["o"]
        else:
            errors.setdefault("hourly_sessions",
                              "fewer than 2 hourly-built sessions; pivots from daily bars")
    if prior is None and len(daily) >= 2:
        pb, tb = daily[-2], daily[-1]
        prior = {"o": pb["o"], "h": pb["h"], "l": pb["l"], "c": pb["c"],
                 "date_label": datetime.fromtimestamp(pb["ts"], tz=timezone.utc).strftime("%Y-%m-%d")}
        today = {"o": tb["o"], "h": tb["h"], "l": tb["l"], "c": tb["c"],
                 "date_label": datetime.fromtimestamp(tb["ts"], tz=timezone.utc).strftime("%Y-%m-%d")}
        basis = "daily_bars_fallback"
        anchor, anchor_tag = tb["c"], "prior_close_fallback"

    pivots, bands = compute_pivots_bands(prior, anchor, atr_d)

    # hourly trend read (normal path only)
    if not degraded:
        sh, sl = swings(hourly)
        e9, e21 = ema_series(hc, 9), ema_series(hc, 21)
        # session VWAP from today's session start (UTC date of last bar)
        last_day = datetime.fromtimestamp(hourly[-1]["ts"], tz=timezone.utc).date()
        sess = [r for r in hourly if datetime.fromtimestamp(r["ts"], tz=timezone.utc).date() == last_day]
        vol_sum = sum(r["v"] for r in sess)
        vwap = (sum(((r["h"] + r["l"] + r["c"]) / 3) * r["v"] for r in sess) / vol_sum) if vol_sum > 0 else None
        it_trend = classify_intraday_trend(hc, sma(hc, 20), sma(hc, 50),
                                           e9[-1] if e9 else None, e21[-1] if e21 else None)
        hourly_block = {
            "bars": len(hourly), "sma20": sma(hc, 20), "sma50": sma(hc, 50),
            "ema9": e9[-1] if e9 else None, "ema21": e21[-1] if e21 else None,
            "ema_cross": ("bullish" if e9 and e21 and e9[-1] > e21[-1] else "bearish") if e9 and e21 else None,
            "rsi14": rsi14(hc), "macd": macd(hc), "atr14": atr_h,
            "swing_highs": sh, "swing_lows": sl,
            "vwap_session": vwap,
            "above_sma20": hc[-1] > sma(hc, 20) if sma(hc, 20) else None,
        }
    else:
        it_trend = "Insufficient data"
        hourly_block = None

    rvol = None
    if len(dc) > 2:
        rets = [math.log(dc[i] / dc[i - 1]) for i in range(max(1, len(dc) - 20), len(dc))]
        if len(rets) > 1:
            mu = sum(rets) / len(rets)
            rvol = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(252) * 100

    d_s20, d_s50, d_s100, d_s200 = sma(dc, 20), sma(dc, 50), sma(dc, 100), sma(dc, 200)
    lt_trend = classify_long_term(dc, d_s50, d_s200)
    alignment = "unknown (no hourly data)" if degraded else alignment_verdict(lt_trend, it_trend)
    # Stats over the daily series (~1y) — Yahoo's own bar convention is self-consistent
    # for prev->cur pivot/containment relationships even where its FX day boundary differs
    # from the 22:00 UTC roll used for today's live pivots.
    stats = level_stats(daily, atr_d)

    # freshness from hourly bars whenever any exist (timestamps reflect feed lag
    # honestly even when too thin to analyze); daily bars only as a last resort
    if hourly:
        fresh = freshness_block(meta_h, hourly, now=cutoff_dt, granularity="hourly",
                                session_profile=session_profile)
    else:
        fresh = freshness_block(meta_d, daily, now=cutoff_dt, granularity="daily",
                                session_profile=session_profile)
    meta_best = (meta_d if degraded else meta_h) or {}
    notes = []
    for m in (meta_h, meta_d):
        if m and m.get("provider_note") and m["provider_note"] not in notes:
            notes.append(m["provider_note"])

    def session_out(s):
        if not s:
            return None
        d = {"date": s["date_label"], "o": s["o"], "h": s["h"], "l": s["l"], "c": s["c"],
             "session_roll_utc": roll}
        if basis:
            d["basis"] = basis
        return d

    pivots_out = {k: round(v, 6) for k, v in pivots.items()} if pivots else None
    if pivots_out and basis:
        pivots_out["basis"] = basis
    bands_out = {k: round(v, 6) for k, v in bands.items()} if bands else None
    if bands_out and anchor_tag:
        bands_out["anchor"] = anchor_tag

    # --- anchored override (--anchor prior-completed|friday) ------------------
    # Re-derive pivots + ATR day-bands on a CHOSEN COMPLETED daily session instead
    # of the live/in-progress one (the pre-market case). We OVERWRITE pivots_classic
    # and atr_day_bands with the anchored values so downstream scaffold_payload.py
    # transparently consumes them, keep the live values under *_live, and document
    # the choice in an "anchor" block. Uses the daily series (the spec's
    # "prior completed daily session HLC" + that session's close as band anchor).
    anchor_meta = {"mode": anchor_mode}
    pivots_live_out = bands_live_out = None
    if anchor_mode != "live":
        # session day of the live/forming daily bar under the roll convention
        def _sess_date(ts):
            return datetime.fromtimestamp(ts - roll * 3600, tz=timezone.utc).date()
        today_sess = _sess_date(datetime.now(timezone.utc).timestamp())
        # candidates = daily bars whose session day is already completed (< today's)
        completed = [(i, r) for i, r in enumerate(daily) if _sess_date(r["ts"]) < today_sess]
        if not completed:  # clock/data edge: treat all-but-last as completed
            completed = list(enumerate(daily))[:-1]
        chosen = None
        if anchor_mode == "prior-completed":
            chosen = completed[-1][1] if completed else None
        elif anchor_mode == "friday":
            fri = [(i, r) for i, r in completed if _sess_date(r["ts"]).weekday() == 4]
            chosen = fri[-1][1] if fri else (completed[-1][1] if completed else None)
            if not fri and completed:
                anchor_meta["note"] = "no completed Friday session found; fell back to last completed session"
        if chosen is None or atr_d is None:
            # nothing safe to anchor on (e.g. <1 completed daily bar, or no ATR):
            # leave the live pivots/bands untouched, record why.
            anchor_meta["applied"] = False
            anchor_meta["reason"] = ("no completed daily session available" if chosen is None
                                     else "daily ATR(14) unavailable")
        else:
            a_close = chosen["c"]
            a_piv, a_bands = compute_pivots_bands(chosen, a_close, atr_d)
            a_date = _sess_date(chosen["ts"]).isoformat()
            pivots_live_out, bands_live_out = pivots_out, bands_out  # preserve live
            pivots_out = {k: round(v, 6) for k, v in a_piv.items()} if a_piv else None
            bands_out = {k: round(v, 6) for k, v in a_bands.items()} if a_bands else None
            if bands_out:
                bands_out["anchor"] = f"{anchor_mode}_session_close"
            anchor_meta.update({"applied": True, "session_date": a_date,
                                "anchor_close": round(a_close, 6)})

    # per-SMA warm-up sufficiency at the DISPLAY-window start (charts crop to display):
    # warm = at least n bars exist BEFORE the display cutoff, so the SMA(n)/RSI line
    # is valid from the first visible bar. Never infer trend from a cold SMA.
    def warm(rows_all, disp_seconds, n):
        if not rows_all:
            return False
        cutoff = rows_all[-1]["ts"] - disp_seconds
        return sum(1 for r in rows_all if r["ts"] < cutoff) >= n

    h_secs = hdisp_days * 86400
    d_secs = int(range_to_timedelta(drange).total_seconds())
    windows = {
        "hourly_display": hrange, "hourly_fetched": hfetch,
        "daily_display": drange, "daily_fetched": dfetch,
        "sma_warm_at_display_start": {
            "h20": warm(hourly, h_secs, 20), "h50": warm(hourly, h_secs, 50),
            "rsi14_hourly": warm(hourly, h_secs, 15),
            "d50": warm(daily, d_secs, 50), "d200": warm(daily, d_secs, 200),
            "rsi14_daily": warm(daily, d_secs, 15),
        },
    }

    last_px = meta_best.get("regularMarketPrice")
    if cutoff_ts is not None or last_px is None:  # as-of (never the live quote), or no provider quote
        last_px = round((hourly[-1]["c"] if hourly else daily[-1]["c"]), 6)  # -> the last bar's close

    # Optional equity fundamentals (Twelve Data). Best-effort + NARRATIVE-ONLY (never scored).
    # Skipped for backdated (--as-of) runs: a current snapshot would be anachronistic / look-ahead.
    fundamentals = None
    # Per-asset source override (synced from Neon engine_assets.fundamentals_source):
    #   none       -> never fetch, even when --fundamentals is on;
    #   twelvedata -> fetch via TD regardless of the global ADVISOR_DATA_PROVIDER;
    #   auto/unset -> fetch only when the global provider is already twelvedata.
    _fsrc = (args.get("--fundamentals-source") or "auto").strip().lower()
    _td_active = (provider or PROVIDER_DEFAULT) == "twelvedata"
    if (args.get("--fundamentals") in ("1", "true", "on", "yes") and _fsrc != "none"
            and (_fsrc == "twelvedata" or _td_active) and cutoff_ts is None):
        fkey = os.environ.get("TWELVEDATA_API_KEY")
        if fkey:
            try:
                fundamentals = twelvedata_fundamentals(symbol, fkey, td_symbol=td_symbol)
            except Exception as ex:
                errors["fundamentals"] = str(ex)[:100]

    out = {
        "symbol": symbol, "timezone": meta_best.get("exchangeTimezoneName"),
        "fetched_utc": (cutoff_dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M"),
        "last_price": last_px,
        "last_bar_utc": fresh["last_bar_utc"],
        "degraded": degraded,
        "errors": errors or None,
        "freshness": fresh,
        "windows": windows,
        "provider": {"hourly": meta_h.get("provider") if meta_h else None,
                     "daily": meta_d.get("provider") if meta_d else None,
                     "note": "; ".join(notes) if notes else None,
                     # license provenance: in commercial mode a non-commercial source -> degraded.
                     **license_fields(meta_h.get("provider") if meta_h else None,
                                      meta_d.get("provider") if meta_d else None)},
        "hourly": hourly_block,
        "trend": {
            "long_term_daily": lt_trend,
            "intraday_hourly": it_trend,
            "alignment": alignment,
            "golden_cross": (d_s50 > d_s200) if (d_s50 and d_s200) else None,
        },
        "stats_last_sessions": stats,
        "related": related,
        "daily": {
            "bars": len(daily), "sma20": d_s20, "sma50": d_s50, "sma100": d_s100, "sma200": d_s200,
            "rsi14": rsi14(dc), "atr14": atr_d,
            "realized_vol_20d_pct": round(rvol, 1) if rvol is not None else None,
            "prior_session": session_out(prior),
            "today_session": session_out(today),
        },
        "pivots_classic": pivots_out,
        "atr_day_bands": bands_out,
        "files": {"hourly_csv": (candles_dir / f"{name}_hourly.csv").as_posix(),
                  "daily_csv": (candles_dir / f"{name}_daily.csv").as_posix()},
        "chart_intervals": chart_intervals,
        "intervals": intervals_meta,
    }
    if cutoff_dt is not None:
        out["as_of"] = cutoff_dt.strftime("%Y-%m-%d %H:%M")
    if anchor_mode != "live":
        out["anchor"] = anchor_meta
        if pivots_live_out is not None:
            out["pivots_classic_live"] = pivots_live_out
        if bands_live_out is not None:
            out["atr_day_bands_live"] = bands_live_out
    if fundamentals is not None:
        out["fundamentals"] = fundamentals
    (analysis_dir / f"{name}_analysis.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
