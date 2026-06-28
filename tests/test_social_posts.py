"""Tests for social_posts.py — the safe-wording QA gate (pump/advice phrases are a
build error), the negated-"guaranteed" allowance, and the neutral
"AssetFrame published..." framing of generated drafts.

Run:  python -m pytest tests/test_social_posts.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import social_posts as SOC


def _payload():
    return {
        "report_id": "AF-20260616-AAPL",
        "title": "Apple (AAPL)",
        "status": "Active",
        "risk": "Medium",
        "confidence": 64,
        "meta": {
            "instrument": "Apple", "ticker": "AAPL", "confidence_band": "Moderate",
            "research_view": "Constructive into the next session while above the floor.",
            "prediction_window_start_report_tz": "Mon 16 Jun 2026 14:30 UK",
            "prediction_window_end_report_tz": "Tue 17 Jun 2026 21:00 UK",
            "report_date": "2026-06-16",
        },
    }


class TestSafeWordingGate(unittest.TestCase):
    def test_banned_phrase_exits_2(self):
        bad = {"x": "AssetFrame says buy now before it moons."}
        with self.assertRaises(SystemExit) as cm:
            SOC.safe_wording_check(bad)
        self.assertEqual(cm.exception.code, 2)

    def test_each_banned_phrase_caught(self):
        for phrase in ("buy now", "sell now", "sure thing", "easy profit",
                       "risk-free", "you should buy", "get rich", "to the moon"):
            with self.assertRaises(SystemExit):
                SOC.safe_wording_check({"post": f"this is a {phrase} situation"})

    def test_clean_posts_pass(self):
        SOC.safe_wording_check({
            "x": "AssetFrame published its next-session read on Apple. Scored after close.",
            "linkedin": "Research view: constructive. Confidence band: Moderate.",
        })  # no raise

    def test_guaranteed_negated_form_allowed(self):
        SOC.safe_wording_check({"x": "No outcome is guaranteed. Do your own research."})

    def test_guaranteed_bare_form_rejected(self):
        with self.assertRaises(SystemExit):
            SOC.safe_wording_check({"x": "This setup is guaranteed to print."})


class TestBuildPosts(unittest.TestCase):
    def setUp(self):
        self.posts = SOC.build_posts(_payload())

    def test_all_four_channels(self):
        self.assertEqual(set(self.posts), {"x", "linkedin", "newsletter_snippet",
                                           "reddit_summary"})

    def test_neutral_published_framing(self):
        # every draft attributes to AssetFrame in a neutral "published" voice
        # (either "AssetFrame published..." or "AssetFrame's latest read is published").
        for key, text in self.posts.items():
            low = text.lower()
            self.assertIn("assetframe", low, f"{key} missing AssetFrame attribution")
            self.assertIn("publish", low, f"{key} missing neutral published framing")

    def test_confidence_expressed_as_band_not_number(self):
        # the drafts express confidence as a BAND, never the raw integer
        for key, text in self.posts.items():
            self.assertIn("Moderate", text)
            self.assertNotIn("64/100", text)

    def test_disclaimer_and_scored_line_present(self):
        for text in self.posts.values():
            self.assertIn("not personal financial advice", text.lower())
        self.assertIn(SOC.SCORED_LINE, self.posts["x"])

    def test_report_link_placeholder_present(self):
        for text in self.posts.values():
            self.assertIn(SOC.REPORT_LINK, text)

    def test_generated_posts_pass_their_own_gate(self):
        SOC.safe_wording_check(self.posts)  # no raise — the templates are safe by design


if __name__ == "__main__":
    unittest.main(verbosity=2)
