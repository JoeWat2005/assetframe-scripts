"""Tests for the multi-source data fallback chain added to intraday.py.

Offline: every network call is monkeypatched, so this never touches a real feed. Covers
  - CoinGecko symbol mapping + OHLC->daily resampling
  - Yahoo host failover (query1 -> query2)
  - fetch_chart provider chain: yahoo -> coingecko (crypto daily), raise for non-crypto daily,
    and the preserved degrade path (a failed HOURLY re-raises so daily-only kicks in).

Run:  python scripts/test_data_fallback.py
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import intraday as I


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
