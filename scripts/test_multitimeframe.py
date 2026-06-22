"""Multi-timeframe track helpers in scaffold_payload: forecast-window -> taxonomy horizon, and
horizon-tagged track report_ids that keep the ticker (last '-' segment) and year (leading digits)
parseable by every downstream consumer (the scorer, sync-db, editions).

Offline, stdlib only.  Run:  python scripts/test_multitimeframe.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scaffold_payload as SP


class TestHorizonFor(unittest.TestCase):
    def test_long_windows_are_multi_session(self):
        self.assertEqual(SP._horizon_for("next_week"), "multi_session")
        self.assertEqual(SP._horizon_for("next_5_sessions"), "multi_session")

    def test_standard_windows_are_next_session(self):
        for fw in ("next_session", "next_liquid_session", "next_regular_session", "rolling_24h", "", None):
            self.assertEqual(SP._horizon_for(fw), "next_session")


class TestTrackReportId(unittest.TestCase):
    def test_next_session_keeps_canonical_id(self):
        # the primary track keeps the published edition id untouched
        self.assertEqual(SP._track_report_id("AF-20260623-GOLD", "next_session"), "AF-20260623-GOLD")

    def test_multi_session_tagged_and_parseable(self):
        rid = SP._track_report_id("AF-20260623-GOLD", "multi_session")
        self.assertEqual(rid, "AF-20260623MS-GOLD")
        self.assertEqual(rid.rsplit("-", 1)[-1], "GOLD")                       # ticker still parses
        self.assertEqual("".join(c for c in rid if c.isdigit())[:4], "2026")   # year still parses
        self.assertNotEqual(rid, "AF-20260623-GOLD")                           # distinct ledger row

    def test_backdated_stamp_tagged(self):
        rid = SP._track_report_id("AF-202606231430-BTC", "multi_session")
        self.assertEqual(rid, "AF-202606231430MS-BTC")
        self.assertEqual(rid.rsplit("-", 1)[-1], "BTC")


class TestSetupOverride(unittest.TestCase):
    """Analyst-selected setup levels: the brief may pick CANONICAL level ids; anything non-canonical
    is ignored (no fabrication). Direction + the deterministic confidence are never touched here."""
    def _by_id(self):
        return {"s1": {"value": 100.0}, "inner_lo": {"value": 98.0}, "s2": {"value": 95.0},
                "pp": {"value": 105.0}, "r1": {"value": 110.0}}

    def _primary(self):
        return {"name": "Long-biased (washout into the floor cluster)", "direction": "long",
                "entry_lo": 101.0, "entry_hi": 102.0, "invalidation": 99.0, "t1": 105.0,
                "t2": 110.0, "rr": "x"}

    def test_override_applies_canonical_levels(self):
        ovr = {"side": "long", "entry_ids": ["s1", "inner_lo"], "invalidation_id": "s2",
               "t1_id": "pp", "t2_id": "r1"}
        s = SP._apply_setup_override(self._primary(), self._by_id(), ovr)
        self.assertEqual((s["entry_lo"], s["entry_hi"]), (98.0, 100.0))
        self.assertEqual(s["invalidation"], 95.0)
        self.assertEqual((s["t1"], s["t2"]), (105.0, 110.0))
        self.assertTrue(s["analyst_selected"])
        self.assertEqual(s["direction"], "long")            # direction never changed

    def test_noncanonical_override_ignored(self):
        ovr = {"side": "long", "entry_ids": ["bogus"], "invalidation_id": "also_bogus"}
        p = self._primary()
        self.assertIs(SP._apply_setup_override(p, self._by_id(), ovr), p)   # unchanged, no fabrication

    def test_missing_invalidation_ignored(self):
        p = self._primary()
        self.assertIs(SP._apply_setup_override(p, self._by_id(), {"side": "long", "entry_ids": ["s1"]}), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
