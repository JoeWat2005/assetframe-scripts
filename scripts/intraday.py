"""Intraday data fetch + analysis for the advisor — stdlib only.

Usage:
  python scripts/intraday.py SYMBOL [--name NAME] [--datadir data]
         [--hrange 10d] [--drange 1y] [--roll-utc 22] [--related "SYM1,SYM2,SYM3"]
         [--provider yahoo|eodhd|twelvedata] [--anchor live|prior-completed|friday]
         [--as-of "YYYY-MM-DD HH:MM"] [--session-profile fx_spot|us_equity_rth|...]
         [--td-symbol XAU/USD] [--fundamentals 1]

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
import csv, json, math, os, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PROVIDER_DEFAULT = os.environ.get("ADVISOR_DATA_PROVIDER", "yahoo")


def _http_json(url, retries=2, backoff=1.0):
    """GET + parse JSON with a bounded retry. A single transient blip (timeout, 5xx, connection
    reset) used to silently degrade an asset to daily-only analysis; now we retry with backoff
    (1s, 2s). A 4xx (bad symbol / unauthorized) fails fast — retrying won't help."""
    import time as _t
    last = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            if ex.code and 400 <= ex.code < 500:
                raise   # client error (bad symbol / auth) — no point retrying
            last = ex
        except Exception as ex:
            last = ex
        if attempt < retries:
            _t.sleep(backoff * (2 ** attempt))
    raise last


_YAHOO_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")


def yahoo_chart(symbol, interval, rng):
    """Yahoo chart fetch with HOST failover (query1 -> query2). A single Yahoo host being
    rate-limited / briefly down is the most common feed failure; trying the mirror host first
    rescues it before the chain falls through to another provider. The response shape is
    validated BEFORE indexing so a malformed / error payload raises a clear ValueError (which
    the fetch chain handles) instead of a cryptic KeyError/IndexError. Once a host returns a
    well-formed result the rows are returned as-is (an empty-but-valid result is left for the
    caller's degrade path, exactly as before)."""
    last = None
    for host in _YAHOO_HOSTS:
        url = (f"https://{host}/v8/finance/chart/{urllib.parse.quote(symbol)}"
               f"?interval={interval}&range={rng}")
        try:
            data = _http_json(url)
        except Exception as ex:
            last = ex
            continue
        chart = (data or {}).get("chart") or {}
        if chart.get("error"):
            last = ValueError(f"yahoo error for {symbol}: {str(chart['error'])[:100]}")
            continue
        results = chart.get("result") or []
        if not results:
            last = ValueError(f"yahoo returned no result for {symbol} ({interval}/{rng})")
            continue
        res = results[0]
        quotes = (res.get("indicators") or {}).get("quote") or []
        if not quotes:
            last = ValueError(f"yahoo response missing quote indicators for {symbol}")
            continue
        q = quotes[0]
        rows = []
        for i, ts in enumerate(res.get("timestamp", [])):
            try:
                o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            except (KeyError, IndexError, TypeError):
                continue
            if None in (o, h, l, c):
                continue
            v = (q.get("volume") or [None] * (i + 1))[i] or 0
            rows.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": v})
        return res.get("meta") or {}, rows
    raise last or ValueError(f"yahoo: no data for {symbol} ({interval}/{rng})")


# --- EODHD adapter (symbols: {CODE}.{EXCHANGE}; only .L->.LSE verified from docs,
# the rest follow the documented pattern — verify with a live key before relying on them)
EODHD_EXCH_MAP = {".L": ".LSE", ".PA": ".PA", ".AS": ".AS", ".DE": ".XETRA", ".MI": ".MI",
                  ".MC": ".MC", ".TO": ".TO", ".HK": ".HK", ".T": ".TSE", ".SW": ".SW"}
CRYPTO_QUOTES = {"USD", "GBP", "EUR", "JPY", "USDT", "BTC", "ETH"}


