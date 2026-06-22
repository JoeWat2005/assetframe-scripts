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


if __name__ == "__main__":
    unittest.main(verbosity=2)
