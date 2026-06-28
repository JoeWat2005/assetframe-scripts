"""Phase-2 INTEGRATION tests for scripts/pipeline/marketdata/* (the whole subdir WIRED together).

Unlike tests/test_pipeline_marketdata_unit.py (which exercises each function in isolation), this
drives the real CROSS-MODULE flow and asserts the DATA CONTRACTS between the modules:

    intraday.main()
        -> data_providers.fetch_chart -> data_providers.yahoo_chart   (real provider chain + parser)
        -> indicators.{sma,ema_series,rsi14,atr14,macd,classify_*,level_stats,swings,
                       compute_pivots_bands}                            (real indicator math)
        -> writes data/analysis/<NAME>_analysis.json + the candle CSVs  (real serialisation)

and the market-weather flow:

    market_context.build_market_weather -> intraday.fetch_chart (re-export) -> yahoo_chart

The ONLY thing faked is the true external boundary: the network. We monkeypatch
data_providers._http_json (the single urllib call) to return realistic Yahoo `chart` JSON, so the
real yahoo_chart parser, the real fetch_chart provider chain, every indicator, the freshness/level/
trend analysis, the chart-interval resampling, the licence provenance, and the JSON/CSV writers all
run for real. No Anthropic / Neon / R2 / subprocess are involved in this subdir. All writes are
redirected to a pytest tmp_path; the real ledger/reports/data are never touched.

The strongest assertions here are CONTRACT cross-checks: the analysis JSON's indicator values must
equal what the real `indicators` module produces from the same bars, and the additive `intervals`
block must agree with the canonical `daily`/`hourly` blocks it derives from.

Run:  python -m pytest tests/test_pipeline_marketdata_integration.py -q
"""
import math
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import data_providers as DP
import indicators as ind
import intraday as I
import market_context as MC