def map_symbol_eodhd(sym):
    """Yahoo symbol -> (eodhd symbol | None, asset_class). None => not covered."""
    if sym.endswith("=F"):
        return None, "futures"
    if sym.endswith("=X"):
        base = sym[:-2]
        if len(base) == 3:  # Yahoo's 'JPY=X' means USD/JPY
            base = "USD" + base
        return base + ".FOREX", "forex"
    if sym.startswith("^"):
        return sym[1:] + ".INDX", "index"
    if "-" in sym and "." not in sym:
        base, _, quote = sym.rpartition("-")
        if base and quote in CRYPTO_QUOTES:  # BTC-USD yes; BRK-B falls through
            return sym + ".CC", "crypto"
    for suf, repl in EODHD_EXCH_MAP.items():
        if sym.endswith(suf):
            return sym[: -len(suf)] + repl, "equity"
    if "." not in sym:
        return sym + ".US", "equity"
    return sym, "equity"  # unknown suffix: pass through, fallback rescues if wrong


def range_to_timedelta(rng):
    rng = rng.strip().lower()
    if rng == "max":
        return timedelta(days=7300)
    if rng.endswith("mo"):
        return timedelta(days=int(rng[:-2]) * 31)
    n, unit = int(rng[:-1]), rng[-1]
    return timedelta(days=n * {"d": 1, "w": 7, "y": 366}[unit])


