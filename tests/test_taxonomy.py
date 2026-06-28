"""Tests for taxonomy.py — validators reject typos, helpers map correctly, and the
confidence band/bucket boundaries are exact. Pure stdlib unittest.

Run:  python -m pytest tests/test_taxonomy.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import taxonomy as T


class TestValidatorsRejectTypos(unittest.TestCase):
    def test_prediction_type(self):
        self.assertEqual(T.validate_prediction_type("breakout"), "breakout")
        for bad in ("breakouts", "Breakout", "momentum", ""):
            with self.assertRaises(T.TaxonomyError):
                T.validate_prediction_type(bad)

    def test_direction(self):
        self.assertEqual(T.validate_direction("bullish"), "bullish")
        for bad in ("bull", "long", "up", "BULLISH"):
            with self.assertRaises(T.TaxonomyError):
                T.validate_direction(bad)

    def test_setup_side(self):
        self.assertEqual(T.validate_setup_side("wait"), "wait")
        for bad in ("buy", "sell", "neutral", "hold"):
            with self.assertRaises(T.TaxonomyError):
                T.validate_setup_side(bad)

    def test_horizon(self):
        self.assertEqual(T.validate_horizon("next_session"), "next_session")
        for bad in ("tomorrow", "daily", "next session"):
            with self.assertRaises(T.TaxonomyError):
                T.validate_horizon(bad)

    def test_asset_class(self):
        self.assertEqual(T.validate_asset_class("equity"), "equity")
        for bad in ("stocks", "stock", "forex", "coins"):
            with self.assertRaises(T.TaxonomyError):
                T.validate_asset_class(bad)

    def test_market_regime(self):
        self.assertEqual(T.validate_market_regime("trend_up"), "trend_up")
        for bad in ("uptrend", "bull", "ranging"):
            with self.assertRaises(T.TaxonomyError):
                T.validate_market_regime(bad)

    def test_taxonomy_error_is_valueerror(self):
        # callers may catch ValueError; TaxonomyError must remain a subclass.
        self.assertTrue(issubclass(T.TaxonomyError, ValueError))


class TestAssetClassKey(unittest.TestCase):
    def test_profile_mapping(self):
        self.assertEqual(T.asset_class_key("us_equity_rth", "AAPL"), "equity")
        self.assertEqual(T.asset_class_key("crypto_24_7", "BTC-USD"), "crypto")
        self.assertEqual(T.asset_class_key("fx_spot", "EURUSD=X"), "fx")

    def test_futures_refinement(self):
        self.assertEqual(T.asset_class_key("cme_futures", "ES=F"), "index")
        self.assertEqual(T.asset_class_key("cme_futures", "NQ=F"), "index")
        self.assertEqual(T.asset_class_key("cme_futures", "CL=F"), "commodity")
        self.assertEqual(T.asset_class_key("cme_futures", "GC=F"), "commodity")

    def test_futures_unknown_root_stays_futures(self):
        self.assertEqual(T.asset_class_key("cme_futures", "ZZZ=F"), "futures")
        # no symbol => cannot refine
        self.assertEqual(T.asset_class_key("cme_futures", ""), "futures")

    def test_override_wins_and_is_validated(self):
        self.assertEqual(T.asset_class_key("cme_futures", "ES=F", override="commodity"),
                         "commodity")
        with self.assertRaises(T.TaxonomyError):
            T.asset_class_key("cme_futures", "ES=F", override="bogus")

    def test_unknown_profile_raises(self):
        with self.assertRaises(T.TaxonomyError):
            T.asset_class_key("nyse_floor", "AAPL")


class TestRegimeDerivation(unittest.TestCase):
    def test_derive_range(self):
        a = {"trend": {"alignment": "mixed (intraday range)"}}
        self.assertEqual(T.derive_market_regime(a), "range")

    def test_derive_trend_up(self):
        a = {"trend": {"long_term_daily": "Uptrend", "alignment": "aligned up"}}
        self.assertEqual(T.derive_market_regime(a), "trend_up")

    def test_derive_trend_up_blocked_by_mixed(self):
        a = {"trend": {"long_term_daily": "Uptrend", "alignment": "mixed"}}
        # mixed alignment must NOT be read as a clean trend
        self.assertEqual(T.derive_market_regime(a), "choppy")

    def test_derive_default_choppy(self):
        self.assertEqual(T.derive_market_regime({}), "choppy")

    def test_normalize_alias(self):
        a = {"trend": {"long_term_daily": "Downtrend", "alignment": "aligned"}}
        # aliases are matched as substrings of the analyst's free text
        self.assertEqual(T.normalize_market_regime("consolidation phase", a), "range")
        self.assertEqual(T.normalize_market_regime("market looks calm", a), "low_volatility")

    def test_normalize_exact_label(self):
        self.assertEqual(T.normalize_market_regime("high_volatility", {}), "high_volatility")

    def test_normalize_unknown_falls_back_to_derived(self):
        a = {"trend": {"long_term_daily": "Uptrend", "alignment": "aligned"}}
        self.assertEqual(T.normalize_market_regime("gibberish-xyz", a), "trend_up")


class TestConfidenceBandBucket(unittest.TestCase):
    def test_band_boundaries(self):
        self.assertEqual(T.confidence_band(49.9), "Low")
        self.assertEqual(T.confidence_band(50), "Moderate")
        self.assertEqual(T.confidence_band(64.9), "Moderate")
        self.assertEqual(T.confidence_band(65), "Elevated")
        self.assertEqual(T.confidence_band(79.9), "Elevated")
        self.assertEqual(T.confidence_band(80), "High")

    def test_band_unparseable(self):
        self.assertEqual(T.confidence_band(None), "Unknown")
        self.assertEqual(T.confidence_band("xx"), "Unknown")

    def test_bucket_boundaries(self):
        self.assertEqual(T.confidence_bucket(60), "<=60")
        self.assertEqual(T.confidence_bucket(60.1), "61-75")
        self.assertEqual(T.confidence_bucket(75), "61-75")
        self.assertEqual(T.confidence_bucket(75.1), ">75")

    def test_bucket_unparseable_is_none(self):
        self.assertIsNone(T.confidence_bucket(None))
        self.assertIsNone(T.confidence_bucket("xx"))


class TestBuildTaxonomy(unittest.TestCase):
    def test_valid(self):
        out = T.build_taxonomy("range_hold", "neutral", "next_session", "equity", "range")
        self.assertEqual(out["prediction_type"], "range_hold")
        self.assertEqual(out["direction"], "neutral")
        self.assertEqual(out["asset_class"], "equity")

    def test_one_bad_field_raises(self):
        with self.assertRaises(T.TaxonomyError):
            T.build_taxonomy("range_hold", "neutral", "next_session", "equity", "uptrend")

    def test_calibration_buckets_constant_shape(self):
        # the bucket labels are a cross-module contract (web/lib/content.ts mirror).
        self.assertEqual(T.CONFIDENCE_BUCKETS, ("<=60", "61-75", ">75"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