# ----------------------------------------------------------------------------- fixture data builders
# A fixed UTC "as-of" moment makes the whole run deterministic: freshness is measured at the cutoff
# (not wall-clock), last_price falls back to the last bar's close (never a live quote), and equity
# fundamentals are skipped. We generate bars up to (and including) the cutoff.
AS_OF = "2026-06-15 12:00"                                  # a Monday, mid-day UTC
CUTOFF_DT = datetime.strptime(AS_OF, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
CUTOFF_TS = int(CUTOFF_DT.timestamp())

META_CRYPTO = {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC",
               "regularMarketPrice": 1234.5, "currentTradingPeriod": None}


def _gen_daily(n=300):
    """A gently rising daily series (overall uptrend, small oscillation) ending at the cutoff date's
    UTC midnight. n>=200 so SMA200 warms. Returns tuples (ts,o,h,l,c,v)."""
    base_midnight = datetime.fromtimestamp(CUTOFF_TS - (n - 1) * 86400, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(n):
        ts = int((base_midnight + timedelta(days=i)).timestamp())
        p = 100.0 + i * 0.5 + 3.0 * math.sin(i / 6.0)
        out.append((ts, p - 0.5, p + 1.2, p - 1.3, p, 1000 + i))
    return out


def _gen_hourly(days=35):
    """A dense 1h series for ~`days` days ending exactly at the cutoff (last bar age 0)."""
    n = days * 24
    start = CUTOFF_TS - (n - 1) * 3600
    out = []
    for i in range(n):
        ts = start + i * 3600
        p = 200.0 + i * 0.05 + 2.0 * math.sin(i / 9.0)
        out.append((ts, p - 0.2, p + 0.6, p - 0.7, p, 10 + (i % 7)))
    return out


STD_DAILY = _gen_daily(300)
STD_HOURLY = _gen_hourly(35)


def _yahoo_payload(bars, meta):
    """Build a realistic Yahoo `chart` JSON envelope from (ts,o,h,l,c,v) tuples (c may be None to
    exercise yahoo_chart's null-bar dropping)."""
    return {"chart": {"error": None, "result": [{
        "meta": meta,
        "timestamp": [b[0] for b in bars],
        "indicators": {"quote": [{
            "open":   [b[1] for b in bars],
            "high":   [b[2] for b in bars],
            "low":    [b[3] for b in bars],
            "close":  [b[4] for b in bars],
            "volume": [b[5] for b in bars],
        }]},
    }]}}


def _rows(bars):
    """Tuples -> the dict shape data_providers/indicators pass around."""
    return [{"ts": b[0], "o": b[1], "h": b[2], "l": b[3], "c": b[4], "v": b[5]} for b in bars]


def _install_fake_feed(monkeypatch, daily=STD_DAILY, hourly=STD_HOURLY, meta=META_CRYPTO):
    """Patch the ONE network boundary so interval=1d requests get the daily payload and every other
    interval gets the hourly payload. Returns nothing — the real yahoo_chart/fetch_chart run."""
    def fake_http_json(url, *a, **k):
        if "interval=1d" in url:
            return _yahoo_payload(daily, meta)
        return _yahoo_payload(hourly, meta)
    monkeypatch.setattr(DP, "_http_json", fake_http_json)


def _run_intraday(monkeypatch, tmp_path, symbol="BTC-USD", extra=None):
    """Run the REAL intraday.main end-to-end against the faked feed; return (parsed_json, datadir)."""
    import json
    datadir = tmp_path / "data"
    argv = ["intraday.py", symbol, "--datadir", str(datadir), "--provider", "yahoo", "--as-of", AS_OF]
    argv += extra or []
    monkeypatch.setattr(sys, "argv", argv)
    I.main()
    name = symbol.replace("=", "").replace("^", "")
    aj = json.loads((datadir / "analysis" / f"{name}_analysis.json").read_text(encoding="utf-8"))
    return aj, datadir


VALID_LT = {"Uptrend", "Downtrend", "Range", "Insufficient data"}


# ===================================================================== happy path: structure present
def test_happy_path_writes_full_analysis_and_files(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, datadir = _run_intraday(monkeypatch, tmp_path)

    # top-level contract the rest of the pipeline (scaffold_payload / report_pdf) reads
    for key in ("symbol", "last_price", "last_bar_utc", "freshness", "windows", "provider",
                "hourly", "trend", "stats_last_sessions", "daily", "pivots_classic",
                "atr_day_bands", "files", "chart_intervals", "intervals", "as_of"):
        assert key in aj, f"missing top-level analysis key {key!r}"

    assert aj["symbol"] == "BTC-USD"
    assert aj["degraded"] is None
    assert aj["as_of"] == AS_OF

    # the candle CSVs the JSON advertises were actually written (no header rows; one line per bar)
    hcsv = datadir / "candles" / "BTC-USD_hourly.csv"
    dcsv = datadir / "candles" / "BTC-USD_daily.csv"
    assert aj["files"]["hourly_csv"] == hcsv.as_posix()
    assert aj["files"]["daily_csv"] == dcsv.as_posix()
    assert hcsv.exists() and dcsv.exists()


def test_indicator_blocks_present_and_sane(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)

    d, h = aj["daily"], aj["hourly"]
    # daily regime block
    assert d["bars"] == len(STD_DAILY)
    for k in ("sma20", "sma50", "sma100", "sma200"):
        assert isinstance(d[k], float) and d[k] > 0
    assert isinstance(d["rsi14"], float) and 0.0 <= d["rsi14"] <= 100.0
    assert isinstance(d["atr14"], float) and d["atr14"] > 0
    assert isinstance(d["realized_vol_20d_pct"], float)

    # hourly trend block
    assert h["bars"] == len(STD_HOURLY)
    for k in ("sma20", "sma50", "ema9", "ema21"):
        assert isinstance(h[k], float) and h[k] > 0
    assert isinstance(h["rsi14"], float) and 0.0 <= h["rsi14"] <= 100.0
    assert isinstance(h["atr14"], float) and h["atr14"] > 0
    # macd is the structured dict the report renderer expects
    assert set(h["macd"]) == {"macd", "signal", "hist", "hist_prev", "cross"}
    assert h["macd"]["cross"] in ("bullish", "bearish")
    assert isinstance(h["swing_highs"], list) and isinstance(h["swing_lows"], list)
    assert h["above_sma20"] in (True, False)

    # trend / alignment block
    t = aj["trend"]
    assert t["long_term_daily"] == "Uptrend"              # rising daily series
    assert t["intraday_hourly"] in VALID_LT
    assert isinstance(t["alignment"], str) and t["alignment"]
    assert t["golden_cross"] in (True, False)


# ===================================================================== CONTRACT: JSON == real indicators
def test_analysis_numbers_match_the_indicators_module(monkeypatch, tmp_path):
    """The heart of the integration: the values intraday wrote must equal what the REAL indicators
    module produces from the very same bars — proving the wiring passes the right series, in the
    right shape, to the right function (catches any silent drift across the refactor)."""
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)

    drows, hrows = _rows(STD_DAILY), _rows(STD_HOURLY)
    dc = [r["c"] for r in drows]
    hc = [r["c"] for r in hrows]
    atr_d = ind.atr14(drows)

    # daily
    assert aj["daily"]["sma200"] == ind.sma(dc, 200)
    assert aj["daily"]["sma50"] == ind.sma(dc, 50)
    assert aj["daily"]["rsi14"] == ind.rsi14(dc)
    assert aj["daily"]["atr14"] == atr_d
    # hourly
    assert aj["hourly"]["rsi14"] == ind.rsi14(hc)
    assert aj["hourly"]["atr14"] == ind.atr14(hrows)
    assert aj["hourly"]["macd"] == ind.macd(hc)
    # trend classifiers fed the exact same SMAs
    assert aj["trend"]["long_term_daily"] == ind.classify_long_term(dc, ind.sma(dc, 50), ind.sma(dc, 200))
    # level stats computed over the daily series with the daily ATR
    assert aj["stats_last_sessions"] == ind.level_stats(drows, atr_d)


def test_freshness_and_last_price_reflect_the_cutoff(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)

    f = aj["freshness"]
    assert f["instrument_type"] == "CRYPTOCURRENCY"
    assert f["market_state"] == "open"                    # crypto = always open
    assert f["stale"] is False                            # last bar sits exactly at the cutoff
    assert f["age_minutes"] == 0
    assert f["last_bar_utc"] == AS_OF
    assert aj["last_bar_utc"] == f["last_bar_utc"]        # top-level mirrors the freshness block
    # --as-of run: last_price is the last bar's close, never a live quote (regularMarketPrice)
    assert aj["last_price"] == round(STD_HOURLY[-1][4], 6)


def test_pivots_and_bands_are_internally_ordered(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)

    p = aj["pivots_classic"]
    assert set(("PP", "R1", "R2", "R3", "S1", "S2", "S3")).issubset(p)
    assert p["R1"] >= p["PP"] >= p["S1"]                  # floor-pivot identity holds for any HLC

    b = aj["atr_day_bands"]
    assert b["outer_hi"] > b["inner_hi"] > b["open"] > b["inner_lo"] > b["outer_lo"]
    # bands are anchored at today's session open +/- ATR multiples
    assert b["inner_hi"] - b["open"] == pytest.approx(aj["daily"]["atr14"] * 0.5, rel=1e-9)
    assert b["outer_hi"] - b["open"] == pytest.approx(aj["daily"]["atr14"] * 1.0, rel=1e-9)


# ===================================================================== chart_intervals (derived series)
def test_intervals_block_agrees_with_canonical_and_writes_derived_csvs(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, datadir = _run_intraday(monkeypatch, tmp_path,
                                extra=["--chart-intervals", "60m,4h,1d,1week"])

    iv = aj["intervals"]
    assert set(iv) == {"60m", "4h", "1d", "1week"}
    assert aj["chart_intervals"] == ["60m", "1d", "4h", "1week"]   # canonical pair first, then extras

    # the canonical interval blocks must be byte-identical to the daily/hourly blocks they re-derive
    assert iv["1d"]["atr14"] == aj["daily"]["atr14"]
    assert iv["1d"]["sma20"] == aj["daily"]["sma20"]
    assert iv["1d"]["sma50"] == aj["daily"]["sma50"]
    assert iv["1d"]["rsi14"] == aj["daily"]["rsi14"]
    assert iv["1d"]["trend"] == aj["trend"]["long_term_daily"]
    assert iv["60m"]["atr14"] == aj["hourly"]["atr14"]
    assert iv["60m"]["rsi14"] == aj["hourly"]["rsi14"]
    assert iv["60m"]["sma20"] == aj["hourly"]["sma20"]
    assert iv["60m"]["bars"] == aj["hourly"]["bars"]

    # derived intervals are RESAMPLED from the canonical pair (no extra fetch) and written to disk
    assert 0 < iv["4h"]["bars"] < aj["hourly"]["bars"]            # 1h -> 4h compresses
    assert 0 < iv["1week"]["bars"] < aj["daily"]["bars"]          # daily -> weekly compresses
    for name in ("4h", "1week"):
        csv_path = tmp_path / "data" / "candles" / f"BTC-USD_{name}.csv"
        assert iv[name]["csv"] == csv_path.as_posix()
        assert csv_path.exists()
        assert sum(1 for ln in csv_path.read_text().splitlines() if ln.strip()) == iv[name]["bars"]


# ===================================================================== licence provenance switch
def test_personal_licence_is_not_degraded_by_yahoo(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)
    prov = aj["provider"]
    assert prov["hourly"] == "yahoo" and prov["daily"] == "yahoo"
    assert prov["license_mode"] == "personal"
    assert prov["license_degraded"] is False              # personal mode tolerates Yahoo


def test_commercial_licence_flags_non_commercial_feed(monkeypatch, tmp_path):
    """End-to-end of the ASSETFRAME_DATA_LICENSE switch: in commercial mode a Yahoo-served series
    (non-commercial) must mark the analysis license_degraded — the provenance flows from
    data_providers.license_fields straight into intraday's output block."""
    monkeypatch.setattr(DP, "DATA_LICENSE", "commercial")
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path)
    prov = aj["provider"]
    assert prov["license_mode"] == "commercial"
    assert prov["hourly_license"] == "non_commercial"
    assert prov["daily_license"] == "non_commercial"
    assert prov["license_degraded"] is True


# ===================================================================== related instruments
def test_related_symbols_resolved_through_real_fetch(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path, extra=["--related", "ETH-USD"])
    rel = aj["related"]
    assert isinstance(rel, list) and len(rel) == 1
    entry = rel[0]
    assert entry["symbol"] == "ETH-USD"
    assert "error" not in entry
    assert entry["last"] == round(STD_DAILY[-1][4], 6)    # related uses the 1d series' last close
    assert isinstance(entry["chg_1d_pct"], float)
    assert isinstance(entry["chg_5d_pct"], float)


# ===================================================================== anchored override (--anchor)
def test_anchored_prior_completed_preserves_live(monkeypatch, tmp_path):
    _install_fake_feed(monkeypatch)
    aj, _ = _run_intraday(monkeypatch, tmp_path, extra=["--anchor", "prior-completed"])
    anc = aj["anchor"]
    assert anc["mode"] == "prior-completed" and anc["applied"] is True
    # when the anchored path applies it OVERWRITES pivots/bands and preserves the live ones under *_live
    assert "pivots_classic_live" in aj
    assert "atr_day_bands_live" in aj
    assert aj["atr_day_bands"]["anchor"] == "prior-completed_session_close"


# ===================================================================== degraded daily-only path
def test_degraded_daily_only_rebuilds_from_daily_bars(monkeypatch, tmp_path):
    """<24 hourly bars but usable daily -> the run still succeeds as 'daily_only': hourly block is
    null, sessions+pivots are rebuilt from the last two DAILY bars and tagged, bands anchor on the
    last daily close. Exercises the indicators<->intraday fallback contract."""
    thin_hourly = [(CUTOFF_TS - (4 - i) * 3600, 200.0 + i, 200.6 + i, 199.4 + i, 200.2 + i, 5)
                   for i in range(5)]
    _install_fake_feed(monkeypatch, hourly=thin_hourly)
    aj, _ = _run_intraday(monkeypatch, tmp_path)

    assert aj["degraded"] == "daily_only"
    assert aj["hourly"] is None
    assert aj["errors"] and "hourly" in aj["errors"]
    assert aj["pivots_classic"]["basis"] == "daily_bars_fallback"
    assert aj["atr_day_bands"]["anchor"] == "prior_close_fallback"
    assert aj["daily"]["prior_session"]["basis"] == "daily_bars_fallback"
    # daily indicators still computed for real
    assert aj["daily"]["atr14"] == ind.atr14(_rows(STD_DAILY))


# ===================================================================== yahoo parser <-> intraday contract
def test_null_close_bars_are_dropped_before_analysis(monkeypatch, tmp_path):
    """A Yahoo payload with null closes must be filtered by the REAL yahoo_chart parser, so the
    downstream bar counts / indicators only ever see complete OHLC rows."""
    holey = [list(b) for b in STD_DAILY]
    holey[10][4] = None                                   # punch two null closes into the daily feed
    holey[20][4] = None
    holey = [tuple(b) for b in holey]
    _install_fake_feed(monkeypatch, daily=holey)
    aj, _ = _run_intraday(monkeypatch, tmp_path)
    assert aj["daily"]["bars"] == len(STD_DAILY) - 2
    assert isinstance(aj["daily"]["atr14"], float) and aj["daily"]["atr14"] > 0


# ===================================================================== market_context wired flow
def test_market_weather_through_the_real_fetch_chain(monkeypatch, tmp_path):
    """market_context.build_market_weather -> intraday.fetch_chart (re-export) -> data_providers
    fetch_chain -> yahoo_chart, all real; only the network is faked. Asserts the macro grouping,
    risk-tone heuristic and deterministic clock are wired correctly across the module boundary."""
    def two_bar_up_05pct(*_a, **_k):
        base, ts0 = 100.0, int(datetime(2026, 6, 12, tzinfo=timezone.utc).timestamp())
        last = base * 1.005
        bars = [(ts0, base, base + 1, base - 1, base, 5),
                (ts0 + 86400, last, last + 1, last - 1, last, 5)]
        return _yahoo_payload(bars, {"instrumentType": "FUTURE", "exchangeTimezoneName": "UTC",
                                     "regularMarketPrice": last, "currentTradingPeriod": None})
    monkeypatch.setattr(DP, "_http_json", two_bar_up_05pct)

    fixed = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)
    w = MC.build_market_weather(now=fixed)

    assert w["as_of_utc"] == "2026-06-15 09:30"           # the injected clock, not wall-clock
    # +0.5% on S&P futures + the Asian session => risk-on (VIX flat doesn't veto)
    assert w["risk_tone"] == "risk-on"
    assert {"S&P 500 futures", "Nasdaq 100 futures", "Nikkei 225", "Hang Seng",
            "VIX (volatility)"}.issubset(w["overnight_recap"])
    assert {"US Dollar Index", "US 10Y yield", "WTI crude"} == set(w["macro_drivers"])
    es = w["overnight_recap"]["S&P 500 futures"]
    assert es["change_pct"] == 0.5 and es["symbol"] == "ES=F" and es["group"] == "risk"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