def eodhd_chart(symbol, interval, rng, api_key):
    esym, klass = map_symbol_eodhd(symbol)
    if esym is None:
        raise ValueError(f"eodhd does not cover {klass}")
    now = datetime.now(timezone.utc)
    start = now - range_to_timedelta(rng)
    rows = []
    if interval == "1d":
        url = (f"https://eodhd.com/api/eod/{urllib.parse.quote(esym)}"
               f"?api_token={api_key}&fmt=json&period=d&order=a"
               f"&from={start:%Y-%m-%d}&to={now:%Y-%m-%d}")
        raw = _http_json(url)
        if not isinstance(raw, list):
            raise ValueError(f"unexpected eodhd response: {str(raw)[:80]}")
        for r in raw:
            o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
            if None in (o, h, l, c):
                continue
            ts = int(datetime.strptime(r["date"], "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp())
            # 'close' not 'adjusted_close': matches Yahoo's unadjusted chart closes,
            # keeping SMA/ATR behavior identical across providers
            rows.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": r.get("volume") or 0})
    else:
        ivmap = {"60m": "1h", "1h": "1h", "5m": "5m", "1m": "1m"}
        url = (f"https://eodhd.com/api/intraday/{urllib.parse.quote(esym)}"
               f"?api_token={api_key}&fmt=json&interval={ivmap.get(interval, '1h')}"
               f"&from={int(start.timestamp())}&to={int(now.timestamp())}")
        raw = _http_json(url)
        if not isinstance(raw, list):
            raise ValueError(f"unexpected eodhd response: {str(raw)[:80]}")
        for r in raw:
            o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
            if None in (o, h, l, c):
                continue
            ts = r.get("timestamp")  # docs name the field ambiguously; accept both
            if ts is None and r.get("datetime"):
                ts = datetime.strptime(r["datetime"][:16], "%Y-%m-%d %H:%M") \
                    .replace(tzinfo=timezone.utc).timestamp()
            if ts is None:
                continue
            rows.append({"ts": int(float(ts)), "o": o, "h": h, "l": l, "c": c,
                         "v": r.get("volume") or 0})
    meta = {"exchangeTimezoneName": None,
            "regularMarketPrice": rows[-1]["c"] if rows else None,
            "instrumentType": {"forex": "CURRENCY", "crypto": "CRYPTOCURRENCY",
                               "index": "INDEX"}.get(klass, "EQUITY"),
            "currentTradingPeriod": None}
    return meta, rows


# --- CoinGecko adapter (keyless DAILY crypto fallback) ----------------------------
# CoinGecko serves free, keyless crypto OHLC. Crypto assets run 24/7/365 (every single day),
# so keeping them alive through a Yahoo outage matters most. Used ONLY as a last-resort
# fallback for the essential DAILY series of a crypto symbol. The public /ohlc endpoint auto-
# picks intraday granularity for short windows, so we resample whatever it returns into DAILY
# OHLC by UTC date (o=first, h=max, l=min, c=last). History is shorter than Yahoo's, so the
# warm-up guard / cold-indicator cap apply downstream — a safe degraded read, never wrong data.
_COINGECKO_IDS = {"BTC-USD": "bitcoin", "ETH-USD": "ethereum", "BTC": "bitcoin", "ETH": "ethereum",
                  "SOL-USD": "solana", "XRP-USD": "ripple", "ADA-USD": "cardano",
                  "DOGE-USD": "dogecoin", "BNB-USD": "binancecoin", "LTC-USD": "litecoin"}


def map_symbol_coingecko(sym):
    """Yahoo crypto symbol -> CoinGecko coin id, or None when it isn't a mapped crypto pair."""
    return _COINGECKO_IDS.get(sym)


def coingecko_chart(symbol, rng):
    """Keyless CoinGecko OHLC resampled to DAILY -> (meta, rows ascending UTC). Crypto only."""
    cid = map_symbol_coingecko(symbol)
    if cid is None:
        raise ValueError(f"coingecko: no id mapping for {symbol}")
    url = f"https://api.coingecko.com/api/v3/coins/{cid}/ohlc?vs_currency=usd&days=30"
    raw = _http_json(url)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"unexpected coingecko response for {cid}: {str(raw)[:80]}")
    days = {}
    for c in raw:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        ms, o, h, l, cl = c[0], c[1], c[2], c[3], c[4]
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        if d not in days:
            days[d] = {"ts": ts, "o": o, "h": h, "l": l, "c": cl}
        else:
            x = days[d]
            x["h"], x["l"], x["c"] = max(x["h"], h), min(x["l"], l), cl
    rows = [{"ts": v["ts"], "o": v["o"], "h": v["h"], "l": v["l"], "c": v["c"], "v": 0}
            for _, v in sorted(days.items())]
    meta = {"exchangeTimezoneName": "UTC", "regularMarketPrice": rows[-1]["c"] if rows else None,
            "instrumentType": "CRYPTOCURRENCY", "currentTradingPeriod": None}
    return meta, rows


# --- Twelve Data adapter (licensed self-serve feed: US equities/ETFs, forex incl. XAU/USD
# spot gold, crypto). Set ADVISOR_DATA_PROVIDER=twelvedata + TWELVEDATA_API_KEY. Futures (=F)
# and indices (^) are intentionally NOT requested from it (Yahoo serves them); any failed /
# empty fetch also falls back to Yahoo per-fetch. Symbols use a SLASH pair format (GBP/USD,
# XAU/USD, BTC/USD) mapped from the Yahoo symbol. Verified live 2026-06-22: values are STRINGS,
# ascending with order=ASC, datetime is 'YYYY-MM-DD' (daily) or 'YYYY-MM-DD HH:MM:SS' (intraday)
# in UTC with timezone=UTC, volume is absent for fx/crypto/metal, and errors come back as
# HTTP 200 with {"status":"error", ...} — so status is checked explicitly, not the HTTP code.
_TWELVEDATA_TYPE_MAP = {"Common Stock": "EQUITY", "ETF": "ETF", "Index": "INDEX",
                        "Digital Currency": "CRYPTOCURRENCY", "Physical Currency": "CURRENCY",
                        "Precious Metal": "CURRENCY"}

# Twelve Data rate throttle. The Basic (free) plan allows only 8 requests/minute. intraday fetches
# hourly+daily+related concurrently and run_daily fans out multiple assets, so without pacing a run
# bursts well past 8/min and most calls 429 -> fall back to Yahoo (clean, but then the licensed feed
# goes unused). This PROCESS-GLOBAL throttle spaces TD requests >= TWELVEDATA_MIN_INTERVAL_S apart
# (default 8s => <=7.5/min); set the env to 0 on a paid tier to disable. run_daily clamps its asset
# workers to 1 while this is active so the spacing holds across the whole run (one process at a time).
_TD_LOCK = threading.Lock()
_TD_STATE = {"last": 0.0}


def _td_interval():
    """Min seconds between Twelve Data calls. Prefer TWELVEDATA_RATE_PER_MIN (the plan's per-minute
    credit limit — 8 on Basic, 55 on Grow) -> 60/rate with a 5% margin; else TWELVEDATA_MIN_INTERVAL_S
    (explicit seconds, default 8). Either set to 0 to disable pacing on a tier with ample headroom."""
    rate = os.environ.get("TWELVEDATA_RATE_PER_MIN")
    if rate not in (None, ""):
        try:
            r = float(rate)
            return (60.0 / r) * 1.05 if r > 0 else 0.0
        except ValueError:
            pass
    try:
        return float(os.environ.get("TWELVEDATA_MIN_INTERVAL_S", "8") or 0)
    except ValueError:
        return 8.0


def _td_throttle():
    interval = _td_interval()
    if interval <= 0:
        return
    with _TD_LOCK:
        wait = interval - (time.monotonic() - _TD_STATE["last"])
        if wait > 0:
            time.sleep(wait)
        _TD_STATE["last"] = time.monotonic()


def map_symbol_twelvedata(sym):
    """Yahoo symbol -> (twelvedata symbol | None, asset_class). None => not covered, so the
    fetch chain serves it from Yahoo. Futures (=F), indices (^) and exchange-suffixed equities
    (BP.L, DX-Y.NYB) are intentionally left to Yahoo; FX/metal -> 'AAA/BBB', crypto 'BTC-USD' ->
    'BTC/USD', US equities pass through (AAPL)."""
    if "/" in sym:                         # already a slash pair = not a Yahoo symbol; let Yahoo handle it
        return None, "non-yahoo-symbol"
    if sym.endswith("=F"):
        return None, "futures"
    if sym.endswith("=X"):
        base = sym[:-2]
        if len(base) == 3:                 # Yahoo 'JPY=X' means USD/JPY
            return "USD/" + base, "forex"
        if len(base) == 6:                 # GBPUSD=X -> GBP/USD ; XAUUSD=X -> XAU/USD
            return base[:3] + "/" + base[3:], "forex"
        return None, "forex"
    if sym.startswith("^"):
        return None, "index"
    if "-" in sym and "." not in sym:
        b, _, q = sym.rpartition("-")
        if b and q in CRYPTO_QUOTES:       # BTC-USD -> BTC/USD
            return b + "/" + q, "crypto"
    if "." in sym:                         # exchange-suffixed: leave to Yahoo
        return None, "equity"
    return sym, "equity"                   # AAPL -> AAPL


def twelvedata_chart(symbol, interval, rng, api_key, td_symbol=None):
    """Twelve Data time_series -> (meta, rows ascending UTC). `td_symbol` (e.g. 'XAU/USD') is an
    explicit per-asset override used directly, bypassing the yahoo->TD mapping — so e.g. gold can be
    yahoo GC=F (futures) for the Yahoo fallback but XAU/USD (spot) on TD. Raises ValueError on an
    uncovered symbol or any non-ok payload (the fetch chain then falls back to Yahoo)."""
    if td_symbol:
        tsym = td_symbol
    else:
        tsym, klass = map_symbol_twelvedata(symbol)
        if tsym is None:
            raise ValueError(f"twelvedata does not cover {klass}")
    tiv = {"60m": "1h", "1h": "1h", "1d": "1day", "5m": "5min", "1m": "1min"}.get(interval, "1day")
    span_days = max(1, range_to_timedelta(rng).days)
    outsize = min(5000, max(60, span_days + 5) if tiv == "1day" else max(120, span_days * 8))
    url = (f"https://api.twelvedata.com/time_series?symbol={urllib.parse.quote(tsym)}"
           f"&interval={tiv}&outputsize={outsize}&order=ASC&timezone=UTC&apikey={api_key}")
    _td_throttle()                          # pace under the Basic plan's 8 req/min ceiling
    data = _http_json(url)
    if not isinstance(data, dict) or data.get("status") != "ok":
        msg = data.get("message") if isinstance(data, dict) else str(data)[:80]
        raise ValueError(f"twelvedata error for {tsym}: {str(msg)[:100]}")
    rows = []
    for r in data.get("values") or []:
        try:
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        except (KeyError, TypeError, ValueError):
            continue
        dt = r.get("datetime")
        if not dt:
            continue
        try:
            ts = int(datetime.strptime(dt, "%Y-%m-%d %H:%M:%S" if len(dt) > 10 else "%Y-%m-%d")
                     .replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
        vol = r.get("volume")
        try:
            vol = float(vol) if vol not in (None, "") else 0
        except (TypeError, ValueError):
            vol = 0
        rows.append({"ts": ts, "o": o, "h": h, "l": l, "c": c, "v": vol})
    rows.sort(key=lambda x: x["ts"])       # order=ASC already, but never trust ordering blindly
    m = data.get("meta") or {}
    meta = {"exchangeTimezoneName": m.get("exchange_timezone") or "UTC",
            "regularMarketPrice": rows[-1]["c"] if rows else None,
            "instrumentType": _TWELVEDATA_TYPE_MAP.get(m.get("type"), "EQUITY"),
            "currentTradingPeriod": None}
    return meta, rows


def _td_get(endpoint, symbol, api_key, **params):
    """One Twelve Data fundamentals GET (throttled, same per-minute budget as the chart fetch).
    Returns the parsed dict; raises ValueError on an error payload."""
    extra = "".join(f"&{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = (f"https://api.twelvedata.com/{endpoint}?symbol={urllib.parse.quote(symbol)}"
           f"&apikey={api_key}{extra}")
    _td_throttle()
    data = _http_json(url)
    if isinstance(data, dict) and data.get("status") == "error":
        raise ValueError(f"twelvedata {endpoint} error: {str(data.get('message'))[:80]}")
    return data


def twelvedata_fundamentals(symbol, api_key, td_symbol=None):
    """Compact equity fundamentals from Twelve Data (statistics + profile + most-recent earnings).
    Best-effort: each missing piece is simply omitted; returns a dict, or None if nothing usable.
    NARRATIVE/CONTEXT ONLY — never fed into confidence/scoring (slow-moving; would risk look-ahead)."""
    sym = td_symbol or symbol
    out, got = {"source": "twelvedata", "symbol": sym}, False
    try:
        st = (_td_get("statistics", sym, api_key).get("statistics") or {})
        vm, fin, sd = (st.get("valuations_metrics") or {}, st.get("financials") or {},
                       st.get("stock_statistics") or {})
        val = {k: vm[k] for k in ("market_capitalization", "trailing_pe", "forward_pe", "peg_ratio",
                                  "price_to_sales_ttm", "price_to_book_mrq") if vm.get(k) is not None}
        marg = {k: fin[k] for k in ("gross_margin", "operating_margin", "profit_margin",
                                    "return_on_equity_ttm", "return_on_assets_ttm") if fin.get(k) is not None}
        shr = {k: sd[k] for k in ("shares_outstanding", "52_week_high", "52_week_low", "beta")
               if sd.get(k) is not None}
        if val: out["valuation"] = val
        if marg: out["margins"] = marg
        if shr: out["share_stats"] = shr
        got = got or bool(val or marg)
    except Exception as ex:
        out["statistics_error"] = str(ex)[:80]
    try:
        pr = _td_get("profile", sym, api_key)
        prof = {k: pr[k] for k in ("sector", "industry", "employees", "website") if pr.get(k) is not None}
        if (pr.get("description") or "").strip():
            prof["description"] = pr["description"].strip()[:400]
        if prof: out["profile"] = prof
        got = got or bool(prof)
    except Exception as ex:
        out["profile_error"] = str(ex)[:80]
    try:
        er = (_td_get("earnings", sym, api_key, outputsize=1).get("earnings") or [])
        if er:
            e = er[0]
            le = {k: e[k] for k in ("date", "eps_estimate", "eps_actual", "surprise_prc")
                  if e.get(k) is not None}
            if le: out["latest_earnings"] = le; got = True
    except Exception as ex:
        out["earnings_error"] = str(ex)[:80]
    out["fetched_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    return out if got else None


def fetch_chart(symbol, interval, rng, provider=None, api_key=None, td_symbol=None):
    """Provider-agnostic OHLCV fetch -> (meta, rows ascending UTC).
    meta keys: exchangeTimezoneName, regularMarketPrice, instrumentType,
    currentTradingPeriod (None if the provider lacks it), provider, provider_note.

    Provider chain: configured provider (twelvedata / eodhd) -> Yahoo (query1->query2 host
    failover) -> CoinGecko (crypto only). The CoinGecko leg is keyless, DAILY-only, and reached
    ONLY when the essential daily series fails everywhere else, so a single-vendor outage no
    longer aborts the whole asset. Hourly keeps its prior behaviour (a failed/empty hourly
    degrades to daily-only analysis downstream)."""
    provider = provider or PROVIDER_DEFAULT
    if api_key is None:
        api_key = (os.environ.get("TWELVEDATA_API_KEY") if provider == "twelvedata"
                   else os.environ.get("EODHD_API_KEY"))
    note = None
    if provider == "twelvedata":
        if td_symbol:
            tsym, klass = td_symbol, "explicit"
        else:
            tsym, klass = map_symbol_twelvedata(symbol)
        if tsym is None:
            note = f"{klass} not covered by twelvedata; served by yahoo"
        elif not api_key:
            note = "TWELVEDATA_API_KEY not set; served by yahoo"
        else:
            try:
                meta, rows = twelvedata_chart(symbol, interval, rng, api_key, td_symbol=tsym)
                if rows:
                    meta["provider"], meta["provider_note"] = "twelvedata", None
                    return meta, rows
                note = "twelvedata returned 0 rows; served by yahoo"
            except Exception as ex:
                note = f"twelvedata failed ({str(ex)[:60]}); served by yahoo"
    elif provider == "eodhd":
        esym, klass = map_symbol_eodhd(symbol)
        if esym is None:
            note = f"{klass} not covered by eodhd; served by yahoo"
        elif not api_key:
            note = "EODHD_API_KEY not set; served by yahoo"
        else:
            try:
                meta, rows = eodhd_chart(symbol, interval, rng, api_key)
                if rows:
                    meta["provider"], meta["provider_note"] = "eodhd", None
                    return meta, rows
                note = "eodhd returned 0 rows; served by yahoo"
            except Exception as ex:
                note = f"eodhd failed ({str(ex)[:60]}); served by yahoo"
    try:
        meta, rows = yahoo_chart(symbol, interval, rng)
        if rows or interval != "1d":
            meta = dict(meta)
            meta["provider"], meta["provider_note"] = "yahoo", note
            return meta, rows                          # success, or an empty hourly (caller degrades)
        ynote = "yahoo returned 0 daily rows"
    except Exception as ex:
        if interval != "1d":
            raise                                      # hourly: preserve the existing degrade path
        ynote = f"yahoo failed ({str(ex)[:70]})"
    # Daily series is essential — for crypto, try the keyless CoinGecko fallback before giving up.
    if map_symbol_coingecko(symbol) is not None:
        try:
            meta, rows = coingecko_chart(symbol, rng)
            if rows:
                meta["provider"] = "coingecko"
                meta["provider_note"] = "; ".join(x for x in (note, ynote, "served by coingecko") if x)
                return meta, rows
            ynote = f"{ynote}; coingecko returned 0 rows"
        except Exception as ex:
            ynote = f"{ynote}; coingecko failed ({str(ex)[:60]})"
    raise RuntimeError(f"no daily data for {symbol}: {'; '.join(x for x in (note, ynote) if x)}")


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


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def rsi14(closes):
    n = 14
    if len(closes) <= n:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(d, 0) for d in deltas[:n]) / n
    al = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        ag = (ag * 13 + max(d, 0)) / 14
        al = (al * 13 + max(-d, 0)) / 14
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def atr14(rows):
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:14]) / 14
    for t in trs[14:]:
        a = (a * 13 + t) / 14
    return a


