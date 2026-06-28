"""Tests for confidence.py — the deterministic confidence engine.

Covers: blend weights sum, every hard cap, social subtract-only (never raises),
calibration-map apply, compute_dq, determinism/reproducibility, and division guards.

Run:  python -m pytest tests/test_confidence.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import confidence as C


# A minimally-complete analysis fixture (warm, fresh, no errors) so caps don't fire
# unless a test deliberately injects the triggering condition.
def _clean_analysis():
    return {
        "degraded": None,
        "errors": None,
        "freshness": {"stale": False, "age_minutes": 30},
        "windows": {"sma_warm_at_display_start": {"h20": True, "h50": True,
                                                  "d50": True, "d200": True}},
        "trend": {"long_term_daily": "Uptrend", "alignment": "aligned",
                  "intraday_hourly": "uptrend"},
        "hourly": {"rsi14": 58, "macd": {"cross": "bullish", "hist": 0.5, "hist_prev": 0.3}},
        "daily": {"rsi14": 60, "atr14": 5.0, "realized_vol_20d_pct": 20},
        "stats_last_sessions": {"close_inside_inner_band_pct": 70,
                                "median_session_range": 5.0},
        "pivots_classic": {"PP": 100, "R1": 103, "S1": 97, "R2": 106, "S2": 94},
        "atr_day_bands": {"open": 100, "inner_hi": 102, "inner_lo": 98,
                          "outer_hi": 105, "outer_lo": 95},
    }


def _clean_setup():
    return {"direction": "long", "entry_lo": 97, "entry_hi": 98,
            "invalidation": 94, "t1": 103, "t2": 106}


class TestBlendWeights(unittest.TestCase):
    def test_top_level_weights_sum_to_100(self):
        self.assertEqual(sum(C.WEIGHTS.values()), 100)

    def test_market_subweights_sum_to_one(self):
        _, subs = C.market_confidence(_clean_analysis(), _clean_setup())
        # the internal market weighting must be a true weighted mean (weights sum 1)
        w = {"trend": 0.22, "momentum": 0.18, "structure": 0.20,
             "rr": 0.16, "volatility": 0.10, "data_quality": 0.14}
        self.assertAlmostEqual(sum(w.values()), 1.0, places=9)
        for k in w:
            self.assertIn(k, subs)

    def test_raw_is_weighted_blend_of_components(self):
        out = C.compute_confidence(_clean_analysis(), _clean_setup())
        expected = (C.WEIGHTS["market"] * out["market"]
                    + C.WEIGHTS["ledger"] * out["ledger"]
                    + C.WEIGHTS["catalyst"] * out["catalyst"]
                    + out["social_adj"])
        self.assertAlmostEqual(out["raw"], round(min(max(expected, 0), 100), 1), places=1)


class TestDeterminism(unittest.TestCase):
    def test_same_inputs_same_output(self):
        a, s = _clean_analysis(), _clean_setup()
        b = {"primary_prediction": {"type": "breakout"}, "claims": []}
        r1 = C.compute_confidence(a, s, b)
        r2 = C.compute_confidence(a, s, b)
        self.assertEqual(r1["published"], r2["published"])
        self.assertEqual(r1, r2)

    def test_published_is_int_0_100(self):
        out = C.compute_confidence(_clean_analysis(), _clean_setup())
        self.assertIsInstance(out["published"], int)
        self.assertGreaterEqual(out["published"], 0)
        self.assertLessEqual(out["published"], 100)


class TestComputeDQ(unittest.TestCase):
    def test_base_score(self):
        self.assertEqual(C.compute_dq({}), 7)

    def test_degraded_stale_errors_floor(self):
        a = {"degraded": "daily_only", "freshness": {"stale": True}, "errors": {"x": 1}}
        # 7 - 3 (degraded) - 2 (stale) - 2 (errors) = 0
        self.assertEqual(C.compute_dq(a), 0)

    def test_old_age_subtracts(self):
        a = {"freshness": {"age_minutes": 200}}
        self.assertEqual(C.compute_dq(a), 6)

    def test_cold_indicators_subtract(self):
        a = {"windows": {"sma_warm_at_display_start": {"h20": True, "d200": False}}}
        self.assertEqual(C.compute_dq(a), 6)

    def test_options_bonus_capped_at_10(self):
        a = _clean_analysis()
        self.assertLessEqual(C.compute_dq(a, options_included=True), 10)

    def test_unsupported_claims_subtract(self):
        claims = [{"status": "unverified"}, {"status": "stale"}]
        self.assertEqual(C.compute_dq({}, claims=claims), 6)

    def test_never_below_zero(self):
        a = {"degraded": True, "freshness": {"stale": True, "age_minutes": 999},
             "errors": {"x": 1}, "windows": {"sma_warm_at_display_start": {"h20": False}}}
        self.assertGreaterEqual(C.compute_dq(a), 0)


class TestSocialSubtractOnly(unittest.TestCase):
    def test_no_social_is_zero(self):
        self.assertEqual(C.social_adjustment(None)[0], 0.0)
        self.assertEqual(C.social_adjustment({})[0], 0.0)

    def test_never_positive(self):
        # any combination of risks must yield <= 0
        for hype in ("high", "medium", "low", ""):
            for crowd in ("high", "medium", "low", ""):
                for contra in (True, False):
                    adj, _ = C.social_adjustment(
                        {"aggregate": {"hype_risk": hype, "crowding_risk": crowd,
                                       "contrarian_warning": "x" if contra else ""}})
                    self.assertLessEqual(adj, 0.0)

    def test_floor_minus_10(self):
        adj, _ = C.social_adjustment(
            {"aggregate": {"hype_risk": "high", "crowding_risk": "high",
                           "contrarian_warning": "crowded"}})
        self.assertEqual(adj, -10.0)

    def test_social_can_only_lower_published(self):
        a, s = _clean_analysis(), _clean_setup()
        base = C.compute_confidence(a, s)
        sp = {"aggregate": {"hype_risk": "high", "crowding_risk": "high",
                            "contrarian_warning": "crowded"}}
        withsoc = C.compute_confidence(a, s, social_pack=sp)
        self.assertLessEqual(withsoc["published"], base["published"])


class TestHardCaps(unittest.TestCase):
    def _published(self, **over):
        a = _clean_analysis()
        a.update(over.pop("analysis", {}))
        return C.compute_confidence(a, _clean_setup(), **over)

    def test_stale_cap_40(self):
        out = self._published(analysis={"freshness": {"stale": True}})
        self.assertLessEqual(out["capped"], 40)
        self.assertIn("stale_data->40", out["caps_applied"])

    def test_degraded_cap_50(self):
        out = self._published(analysis={"degraded": "daily_only"})
        self.assertLessEqual(out["capped"], 50)
        self.assertIn("degraded_data->50", out["caps_applied"])

    def test_cold_indicators_cap_60(self):
        out = self._published(analysis={"windows": {"sma_warm_at_display_start":
                                                    {"h20": True, "d200": False}}})
        self.assertLessEqual(out["capped"], 60)
        self.assertIn("cold_indicators->60", out["caps_applied"])

    def test_engine_errors_cap_65(self):
        out = self._published(analysis={"errors": {"hourly": "boom"}})
        self.assertLessEqual(out["capped"], 65)
        self.assertIn("engine_errors->65", out["caps_applied"])

    def test_single_source_thesis_cap_65(self):
        # a single-source thesis claim is a yellow flag (65), NOT a near-neutral pin (55) —
        # this is the fix for confidence being "always 55" on technically-led calls.
        brief = {"primary_prediction": {"type": "breakout"},
                 "claims": [{"claim": "rumor", "status": "single-source",
                             "used_in_thesis": True, "source": "x"}]}
        out = C.compute_confidence(_clean_analysis(), _clean_setup(), brief=brief)
        self.assertLessEqual(out["capped"], 65)
        self.assertIn("single_source_thesis->65", out["caps_applied"])

    def test_unverified_thesis_cap_55_defence_in_depth(self):
        # unverified/stale claims must never drive a thesis (the brief validator blocks
        # them); if a malformed brief reaches here, hold the harder 55 floor.
        brief = {"primary_prediction": {"type": "breakout"},
                 "claims": [{"claim": "rumor", "status": "unverified",
                             "used_in_thesis": True, "source": "x"}]}
        out = C.compute_confidence(_clean_analysis(), _clean_setup(), brief=brief)
        self.assertLessEqual(out["capped"], 55)
        self.assertIn("single_source_thesis->55", out["caps_applied"])

    def test_hype_driven_thesis_cap_55(self):
        brief = {"primary_prediction": {"type": "breakout"},
                 "social_context": {"drives_thesis": True}, "claims": []}
        sp = {"aggregate": {"hype_risk": "high"}}
        out = C.compute_confidence(_clean_analysis(), _clean_setup(),
                                   brief=brief, social_pack=sp)
        self.assertLessEqual(out["capped"], 55)
        self.assertIn("hype_driven_thesis->55", out["caps_applied"])

    def test_ledger_failure_pattern_cap_55(self):
        brief = {"primary_prediction": {"type": "breakout"}, "claims": []}
        lc = {"prediction_type_hit_rates": {"breakout": 30},
              "prediction_type_counts": {"breakout": 8}}
        out = C.compute_confidence(_clean_analysis(), _clean_setup(),
                                   brief=brief, ledger_context=lc)
        self.assertLessEqual(out["capped"], 55)
        self.assertIn("ledger_failure_pattern->55", out["caps_applied"])

    def test_ledger_failure_needs_min_5(self):
        # below n=5, a low rate must NOT trigger the failure cap (too little data)
        self.assertFalse(C._ledger_failure(
            {"prediction_type_hit_rates": {"breakout": 10},
             "prediction_type_counts": {"breakout": 4}}, "breakout"))
        self.assertTrue(C._ledger_failure(
            {"prediction_type_hit_rates": {"breakout": 10},
             "prediction_type_counts": {"breakout": 5}}, "breakout"))

    def test_lowest_cap_wins(self):
        # stale (40) + degraded (50) + errors (65) -> min is 40
        a = _clean_analysis()
        a.update({"freshness": {"stale": True}, "degraded": True, "errors": {"x": 1}})
        out = C.compute_confidence(a, _clean_setup())
        self.assertLessEqual(out["capped"], 40)


class TestApplyCalibration(unittest.TestCase):
    def test_identity_passthrough_when_no_map(self):
        self.assertEqual(C._apply_calibration(42, None), 42)
        self.assertEqual(C._apply_calibration(42, {}), 42)

    def test_fewer_than_two_knots_is_identity(self):
        self.assertEqual(C._apply_calibration(42, {"knots": [[10, 20]]}), 42)

    def test_identity_map_returns_input(self):
        ident = {"knots": [[0.0, 0.0], [100.0, 100.0]]}
        for s in (0, 25.5, 50, 99.9, 100):
            self.assertAlmostEqual(C._apply_calibration(s, ident), s, places=6)

    def test_linear_interpolation(self):
        calib = {"knots": [[0.0, 0.0], [50.0, 60.0], [100.0, 100.0]]}
        # midpoint of first segment: x=25 -> y=30
        self.assertAlmostEqual(C._apply_calibration(25, calib), 30.0, places=6)
        # x=75 -> halfway between 60 and 100 -> 80
        self.assertAlmostEqual(C._apply_calibration(75, calib), 80.0, places=6)

    def test_clamps_below_first_and_above_last_knot(self):
        calib = {"knots": [[10.0, 20.0], [90.0, 80.0]]}
        self.assertEqual(C._apply_calibration(5, calib), 20.0)    # below first x
        self.assertEqual(C._apply_calibration(95, calib), 80.0)   # above last x

    def test_calibration_flag_reported(self):
        out_no = C.compute_confidence(_clean_analysis(), _clean_setup())
        self.assertFalse(out_no["calibrated"])
        calib = {"knots": [[0.0, 0.0], [100.0, 100.0]]}
        out_yes = C.compute_confidence(_clean_analysis(), _clean_setup(), calib=calib)
        self.assertTrue(out_yes["calibrated"])


class TestLedgerConfidence(unittest.TestCase):
    def test_no_context_neutral(self):
        score, _ = C.ledger_confidence(None)
        self.assertEqual(score, 0.5)
        score, _ = C.ledger_confidence({})
        self.assertEqual(score, 0.5)

    def test_shrinks_toward_half_with_low_n(self):
        # high rate but n=1 -> stays close to the 0.5 prior
        low_n, _ = C.ledger_confidence({"instrument_hit_rate": 100,
                                        "historical_prediction_count": 1})
        high_n, _ = C.ledger_confidence({"instrument_hit_rate": 100,
                                         "historical_prediction_count": 50})
        self.assertLess(low_n, high_n)
        self.assertGreater(high_n, 0.9)

    def test_percent_and_fraction_rates_equivalent(self):
        pct, _ = C.ledger_confidence({"instrument_hit_rate": 70,
                                      "historical_prediction_count": 10})
        frac, _ = C.ledger_confidence({"instrument_hit_rate": 0.70,
                                       "historical_prediction_count": 10})
        self.assertAlmostEqual(pct, frac, places=6)

    def test_instrument_drives_over_broad_asset_class(self):
        # the instrument's OWN strong record should dominate a large but weaker class
        # sample (learn per-stock): asset-class weight is capped, so it can't drown it.
        score, detail = C.ledger_confidence({
            "instrument_hit_rate": 80, "historical_prediction_count": 10,
            "asset_class_hit_rate": 50, "asset_class_count": 200})
        self.assertGreater(score, 0.7)   # ~instrument's 0.8, not pulled to the class 0.5
        cls = next(b for b in detail["blend"] if b["basis"] == "asset_class")
        self.assertEqual(cls["weight"], C.CLASS_PRIOR_MAX_W)


class TestDivisionGuards(unittest.TestCase):
    def test_rr_zero_risk(self):
        self.assertEqual(C._rr_score({"entry_lo": 100, "entry_hi": 100,
                                      "invalidation": 100, "t1": 105}), 0.5)

    def test_vol_zero_median(self):
        self.assertEqual(C._vol_score({"daily": {"atr14": 5},
                                       "stats_last_sessions": {"median_session_range": 0}}),
                         0.5)

    def test_vol_no_data(self):
        self.assertEqual(C._vol_score({}), 0.5)

    def test_momentum_no_side(self):
        self.assertEqual(C._momentum_score(_clean_analysis(), {"direction": "wait"}), 0.5)


class TestCatalystConfidence(unittest.TestCase):
    def test_no_brief_neutral(self):
        score, _ = C.catalyst_confidence(None)
        self.assertEqual(score, 0.5)

    def test_strong_claim_not_penalised_by_pack_mismatch(self):
        # a multiple-source thesis claim must never be downgraded for a fuzzy
        # string mismatch against the research pack (would paradoxically lower conf).
        brief = {"claims": [{"claim": "x", "status": "multiple-source",
                             "used_in_thesis": True, "source": "https://cnbc.com/a"}]}
        pack = {"items": [{"url": "https://reuters.com/z"}]}  # no overlap
        score, detail = C.catalyst_confidence(brief, research_pack=pack)
        self.assertGreaterEqual(detail["claim_support"], 1.0)

    def test_weak_untraced_thesis_claim_downgraded(self):
        brief = {"claims": [{"claim": "y", "status": "single-source",
                             "used_in_thesis": True, "source": "blog"}]}
        pack = {"items": [{"url": "https://reuters.com/z"}]}
        score, detail = C.catalyst_confidence(brief, research_pack=pack)
        self.assertLessEqual(detail["claim_support"], 0.25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
