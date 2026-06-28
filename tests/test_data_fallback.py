"""Tests for the multi-source data fallback chain added to intraday.py.

Offline: every network call is monkeypatched, so this never touches a real feed. Covers
  - CoinGecko symbol mapping + OHLC->daily resampling
  - Yahoo host failover (query1 -> query2)
  - fetch_chart provider chain: yahoo -> coingecko (crypto daily), raise for non-crypto daily,
    and the preserved degrade path (a failed HOURLY re-raises so daily-only kicks in).

Run:  python -m pytest tests/test_data_fallback.py
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data_providers as I   # the fetch layer (monkeypatched here) lives in data_providers now
import intraday              # freshness_block stays in intraday


def setUpModule():
    # Disable the Twelve Data rate throttle during tests so adapter tests don't sleep between calls;
    # the throttle's own timing logic is covered with a mocked clock in TestTwelveDataThrottle.
    os.environ["TWELVEDATA_MIN_INTERVAL_S"] = "0"
    os.environ.pop("TWELVEDATA_RATE_PER_MIN", None)


def _ms(y, m, d, h=0):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


class TestCoinGeckoMapping(unittest.TestCase):
    def test_known_pairs(self):
        self.assertEqual(I.map_symbol_coingecko("BTC-USD"), "bitcoin")
        self.assertEqual(I.map_symbol_coingecko("ETH-USD"), "ethereum")

    def test_unmapped_returns_none(self):
        self.assertIsNone(I.map_symbol_coingecko("AAPL"))
        self.assertIsNone(I.map_symbol_coingecko("GBPUSD=X"))


class TestCoinGeckoResample(unittest.TestCase):
    def test_intraday_candles_collapse_to_daily_ohlc(self):
        # two UTC days, two 12h candles each -> one daily bar per day (o=first,h=max,l=min,c=last)
        raw = [
            [_ms(2026, 6, 1, 0), 100, 110, 95, 105],
            [_ms(2026, 6, 1, 12), 105, 120, 104, 118],
            [_ms(2026, 6, 2, 0), 118, 119, 90, 92],
            [_ms(2026, 6, 2, 12), 92, 130, 91, 125],
        ]
        orig = I._http_json
        I._http_json = lambda *a, **k: raw
        try:
            meta, rows = I.coingecko_chart("BTC-USD", "3mo")
        finally:
            I._http_json = orig
        self.assertEqual(len(rows), 2)
        d1, d2 = rows
        self.assertEqual((d1["o"], d1["h"], d1["l"], d1["c"]), (100, 120, 95, 118))
        self.assertEqual((d2["o"], d2["h"], d2["l"], d2["c"]), (118, 130, 90, 125))
        self.assertEqual(meta["instrumentType"], "CRYPTOCURRENCY")
        self.assertEqual(meta["regularMarketPrice"], 125)

    def test_bad_response_raises(self):
        orig = I._http_json
        I._http_json = lambda *a, **k: {"error": "boom"}
        try:
            self.assertRaises(ValueError, I.coingecko_chart, "BTC-USD", "1mo")
        finally:
            I._http_json = orig


class TestYahooHostFailover(unittest.TestCase):
    def _payload(self, closes):
        ts = [int(datetime(2026, 6, i + 1, tzinfo=timezone.utc).timestamp())
              for i in range(len(closes))]
        return {"chart": {"result": [{"meta": {"instrumentType": "CURRENCY"},
                 "timestamp": ts,
                 "indicators": {"quote": [{"open": closes, "high": closes,
                                           "low": closes, "close": closes,
                                           "volume": [0] * len(closes)}]}}]}}

    def test_query1_down_query2_serves(self):
        calls = []

        def fake(url, *a, **k):
            calls.append(url)
            if "query1" in url:
                raise RuntimeError("query1 down")
            return self._payload([1.1, 1.2, 1.3])

        orig = I._http_json
        I._http_json = fake
        try:
            meta, rows = I.yahoo_chart("GBPUSD=X", "1d", "1mo")
        finally:
            I._http_json = orig
        self.assertEqual(len(rows), 3)
        self.assertTrue(any("query1" in u for u in calls) and any("query2" in u for u in calls))

    def test_both_hosts_down_raises(self):
        orig = I._http_json
        I._http_json = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            self.assertRaises(Exception, I.yahoo_chart, "GBPUSD=X", "1d", "1mo")
        finally:
            I._http_json = orig


class TestFetchChartChain(unittest.TestCase):
    def setUp(self):
        self._yc = I.yahoo_chart
        self._cg = I.coingecko_chart

    def tearDown(self):
        I.yahoo_chart, I.coingecko_chart = self._yc, self._cg

    def test_crypto_daily_falls_through_to_coingecko(self):
        I.yahoo_chart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yahoo out"))
        I.coingecko_chart = lambda *a, **k: ({"instrumentType": "CRYPTOCURRENCY"},
                                             [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("BTC-USD", "1d", "3mo", provider="yahoo")
        self.assertEqual(meta["provider"], "coingecko")
        self.assertIn("served by coingecko", meta["provider_note"])

    def test_non_crypto_daily_raises_when_yahoo_down(self):
        I.yahoo_chart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yahoo out"))
        self.assertRaises(RuntimeError, I.fetch_chart, "AAPL", "1d", "1mo", provider="yahoo")

    def test_hourly_failure_reraises_to_preserve_degrade(self):
        # a failed HOURLY fetch must propagate so intraday.main can degrade to daily-only,
        # NOT be swallowed into the coingecko/daily path.
        I.yahoo_chart = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("yahoo hourly out"))
        self.assertRaises(RuntimeError, I.fetch_chart, "BTC-USD", "60m", "10d", provider="yahoo")

    def test_yahoo_success_returns_directly(self):
        I.yahoo_chart = lambda *a, **k: ({"instrumentType": "CURRENCY"},
                                         [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("GBPUSD=X", "1d", "1mo", provider="yahoo")
        self.assertEqual(meta["provider"], "yahoo")
        self.assertEqual(len(rows), 1)


class TestTwelveDataMapping(unittest.TestCase):
    def test_each_asset_class(self):
        self.assertEqual(I.map_symbol_twelvedata("AAPL"), ("AAPL", "equity"))
        self.assertEqual(I.map_symbol_twelvedata("BTC-USD"), ("BTC/USD", "crypto"))
        self.assertEqual(I.map_symbol_twelvedata("GBPUSD=X"), ("GBP/USD", "forex"))
        self.assertEqual(I.map_symbol_twelvedata("XAUUSD=X"), ("XAU/USD", "forex"))
        self.assertEqual(I.map_symbol_twelvedata("JPY=X"), ("USD/JPY", "forex"))

    def test_uncovered_left_to_yahoo(self):
        # futures, indices, exchange-suffixed and already-slashed symbols return None -> served via Yahoo
        for s in ("GC=F", "ES=F", "^VIX", "DX-Y.NYB", "BP.L", "XAU/USD", "BTC/USD"):
            self.assertIsNone(I.map_symbol_twelvedata(s)[0], s)


class TestTwelveDataChart(unittest.TestCase):
    def _patch(self, payload):
        self._orig = I._http_json
        I._http_json = lambda *a, **k: payload

    def tearDown(self):
        if hasattr(self, "_orig"):
            I._http_json = self._orig

    def test_daily_equity_with_volume(self):
        self._patch({"meta": {"exchange_timezone": "America/New_York", "type": "Common Stock"},
                     "values": [{"datetime": "2026-06-17", "open": "300.85", "high": "302.07",
                                 "low": "294.36", "close": "295.95", "volume": "42745100"},
                                {"datetime": "2026-06-18", "open": "298.11", "high": "300.57",
                                 "low": "295.62", "close": "298.01", "volume": "85962200"}],
                     "status": "ok"})
        meta, rows = I.twelvedata_chart("AAPL", "1d", "1mo", "k")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[0]["ts"] < rows[1]["ts"])           # ascending
        self.assertEqual(rows[0]["c"], 295.95)                   # strings -> float
        self.assertEqual(rows[1]["v"], 85962200.0)
        self.assertEqual(meta["instrumentType"], "EQUITY")
        self.assertEqual(meta["regularMarketPrice"], 298.01)

    def test_forex_has_no_volume_and_maps_to_currency(self):
        self._patch({"meta": {"type": "Physical Currency"},
                     "values": [{"datetime": "2026-06-21", "open": "1.323", "high": "1.324",
                                 "low": "1.317", "close": "1.320"}],
                     "status": "ok"})
        meta, rows = I.twelvedata_chart("GBPUSD=X", "1d", "1mo", "k")
        self.assertEqual(rows[0]["v"], 0)                        # volume absent -> 0
        self.assertEqual(meta["instrumentType"], "CURRENCY")

    def test_precious_metal_is_currency_24_5(self):
        self._patch({"meta": {"type": "Precious Metal"},
                     "values": [{"datetime": "2026-06-22", "open": "4156", "high": "4216",
                                 "low": "4135", "close": "4187"}], "status": "ok"})
        meta, _ = I.twelvedata_chart("XAUUSD=X", "1d", "1mo", "k")
        self.assertEqual(meta["instrumentType"], "CURRENCY")     # spot gold uses the FX 24/5 staleness rule

    def test_descending_input_is_sorted_ascending(self):
        self._patch({"meta": {"type": "Digital Currency"},
                     "values": [{"datetime": "2026-06-22", "open": "2", "high": "2", "low": "2", "close": "2"},
                                {"datetime": "2026-06-20", "open": "1", "high": "1", "low": "1", "close": "1"}],
                     "status": "ok"})
        _, rows = I.twelvedata_chart("BTC-USD", "1d", "1mo", "k")
        self.assertTrue(rows[0]["ts"] < rows[1]["ts"])
        self.assertEqual(rows[-1]["c"], 2)

    def test_intraday_datetime_parsed(self):
        self._patch({"meta": {"type": "Common Stock"},
                     "values": [{"datetime": "2026-06-22 16:30:00", "open": "300", "high": "301",
                                 "low": "299", "close": "300.5", "volume": "666556"}], "status": "ok"})
        _, rows = I.twelvedata_chart("AAPL", "60m", "10d", "k")
        self.assertEqual(len(rows), 1)
        self.assertEqual(datetime.fromtimestamp(rows[0]["ts"], tz=timezone.utc).hour, 16)

    def test_error_status_raises(self):
        self._patch({"status": "error", "code": 429, "message": "You have run out of API credits"})
        self.assertRaises(ValueError, I.twelvedata_chart, "AAPL", "1d", "1mo", "k")

    def test_uncovered_symbol_raises_without_fetch(self):
        # no monkeypatch: must raise from the mapping, never hitting the network
        self.assertRaises(ValueError, I.twelvedata_chart, "GC=F", "1d", "1mo", "k")

    def test_explicit_td_symbol_bypasses_uncovered_check(self):
        # gold: GC=F maps to None, but an explicit td_symbol (XAU/USD spot) routes it to TD directly
        self._patch({"meta": {"type": "Precious Metal"},
                     "values": [{"datetime": "2026-06-22", "open": "4100", "high": "4200",
                                 "low": "4090", "close": "4187"}], "status": "ok"})
        meta, rows = I.twelvedata_chart("GC=F", "1d", "1mo", "k", td_symbol="XAU/USD")
        self.assertEqual(len(rows), 1)
        self.assertEqual(meta["instrumentType"], "CURRENCY")


class TestFetchChartTwelveData(unittest.TestCase):
    def setUp(self):
        self._yc, self._td = I.yahoo_chart, I.twelvedata_chart

    def tearDown(self):
        I.yahoo_chart, I.twelvedata_chart = self._yc, self._td

    def test_covered_symbol_served_by_twelvedata(self):
        I.twelvedata_chart = lambda *a, **k: ({"instrumentType": "EQUITY"},
                                              [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("AAPL", "1d", "1mo", provider="twelvedata", api_key="k")
        self.assertEqual(meta["provider"], "twelvedata")
        self.assertIsNone(meta["provider_note"])

    def test_uncovered_futures_falls_back_to_yahoo(self):
        I.yahoo_chart = lambda *a, **k: ({"instrumentType": "FUTURE"},
                                         [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("GC=F", "1d", "1mo", provider="twelvedata", api_key="k")
        self.assertEqual(meta["provider"], "yahoo")
        self.assertIn("not covered by twelvedata", meta["provider_note"])

    def test_twelvedata_error_falls_back_to_yahoo(self):
        I.twelvedata_chart = lambda *a, **k: (_ for _ in ()).throw(ValueError("out of credits"))
        I.yahoo_chart = lambda *a, **k: ({"instrumentType": "EQUITY"},
                                         [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("AAPL", "1d", "1mo", provider="twelvedata", api_key="k")
        self.assertEqual(meta["provider"], "yahoo")
        self.assertIn("twelvedata failed", meta["provider_note"])

    def test_missing_key_falls_back_to_yahoo(self):
        I.yahoo_chart = lambda *a, **k: ({"instrumentType": "EQUITY"},
                                         [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        meta, rows = I.fetch_chart("AAPL", "1d", "1mo", provider="twelvedata", api_key=None)
        self.assertEqual(meta["provider"], "yahoo")
        self.assertIn("TWELVEDATA_API_KEY not set", meta["provider_note"])

    def test_explicit_td_symbol_routes_uncovered_to_twelvedata(self):
        # gold GC=F maps to None normally; an explicit td_symbol sends it to TD (spot) anyway
        captured = {}
        def fake(symbol, interval, rng, api_key, td_symbol=None):
            captured["td"] = td_symbol
            return ({"instrumentType": "CURRENCY"}, [{"ts": 1, "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}])
        I.twelvedata_chart = fake
        meta, rows = I.fetch_chart("GC=F", "1d", "1mo", provider="twelvedata", api_key="k", td_symbol="XAU/USD")
        self.assertEqual(meta["provider"], "twelvedata")
        self.assertEqual(captured["td"], "XAU/USD")


class TestTwelveDataThrottle(unittest.TestCase):
    def test_spaces_calls_by_interval(self):
        # mocked monotonic clock: 1st call sets the baseline (no sleep), 2nd call 1s later must sleep
        # the remaining 4s of a 5s interval. time.sleep is captured, never actually slept (non-flaky).
        prev = os.environ.get("TWELVEDATA_MIN_INTERVAL_S")
        os.environ["TWELVEDATA_MIN_INTERVAL_S"] = "5"
        I._TD_STATE["last"] = 0.0
        sleeps = []
        orig_sleep, orig_mono = I.time.sleep, I.time.monotonic
        clock = iter([100.0, 100.0, 101.0, 101.0])
        I.time.sleep = lambda s: sleeps.append(s)
        I.time.monotonic = lambda: next(clock)
        try:
            I._td_throttle()    # last=0 -> wait=5-(100-0)<0 -> no sleep; last=100
            I._td_throttle()    # wait=5-(101-100)=4 -> sleep 4; last=101
        finally:
            I.time.sleep, I.time.monotonic = orig_sleep, orig_mono
            if prev is None:
                os.environ.pop("TWELVEDATA_MIN_INTERVAL_S", None)
            else:
                os.environ["TWELVEDATA_MIN_INTERVAL_S"] = prev
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 4.0, places=6)

    def test_disabled_when_zero(self):
        os.environ["TWELVEDATA_MIN_INTERVAL_S"] = "0"
        I._TD_STATE["last"] = 0.0
        slept = []
        orig = I.time.sleep
        I.time.sleep = lambda s: slept.append(s)
        try:
            I._td_throttle()
            I._td_throttle()
        finally:
            I.time.sleep = orig
        self.assertEqual(slept, [])

    def test_rate_per_min_derives_interval(self):
        prev = (os.environ.get("TWELVEDATA_RATE_PER_MIN"), os.environ.get("TWELVEDATA_MIN_INTERVAL_S"))
        try:
            os.environ.pop("TWELVEDATA_MIN_INTERVAL_S", None)
            os.environ["TWELVEDATA_RATE_PER_MIN"] = "55"        # Grow: 55/min -> ~1.15s spacing
            self.assertAlmostEqual(I._td_interval(), 60.0 / 55 * 1.05, places=4)
            os.environ["TWELVEDATA_RATE_PER_MIN"] = "8"         # Basic
            self.assertAlmostEqual(I._td_interval(), 60.0 / 8 * 1.05, places=4)
            os.environ["TWELVEDATA_RATE_PER_MIN"] = "0"         # disabled
            self.assertEqual(I._td_interval(), 0.0)
        finally:
            for k, v in zip(("TWELVEDATA_RATE_PER_MIN", "TWELVEDATA_MIN_INTERVAL_S"), prev):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestFreshnessSessionProfile(unittest.TestCase):
    # 2026-06-15 is a Monday; 17:00 UTC = 13:00 ET = mid US regular session.
    NOW = datetime(2026, 6, 15, 17, 0, tzinfo=timezone.utc)

    def _rows(self, age):
        last = self.NOW - age
        return [{"ts": int(last.timestamp()), "o": 1, "h": 1, "l": 1, "c": 1, "v": 0}]

    def test_in_session_provider_equity_flagged_stale(self):
        # provider equity (no currentTradingPeriod) that's in-session and 3h stale -> stale=True
        meta = {"instrumentType": "EQUITY", "currentTradingPeriod": None}
        f = intraday.freshness_block(meta, self._rows(timedelta(hours=3)), now=self.NOW,
                              session_profile="us_equity_rth")
        self.assertEqual(f["market_state"], "open")
        self.assertTrue(f["stale"])

    def test_fresh_in_session_provider_equity_not_stale(self):
        meta = {"instrumentType": "EQUITY", "currentTradingPeriod": None}
        f = intraday.freshness_block(meta, self._rows(timedelta(minutes=30)), now=self.NOW,
                              session_profile="us_equity_rth")
        self.assertFalse(f["stale"])

    def test_without_profile_uses_lax_96h_rule(self):
        # no session_profile -> the prior lax behaviour: 3h-old in-session equity NOT flagged
        meta = {"instrumentType": "EQUITY", "currentTradingPeriod": None}
        f = intraday.freshness_block(meta, self._rows(timedelta(hours=3)), now=self.NOW)
        self.assertFalse(f["stale"])


class TestTwelveDataFundamentals(unittest.TestCase):
    def setUp(self):
        self._orig = I._td_get

    def tearDown(self):
        I._td_get = self._orig

    def test_compact_fundamentals_parsed(self):
        def fake(endpoint, sym, api_key, **kw):
            if endpoint == "statistics":
                return {"statistics": {
                    "valuations_metrics": {"trailing_pe": 36.1, "forward_pe": 31.0,
                                           "market_capitalization": 4359060534113, "peg_ratio": 1.6},
                    "financials": {"profit_margin": 0.27, "operating_margin": 0.32,
                                   "gross_margin": 0.49, "return_on_equity_ttm": 1.41}}}
            if endpoint == "profile":
                return {"sector": "Technology", "industry": "Consumer Electronics",
                        "employees": 166000, "description": "Apple Inc. designs ..."}
            if endpoint == "earnings":
                return {"earnings": [{"date": "2026-06-10", "eps_estimate": -0.77,
                                      "eps_actual": -1.36, "surprise_prc": 76.62}]}
            return {}
        I._td_get = fake
        f = I.twelvedata_fundamentals("AAPL", "k")
        self.assertEqual(f["source"], "twelvedata")
        self.assertEqual(f["valuation"]["trailing_pe"], 36.1)
        self.assertEqual(f["margins"]["profit_margin"], 0.27)
        self.assertEqual(f["profile"]["sector"], "Technology")
        self.assertEqual(f["latest_earnings"]["surprise_prc"], 76.62)

    def test_returns_none_when_nothing_usable(self):
        I._td_get = lambda *a, **k: {}
        self.assertIsNone(I.twelvedata_fundamentals("AAPL", "k"))

    def test_endpoint_error_captured_not_fatal(self):
        def fake(endpoint, sym, api_key, **kw):
            if endpoint == "statistics":
                raise ValueError("boom")
            return {"sector": "Tech"} if endpoint == "profile" else {}
        I._td_get = fake
        f = I.twelvedata_fundamentals("AAPL", "k")
        self.assertIn("statistics_error", f)
        self.assertEqual(f["profile"]["sector"], "Tech")


if __name__ == "__main__":
    unittest.main(verbosity=2)
