"""Offline unit tests for scripts/pipeline/marketdata/* (post-refactor into subgroups).

Targets the GAPS left by the existing suites:
  - indicators.py  : NOT imported by any existing test — exercised here directly (sma, ema_series,
                     rsi14, atr14, macd, classify_long_term, classify_intraday_trend,
                     alignment_verdict, level_stats, swings, compute_pivots_bands edge math).
  - data_providers : map_symbol_eodhd, range_to_timedelta, eodhd_chart parsing (only covered
                     transitively / not at all before).
  - intraday       : freshness_block crypto/FX/equity/daily branches + interval_block trend paths.
  - market_context : _daily_change + _risk_tone directly, build_market_weather determinism.

Everything is offline & deterministic: no network/Neon/Anthropic/R2. The only I/O-bearing calls
(intraday.fetch_chart, data_providers._http_json) are monkeypatched. Mirrors the existing tests'
import style (sys.path.insert + flat `import <mod>`), which resolves via tests/conftest.py's shim.

Run:  python -m pytest tests/test_pipeline_marketdata_unit.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import indicators as ind
import data_providers as DP
import intraday as I
import market_context as MC


# ===================================================================== indicators: moving averages
def test_sma_returns_mean_of_last_n():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5            # (3+4)/2
    assert ind.sma([2, 4], 2) == 3.0                  # exactly n bars


def test_sma_insufficient_history_returns_none():
    assert ind.sma([1], 2) is None
    assert ind.sma([], 5) is None


def test_ema_series_insufficient_returns_empty():
    assert ind.ema_series([1, 2], 3) == []


def test_ema_series_seed_is_sma_of_first_n_and_full_length():
    es = ind.ema_series([1, 2, 3, 4, 5], 3)
    assert es[0] == 2.0                               # seed = (1+2+3)/3
    assert len(es) == 3                               # one value per bar from index n-1 onward


def test_ema_series_tracks_a_rising_series_upward():
    es = ind.ema_series([float(x) for x in range(1, 30)], 10)
    assert es[-1] > es[0]                             # EMA rises with the data


# ===================================================================== indicators: RSI / ATR / MACD
def test_rsi14_needs_more_than_14_closes():
    assert ind.rsi14(list(range(14))) is None         # 14 closes -> None (needs >14)


def test_rsi14_all_gains_is_100():
    assert ind.rsi14(list(range(15))) == 100.0        # zero average loss -> 100.0


def test_rsi14_stays_within_bounds_for_mixed_series():
    closes = [10, 11, 10.5, 12, 11.5, 13, 12.5, 14, 13, 15, 14.5, 16, 15, 17, 16.5, 18]
    r = ind.rsi14(closes)
    assert r is not None and 0.0 <= r <= 100.0


def test_atr14_needs_at_least_15_rows():
    rows = [{"h": i + 1, "l": i, "c": i + 0.5} for i in range(14)]
    assert ind.atr14(rows) is None


def test_atr14_constant_one_point_range_is_one():
    # every bar spans exactly 1.0 high-low and closes flat -> true range == 1.0 each -> ATR == 1.0
    rows = [{"h": 1.0, "l": 0.0, "c": 0.5} for _ in range(20)]
    assert ind.atr14(rows) == 1.0


def test_macd_returns_none_without_enough_history():
    assert ind.macd(list(range(20))) is None          # cannot form the 26-period EMA


def test_macd_structure_and_cross_direction():
    closes = [float(x) for x in range(40, 0, -1)] + [float(x) for x in range(1, 20)]  # V-shape
    m = ind.macd(closes)
    assert set(m) == {"macd", "signal", "hist", "hist_prev", "cross"}
    assert m["cross"] == "bullish"                    # recovering leg -> line above signal


def test_macd_hist_prev_none_at_minimum_signal_length():
    # exactly 34 closes -> the MACD line is 9 long -> the signal EMA has a single value -> no prev hist
    m = ind.macd([float(x) for x in range(34)])
    assert m["hist_prev"] is None
    assert m["cross"] == "bearish"                    # monotonic ramp: line == signal -> not strictly >


# ===================================================================== indicators: trend classifiers
def test_classify_long_term_insufficient_when_smas_missing():
    assert ind.classify_long_term([1, 2, 3], None, 5) == "Insufficient data"
    assert ind.classify_long_term([1, 2, 3], 5, None) == "Insufficient data"


def test_classify_long_term_uptrend_downtrend_range():
    up = [float(x) for x in range(50)]
    assert ind.classify_long_term(up, s50=40, s200=10) == "Uptrend"
    down = [float(50 - x) for x in range(50)]
    assert ind.classify_long_term(down, s50=10, s200=40) == "Downtrend"
    # price above SMA200 but no positive slope and SMA50<=SMA200 -> only one vote -> Range
    assert ind.classify_long_term([100.0] * 25, s50=40, s200=50) == "Range"


def test_classify_long_term_lookback_slope_flips_vote():
    flat = [50.0] * 30
    # last == lookback-ago so slope_up False; with s50<=s200 and last>s200 -> Range not Uptrend
    assert ind.classify_long_term(flat, s50=40, s200=45, lookback=20) == "Range"


def test_classify_intraday_trend_three_regimes():
    up = [float(x) for x in range(30)]
    assert ind.classify_intraday_trend(up, s20=10, s50=5, e9=8, e21=7) == "Uptrend"
    down = [float(30 - x) for x in range(30)]
    assert ind.classify_intraday_trend(down, s20=20, s50=25, e9=5, e21=8) == "Downtrend"
    assert ind.classify_intraday_trend([5.0] * 30, s20=4, s50=4, e9=5, e21=6) == "Range"


def test_classify_intraday_trend_handles_none_indicators():
    # all indicator inputs None -> zero votes -> Downtrend, never raises
    assert ind.classify_intraday_trend([1.0] * 5, None, None, None, None) == "Downtrend"


def test_alignment_verdict_all_branches():
    assert ind.alignment_verdict("Uptrend", "Uptrend") == "aligned-up"
    assert ind.alignment_verdict("Downtrend", "Downtrend") == "aligned-down"
    assert ind.alignment_verdict("Range", "Uptrend") == "mixed (long-term range)"
    assert ind.alignment_verdict("Uptrend", "Range") == "mixed (intraday range)"
    assert ind.alignment_verdict("Uptrend", "Downtrend") == "counter-trend (intraday against long-term)"


# ===================================================================== indicators: level_stats
def test_level_stats_golden_containment_and_touch_rates():
    sessions = [
        {"h": 10, "l": 0, "c": 5, "ts": 1},
        {"h": 12, "l": 2, "c": 6, "ts": 2},
        {"h": 20, "l": 10, "c": 15, "ts": 3},
        {"h": 1, "l": 1, "c": 1, "ts": 4},   # in-progress session, excluded by comp[:-1]
    ]
    s = ind.level_stats(sessions, 10.0)
    assert s["sessions_evaluated"] == 2
    assert s["close_inside_inner_band_pct"] == 50.0
    assert s["close_inside_outer_band_pct"] == 100.0
    assert s["touched_PP_pct"] == 50.0
    assert s["touched_R1_pct"] == 100.0
    assert s["touched_S1_pct"] == 0.0
    assert s["median_session_range"] == 10


def test_level_stats_none_without_atr_or_enough_sessions():
    sessions = [{"h": 10, "l": 8, "c": 9, "ts": 1}, {"h": 11, "l": 9, "c": 10, "ts": 2},
                {"h": 12, "l": 10, "c": 11, "ts": 3}]
    assert ind.level_stats(sessions, None) is None        # no ATR -> no bands -> None
    assert ind.level_stats(sessions, 0) is None            # ATR 0 is falsy -> None
    assert ind.level_stats(sessions[:2], 5.0) is None      # only 1 completed -> no pairs -> None
    assert ind.level_stats([], 5.0) is None


def test_level_stats_respects_max_n_cap():
    # 130 completed sessions but max_n=5 -> only the last 5 pairs are evaluated
    sessions = [{"h": 10 + i, "l": i, "c": 5 + i, "ts": i} for i in range(132)]
    s = ind.level_stats(sessions, 10.0, max_n=5)
    assert s["sessions_evaluated"] == 5


# ===================================================================== indicators: swings
def test_swings_detects_fractal_high_and_low():
    seq = [(1, 0), (2, 1), (5, 4), (2, 1), (1, 0), (3, 2), (6, 5), (3, 2), (2, 1)]
    rows = [{"h": h, "l": l, "ts": i} for i, (h, l) in enumerate(seq)]
    hi, lo = ind.swings(rows, k=2)
    assert {"t": 2, "p": 5} in hi and {"t": 6, "p": 6} in hi   # local-max highs
    assert {"t": 4, "p": 0} in lo                              # local-min low


def test_swings_too_few_rows_returns_empty():
    rows = [{"h": 1, "l": 0, "ts": i} for i in range(3)]
    assert ind.swings(rows, k=2) == ([], [])


def test_swings_caps_to_last_five():
    # a long monotonic ramp produces many confirmed highs; only the final 5 are returned
    rows = [{"h": float(i), "l": float(i) - 0.5, "ts": i} for i in range(40)]
    hi, _ = ind.swings(rows, k=2)
    assert len(hi) <= 5


# ===================================================================== indicators: pivots/bands edge
def test_compute_pivots_bands_third_level_and_anchor():
    piv, bands = ind.compute_pivots_bands({"h": 100, "l": 90, "c": 95}, 96, 4.0)
    assert piv["R3"] == 100 + 2 * (95 - 90)            # h + 2*(PP-l) = 110
    assert piv["S3"] == 90 - 2 * (100 - 95)            # l - 2*(h-PP) = 80
    assert bands["open"] == 96


def test_compute_pivots_bands_guards():
    assert ind.compute_pivots_bands(None, 96, 4.0)[0] is None      # no prior -> no pivots
    assert ind.compute_pivots_bands({"h": 1, "l": 0, "c": 0.5}, None, 4.0)[1] is None  # no anchor close
    assert ind.compute_pivots_bands({"h": 1, "l": 0, "c": 0.5}, 1, 0)[1] is None       # ATR 0 -> no bands


# ===================================================================== data_providers: range parsing
def test_range_to_timedelta_units_and_special():
    assert DP.range_to_timedelta("max") == timedelta(days=7300)
    assert DP.range_to_timedelta("1mo") == timedelta(days=31)
    assert DP.range_to_timedelta("6mo") == timedelta(days=186)
    assert DP.range_to_timedelta("10d") == timedelta(days=10)
    assert DP.range_to_timedelta("2w") == timedelta(days=14)
    assert DP.range_to_timedelta("1y") == timedelta(days=366)


def test_range_to_timedelta_is_case_and_space_insensitive():
    assert DP.range_to_timedelta("  5Y ") == timedelta(days=5 * 366)
    assert DP.range_to_timedelta("MAX") == timedelta(days=7300)


# ===================================================================== data_providers: eodhd mapping
def test_map_symbol_eodhd_uncovered_futures():
    assert DP.map_symbol_eodhd("GC=F") == (None, "futures")


def test_map_symbol_eodhd_forex_three_and_six_char():
    assert DP.map_symbol_eodhd("JPY=X") == ("USDJPY.FOREX", "forex")    # 3-char -> USD base prefix
    assert DP.map_symbol_eodhd("GBPUSD=X") == ("GBPUSD.FOREX", "forex")  # 6-char passthrough


def test_map_symbol_eodhd_index_and_crypto():
    assert DP.map_symbol_eodhd("^FTSE") == ("FTSE.INDX", "index")
    assert DP.map_symbol_eodhd("BTC-USD") == ("BTC-USD.CC", "crypto")


def test_map_symbol_eodhd_equity_exchange_suffix_and_plain():
    assert DP.map_symbol_eodhd("BP.L") == ("BP.LSE", "equity")          # .L -> .LSE
    assert DP.map_symbol_eodhd("AAPL") == ("AAPL.US", "equity")         # bare -> .US
    # a dashed non-crypto ticker (quote not in CRYPTO_QUOTES) falls through to .US, never .CC
    assert DP.map_symbol_eodhd("BRK-B") == ("BRK-B.US", "equity")


def test_map_symbol_eodhd_unknown_suffix_passes_through():
    assert DP.map_symbol_eodhd("ABC.ZZ") == ("ABC.ZZ", "equity")        # unknown dot-suffix kept as-is


# ===================================================================== data_providers: eodhd_chart
def test_eodhd_chart_daily_parses_and_skips_null_close(monkeypatch):
    raw = [
        {"date": "2026-06-10", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100},
        {"date": "2026-06-11", "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 200},
        {"date": "2026-06-12", "open": 2.0, "high": 2.2, "low": 1.8, "close": None, "volume": 0},
    ]
    monkeypatch.setattr(DP, "_http_json", lambda *a, **k: raw)
    meta, rows = DP.eodhd_chart("AAPL", "1d", "1mo", "key")
    assert len(rows) == 2                                # the null-close bar is dropped
    assert rows[0]["ts"] < rows[1]["ts"]                 # ascending UTC
    assert meta["instrumentType"] == "EQUITY"
    assert meta["regularMarketPrice"] == 2.0             # last good close


def test_eodhd_chart_intraday_accepts_timestamp_and_datetime(monkeypatch):
    raw = [
        {"datetime": "2026-06-10 14:00:00", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5},
        {"timestamp": 1718031600, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0, "volume": 9},
    ]
    monkeypatch.setattr(DP, "_http_json", lambda *a, **k: raw)
    _, rows = DP.eodhd_chart("AAPL", "60m", "10d", "key")
    assert len(rows) == 2
    assert all(isinstance(r["ts"], int) for r in rows)


def test_eodhd_chart_non_list_payload_raises(monkeypatch):
    monkeypatch.setattr(DP, "_http_json", lambda *a, **k: {"error": "bad"})
    try:
        DP.eodhd_chart("AAPL", "1d", "1mo", "key")
    except ValueError as ex:
        assert "unexpected eodhd response" in str(ex)
    else:
        raise AssertionError("expected ValueError for a non-list payload")


def test_eodhd_chart_uncovered_symbol_raises_before_network():
    # no _http_json patch -> must raise from the mapping, never reaching the network
    try:
        DP.eodhd_chart("ES=F", "1d", "1mo", "key")
    except ValueError as ex:
        assert "does not cover" in str(ex)
    else:
        raise AssertionError("expected ValueError for an uncovered symbol")


# ===================================================================== intraday: freshness_block
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)   # a Monday, mid-day


def _row_aged(age):
    last = NOW - age
    return [{"ts": int(last.timestamp()), "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]


def test_freshness_crypto_stale_after_three_hours():
    f = I.freshness_block({"instrumentType": "CRYPTOCURRENCY"}, _row_aged(timedelta(hours=4)), now=NOW)
    assert f["market_state"] == "open" and f["stale"] is True


def test_freshness_crypto_fresh_within_three_hours():
    f = I.freshness_block({"instrumentType": "CRYPTOCURRENCY"}, _row_aged(timedelta(hours=1)), now=NOW)
    assert f["stale"] is False


def test_freshness_fx_open_midweek_uses_three_hour_rule():
    f = I.freshness_block({"instrumentType": "CURRENCY"}, _row_aged(timedelta(hours=4)), now=NOW)
    assert f["market_state"] == "open" and f["stale"] is True


def test_freshness_fx_weekend_is_lenient_until_friday_close():
    sat = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    # last bar at Fri 21:30 (before the Fri 22:00 close) -> NOT stale despite being a day old
    fresh_last = [{"ts": int(datetime(2026, 6, 12, 21, 30, tzinfo=timezone.utc).timestamp()),
                   "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]
    f = I.freshness_block({"instrumentType": "CURRENCY"}, fresh_last, now=sat)
    assert f["market_state"] == "closed_weekend" and f["stale"] is False
    # a bar predating the Friday close by >3h IS stale
    old_last = [{"ts": int(datetime(2026, 6, 12, 17, 0, tzinfo=timezone.utc).timestamp()),
                 "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]
    assert I.freshness_block({"instrumentType": "CURRENCY"}, old_last, now=sat)["stale"] is True


def test_freshness_equity_in_session_via_trading_period():
    start = int((NOW - timedelta(hours=1)).timestamp())
    end = int((NOW + timedelta(hours=3)).timestamp())
    meta = {"instrumentType": "EQUITY", "currentTradingPeriod": {"regular": {"start": start, "end": end}}}
    fresh = I.freshness_block(meta, _row_aged(timedelta(minutes=30)), now=NOW)
    assert fresh["market_state"] == "open" and fresh["stale"] is False
    stale = I.freshness_block(meta, _row_aged(timedelta(hours=2)), now=NOW)   # >90 min in-session
    assert stale["stale"] is True


def test_freshness_equity_offhours_uses_96h_rule():
    # trading period ended hours ago -> off-hours; 5h-old bar is NOT stale (only >96h is)
    start = int((NOW - timedelta(hours=8)).timestamp())
    end = int((NOW - timedelta(hours=2)).timestamp())
    meta = {"instrumentType": "EQUITY", "currentTradingPeriod": {"regular": {"start": start, "end": end}}}
    f = I.freshness_block(meta, _row_aged(timedelta(hours=5)), now=NOW)
    assert f["market_state"] == "closed_offhours" and f["stale"] is False


def test_freshness_daily_granularity_ignores_intraday_age():
    # a 5h-old daily bar is fine: daily stamps at session open, so only the 96h dead-feed rule applies
    f = I.freshness_block({"instrumentType": "EQUITY"}, _row_aged(timedelta(hours=5)),
                          now=NOW, granularity="daily")
    assert f["stale"] is False and f["bar_granularity"] == "daily"


def test_freshness_unknown_type_dead_feed_after_96h():
    f = I.freshness_block({"instrumentType": "WIDGET"}, _row_aged(timedelta(hours=200)), now=NOW)
    assert f["market_state"] == "unknown" and f["stale"] is True
    assert f["stale_reason"] is not None


def test_freshness_missing_meta_defaults_unknown():
    f = I.freshness_block(None, _row_aged(timedelta(minutes=10)), now=NOW)
    assert f["instrument_type"] == "UNKNOWN" and f["stale"] is False


# ===================================================================== intraday: interval_block
def test_interval_block_empty_is_metadata_safe():
    blk = I.interval_block("4h", [])
    assert blk["bars"] == 0 and blk["last_close"] is None
    assert blk["trend"] == "Insufficient data"
    assert blk["sma20"] is None and blk["rsi14"] is None


def test_interval_block_short_series_reports_insufficient_trend():
    rows = [{"ts": i, "o": 1, "h": 2, "l": 0.5, "c": 1.0 + i, "v": 1} for i in range(10)]
    blk = I.interval_block("4h", rows)
    assert blk["bars"] == 10 and blk["last_close"] == 10.0
    assert blk["trend"] == "Insufficient data"          # <50 closes -> no regime call


def test_interval_block_long_rising_series_is_uptrend():
    rows = [{"ts": i, "o": 1, "h": 2, "l": 0.5, "c": float(i), "v": 1} for i in range(220)]
    blk = I.interval_block("1d", rows)
    assert blk["trend"] == "Uptrend"                    # SMA200 warmed + rising
    assert blk["sma20"] is not None and blk["rsi14"] == 100.0


# ===================================================================== market_context: helpers
def _patch_fetch(monkeypatch, fn):
    monkeypatch.setattr(MC.intraday, "fetch_chart", fn)


def test_daily_change_computes_percent_move(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: ({"provider": "x"}, [{"ts": 1, "c": 100.0},
                                                                   {"ts": 2, "c": 110.0}]))
    d = MC._daily_change("ES=F")
    assert d == {"last": 110.0, "prev_close": 100.0, "change_pct": 10.0}


def test_daily_change_none_when_fewer_than_two_closes(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: ({"provider": "x"}, [{"ts": 1, "c": 5.0}]))
    assert MC._daily_change("ES=F") is None


def test_daily_change_swallows_fetch_errors(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("feed down")))
    assert MC._daily_change("ES=F") is None


def test_daily_change_guards_zero_prior_close(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: ({"provider": "x"}, [{"ts": 1, "c": 0.0},
                                                                   {"ts": 2, "c": 5.0}]))
    assert MC._daily_change("ES=F")["change_pct"] == 0.0   # no ZeroDivisionError


def test_daily_change_drops_none_closes(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: ({"provider": "x"},
                                               [{"ts": 1, "c": None}, {"ts": 2, "c": 4.0},
                                                {"ts": 3, "c": 6.0}]))
    d = MC._daily_change("ES=F")
    assert d["prev_close"] == 4.0 and d["last"] == 6.0    # None close filtered out


def test_risk_tone_empty_is_neutral():
    assert MC._risk_tone({}) == "mixed/neutral"


def test_risk_tone_below_threshold_is_neutral():
    # +0.1% futures is under the 0.15 vote threshold -> no vote -> neutral
    assert MC._risk_tone({"S&P 500 futures": {"change_pct": 0.1}}) == "mixed/neutral"


def test_risk_tone_risk_on_and_off():
    assert MC._risk_tone({"S&P 500 futures": {"change_pct": 0.5}}) == "risk-on"
    assert MC._risk_tone({"VIX (volatility)": {"change_pct": 3.0}}) == "risk-off"   # vol spike


def test_risk_tone_falling_vix_votes_risk_on():
    assert MC._risk_tone({"VIX (volatility)": {"change_pct": -3.0}}) == "risk-on"


# ===================================================================== market_context: build pack
def test_build_market_weather_is_deterministic_with_now(monkeypatch):
    fixed = datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc)

    def fetch(symbol, interval, rng, **kw):
        moves = {"ES=F": (100, 101.0), "DX-Y.NYB": (100, 100.3),
                 "^TNX": (42, 42.5), "CL=F": (100, 99.0)}
        if symbol in moves:
            prev, last = moves[symbol]
            return ({"provider": "x"}, [{"ts": 1, "c": prev}, {"ts": 2, "c": last}])
        raise RuntimeError("no data")

    _patch_fetch(monkeypatch, fetch)
    w = MC.build_market_weather(now=fixed)
    assert w["as_of_utc"] == "2026-06-15 09:30"          # the provided clock, not wall-clock
    assert "US Dollar Index" in w["macro_drivers"]        # macro group routed correctly
    assert "WTI crude" in w["macro_drivers"]
    assert "S&P 500 futures" in w["overnight_recap"]      # risk group lands in the overnight recap
    assert "note" in w and isinstance(w["risk_tone"], str)


def test_build_market_weather_omits_all_failed_fetches(monkeypatch):
    _patch_fetch(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    w = MC.build_market_weather(now=datetime(2026, 6, 15, 9, 30, tzinfo=timezone.utc))
    assert w["overnight_recap"] == {} and w["macro_drivers"] == {}   # nothing resolved, never raises


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
