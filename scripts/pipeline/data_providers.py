"""data_providers.py — provider/fetch layer extracted from intraday.py.

Network I/O only (Yahoo host-failover, Twelve Data, EODHD, CoinGecko), symbol maps, the TD
throttle, fundamentals, and license provenance. Imports NOTHING from intraday (one-way edge).
"""
import json, os, sys, threading, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
PROVIDER_DEFAULT = os.environ.get("ADVISOR_DATA_PROVIDER", "yahoo")
# Data-license mode: "personal" (default, today's behaviour incl. free Yahoo/CoinGecko fallbacks)
# or "commercial" (a series sourced from a non-commercial feed is flagged license_degraded so the
# provenance marks it not-for-redistribution). Set via ASSETFRAME_DATA_LICENSE / engine.json.
DATA_LICENSE = os.environ.get("ASSETFRAME_DATA_LICENSE", "personal")

# Which feeds may legally back a PAID, published report. `commercial=True` is an OPERATOR ASSERTION
# that the named redistribution contract is signed — flip it ONLY when the plan is actually in place.
# Yahoo (unofficial chart API) and the keyless CoinGecko Demo tier have NO commercial/redistribution
# licence and no upgrade path, so they can never back a sellable report.
PROVIDER_REGISTRY = {
    #              commercial  needs_key   required plan / note
    "yahoo":      {"commercial": False, "needs_key": False},   # personal use only; no commercial tier
    "coingecko":  {"commercial": False, "needs_key": False},   # keyless Demo tier = non-commercial
    "twelvedata": {"commercial": True,  "needs_key": True},    # Business plan + Redistribution Add-On
    "eodhd":      {"commercial": True,  "needs_key": True},    # Commercial plan + redistribution approval
}


def provider_is_commercial(provider):
    """True if `provider` may back a paid, published report (per PROVIDER_REGISTRY)."""
    return bool(PROVIDER_REGISTRY.get(provider, {}).get("commercial"))


def series_license(provider):
    """License tag for a fetched series given its resolved provider: 'commercial' | 'non_commercial'."""
    return "commercial" if provider_is_commercial(provider) else "non_commercial"


def license_fields(hourly_provider, daily_provider, mode=None):
    """License-provenance fields for the analysis `provider` block. `license_degraded` is True only
    in commercial mode when a fetched series came from a non-commercial feed (the report still
    publishes, but the provenance marks it not-for-redistribution). A None provider = no such series,
    so it can't degrade anything."""
    mode = mode or DATA_LICENSE
    degraded = mode == "commercial" and (
        (hourly_provider is not None and not provider_is_commercial(hourly_provider)) or
        (daily_provider is not None and not provider_is_commercial(daily_provider)))
    return {
        "license_mode": mode,
        "hourly_license": series_license(hourly_provider) if hourly_provider is not None else None,
        "daily_license": series_license(daily_provider) if daily_provider is not None else None,
        "license_degraded": bool(degraded),
    }


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
    interval = {"1week": "1wk", "1month": "1mo"}.get(interval, interval)  # Yahoo's vocabulary
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
    tiv = {"60m": "1h", "1h": "1h", "1d": "1day", "5m": "5min", "1m": "1min",
           "1week": "1week", "1month": "1month"}.get(interval)
    if tiv is None:                                   # explicit, not a silent default-to-daily
        raise ValueError(f"twelvedata: unsupported interval {interval!r}")
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