def macd(closes):
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    if not e26:
        return None
    line = [a - b for a, b in zip(e12[-len(e26):], e26)]
    sig = ema_series(line, 9)
    if not sig:
        return None
    hist = [a - b for a, b in zip(line[-len(sig):], sig)]
    return {"macd": round(line[-1], 6), "signal": round(sig[-1], 6), "hist": round(hist[-1], 6),
            "hist_prev": round(hist[-2], 6) if len(hist) > 1 else None,
            "cross": ("bullish" if line[-1] > sig[-1] else "bearish")}


def classify_long_term(closes, s50, s200, lookback=20):
    """Daily regime: Uptrend / Downtrend / Range from price vs SMA200, SMA50/200 cross, slope."""
    if s200 is None or s50 is None:
        return "Insufficient data"
    last = closes[-1]
    slope_up = len(closes) > lookback and closes[-1] > closes[-1 - lookback]
    votes = int(last > s200) + int(s50 > s200) + int(slope_up)
    if votes >= 2 and last > s200:
        return "Uptrend"
    if votes <= 1 and last < s200:
        return "Downtrend"
    return "Range"


def classify_intraday_trend(hc, s20, s50, e9, e21):
    votes = 0
    votes += int(s50 is not None and hc[-1] > s50)
    votes += int(s20 is not None and hc[-1] > s20)
    votes += int(e9 is not None and e21 is not None and e9 > e21)
    votes += int(len(hc) > 24 and hc[-1] > hc[-25])
    if votes >= 3:
        return "Uptrend"
    if votes <= 1:
        return "Downtrend"
    return "Range"


