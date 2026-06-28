"""Phase 2 foundation: horizon-aware calibration (calibrate.build_calibration +
confidence._apply_calibration) and the per-asset timeframes / fetch-flag config defaults.

Offline, stdlib only.  Run:  python -m pytest tests/test_horizon_calibration.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibrate as CB
import confidence as C
import config_loader as CL


def _pts(horizon, n, rate, x=70.0):
    """n observations at raw score x with the given realised rate (0..1), tagged `horizon`."""
    return [(x, rate, 1, horizon) for _ in range(n)]


class TestBuildCalibration(unittest.TestCase):
    def test_global_map_present_and_versioned(self):
        out = CB.build_calibration(_pts("next_session", 20, 0.7))
        self.assertEqual(out["version"], 2)
        self.assertGreaterEqual(len(out["knots"]), 2)        # global map at the top level

    def test_per_horizon_maps_differ(self):
        out = CB.build_calibration(_pts("intraday", 20, 0.4) + _pts("multi_session", 20, 0.9))
        self.assertIn("intraday", out["by_horizon"])
        self.assertIn("multi_session", out["by_horizon"])
        self.assertNotEqual(out["by_horizon"]["intraday"]["knots"],
                            out["by_horizon"]["multi_session"]["knots"])

    def test_thin_horizon_omitted(self):
        out = CB.build_calibration(_pts("next_session", 20, 0.7) + _pts("intraday", 2, 0.1))
        self.assertNotIn("intraday", out.get("by_horizon", {}))   # < min_rows -> falls back to global

    def test_untagged_rows_yield_only_global(self):
        out = CB.build_calibration(_pts("", 20, 0.7))
        self.assertNotIn("by_horizon", out)


class TestApplyCalibration(unittest.TestCase):
    def _map(self):
        # global map halves the score; the intraday sub-map is identity
        return {"knots": [[0, 0], [100, 50]],
                "by_horizon": {"intraday": {"knots": [[0, 0], [100, 100]]}}}

    def test_horizon_submap_used_when_present(self):
        self.assertEqual(C._apply_calibration(80, self._map(), "intraday"), 80)

    def test_other_horizon_uses_global(self):
        self.assertEqual(C._apply_calibration(80, self._map(), "multi_session"), 40)

    def test_no_horizon_uses_global(self):
        self.assertEqual(C._apply_calibration(80, self._map(), None), 40)

    def test_invalid_submap_falls_back_to_global(self):
        m = {"knots": [[0, 0], [100, 50]], "by_horizon": {"intraday": {"knots": [[0, 0]]}}}
        self.assertEqual(C._apply_calibration(80, m, "intraday"), 40)


class TestTimeframesConfig(unittest.TestCase):
    def _asset(self, **over):
        base = {"id": "x", "name": "X", "instrument": "X", "ticker": "X",
                "provider_symbols": {"yahoo": "X"}, "asset_class": "equity",
                "session_profile": "us_equity_rth", "cadence": "daily", "timezone": "UTC"}
        base.update(over)
        return CL._normalize(base)

    def test_timeframes_defaults_to_forecast_window(self):
        self.assertEqual(self._asset(forecast_window="next_week")["timeframes"], ["next_week"])

    def test_timeframes_dedup_keep_order(self):
        a = self._asset(timeframes=["next_session", "next_week", "next_session"])
        self.assertEqual(a["timeframes"], ["next_session", "next_week"])

    def test_fetch_flag_defaults(self):
        eq = self._asset(asset_class="equity")
        fx = self._asset(asset_class="fx", session_profile="fx_spot")
        self.assertTrue(eq["include_fundamentals"])      # equities default fundamentals on
        self.assertFalse(fx["include_fundamentals"])     # non-equities default off
        self.assertTrue(eq["include_news"])

    def test_bad_timeframe_rejected(self):
        errs = CL._validate_one({"id": "x", "name": "X", "instrument": "X", "ticker": "X",
                                 "provider_symbols": {"yahoo": "X"}, "asset_class": "equity",
                                 "session_profile": "us_equity_rth", "cadence": "daily",
                                 "timezone": "UTC", "timeframes": ["bogus"]}, 0, set())
        self.assertTrue(any("timeframe 'bogus'" in e for e in errs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