def alignment_verdict(lt, it):
    if lt == it and lt in ("Uptrend", "Downtrend"):
        return "aligned-" + ("up" if lt == "Uptrend" else "down")
    if lt == "Range":
        return "mixed (long-term range)"
    if it == "Range":
        return "mixed (intraday range)"
    return "counter-trend (intraday against long-term)"


def level_stats(sessions_sorted, atr_d, max_n=120):
    """Empirical band-containment and pivot-touch rates over completed sessions.
    Approximation: uses the CURRENT ATR for all historical bands (documented)."""
    comp = sessions_sorted[:-1]  # exclude in-progress session
    pairs = list(zip(comp, comp[1:]))[-max_n:]
    if not pairs or not atr_d:
        return None
    inner = outer = pp_t = r1_t = s1_t = 0
    ranges = []
    for prev, cur in pairs:
        pp = (prev["h"] + prev["l"] + prev["c"]) / 3
        r1, s1 = 2 * pp - prev["l"], 2 * pp - prev["h"]
        move = abs(cur["c"] - prev["c"])  # net session move (prior close ~ session open in 24h markets)
        inner += move <= 0.5 * atr_d
        outer += move <= 1.0 * atr_d
        pp_t += cur["l"] <= pp <= cur["h"]
        r1_t += cur["h"] >= r1
        s1_t += cur["l"] <= s1
        ranges.append(cur["h"] - cur["l"])
    n = len(pairs)
    ranges.sort()
    return {
        "sessions_evaluated": n,
        "close_inside_inner_band_pct": round(100 * inner / n, 1),
        "close_inside_outer_band_pct": round(100 * outer / n, 1),
        "touched_PP_pct": round(100 * pp_t / n, 1),
        "touched_R1_pct": round(100 * r1_t / n, 1),
        "touched_S1_pct": round(100 * s1_t / n, 1),
        "median_session_range": round(ranges[n // 2], 6),
        "note": "containment = |close - prior close| vs current ATR(14) bands (approximation); touches use session H/L vs prior-session pivots",
    }


def swings(rows, k=2):
    """Confirmed swing highs/lows (fractal, k bars either side)."""
    hi, lo = [], []
    for i in range(k, len(rows) - k):
        win = rows[i - k:i + k + 1]
        if rows[i]["h"] == max(r["h"] for r in win):
            hi.append({"t": rows[i]["ts"], "p": rows[i]["h"]})
        if rows[i]["l"] == min(r["l"] for r in win):
            lo.append({"t": rows[i]["ts"], "p": rows[i]["l"]})
    return hi[-5:], lo[-5:]


def compute_pivots_bands(prior_hlc, anchor_close, atr_daily):
    """Shared floor-pivots + ATR day-band math for both the live and anchored paths.

    prior_hlc    dict/mapping with "h","l","c" of the session the pivots derive from
                 (live path: prior completed session; anchored path: chosen session).
    anchor_close band anchor: the live path passes TODAY'S session open; the anchored
                 paths pass the chosen completed session's close. None => no bands.
    atr_daily    daily ATR(14); None/0 => no bands.

    Returns (pivots_dict | None, bands_dict | None), UNROUNDED — callers round/tag
    exactly as before. Math is identical to the original inline block so swapping the
    live path to this helper is byte-for-byte (see the golden-file tests).
    """
    pivots = None
    if prior_hlc:
        pp = (prior_hlc["h"] + prior_hlc["l"] + prior_hlc["c"]) / 3
        rng = prior_hlc["h"] - prior_hlc["l"]
        pivots = {"PP": pp, "R1": 2 * pp - prior_hlc["l"], "S1": 2 * pp - prior_hlc["h"],
                  "R2": pp + rng, "S2": pp - rng,
                  "R3": prior_hlc["h"] + 2 * (pp - prior_hlc["l"]),
                  "S3": prior_hlc["l"] - 2 * (prior_hlc["h"] - pp)}
    bands = None
    if atr_daily and anchor_close is not None:
        bands = {"open": anchor_close,
                 "inner_hi": anchor_close + 0.5 * atr_daily,
                 "inner_lo": anchor_close - 0.5 * atr_daily,
                 "outer_hi": anchor_close + 1.0 * atr_daily,
                 "outer_lo": anchor_close - 1.0 * atr_daily}
    return pivots, bands


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
    if (args.get("--fundamentals") in ("1", "true", "on", "yes")
            and (provider or PROVIDER_DEFAULT) == "twelvedata" and cutoff_ts is None):
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
                     "note": "; ".join(notes) if notes else None},
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
