"""Per-asset news toggle in brief_writer: include_news -> (web_search budget, prompt suffix),
and the --no-news CLI flag. TD /news is a business-tier feature (404 on the individual Grow plan),
so WebSearch stays the news source and this just gates how much of it the brief does.

Offline, stdlib only.  Run:  python -m pytest tests/test_news_toggle.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import brief_writer as B
    _HAVE = True
except Exception:                       # brief_writer may need the Anthropic SDK to import
    _HAVE = False


@unittest.skipUnless(_HAVE, "brief_writer import (Anthropic SDK)")
class TestNewsToggle(unittest.TestCase):
    def test_news_on_full_budget_no_suffix(self):
        # Default news-on budget is 6 (ASSETFRAME_BRIEF_WEB_MAX_USES); news-off must be < news-on.
        os.environ.pop("ASSETFRAME_BRIEF_WEB_MAX_USES", None)
        uses, suffix = B._news_settings(True)
        self.assertEqual(uses, 6)
        self.assertEqual(suffix, "")

    def test_news_on_budget_is_configurable(self):
        os.environ["ASSETFRAME_BRIEF_WEB_MAX_USES"] = "4"
        try:
            self.assertEqual(B._news_settings(True)[0], 4)
        finally:
            os.environ.pop("ASSETFRAME_BRIEF_WEB_MAX_USES", None)
        # a garbage value falls back to the default, never crashes
        os.environ["ASSETFRAME_BRIEF_WEB_MAX_USES"] = "nope"
        try:
            self.assertEqual(B._news_settings(True)[0], 6)
        finally:
            os.environ.pop("ASSETFRAME_BRIEF_WEB_MAX_USES", None)

    def test_news_off_trims_budget_and_adds_directive(self):
        os.environ.pop("ASSETFRAME_BRIEF_WEB_MAX_USES", None)
        uses, suffix = B._news_settings(False)
        self.assertLess(uses, B._news_settings(True)[0])
        self.assertIn("technical-focus", suffix)
        self.assertIn("INSTRUMENT MODE", suffix)

    def test_no_news_flag_parsed(self):
        base = ["BTC", "--analysis", "a.json", "--memory-pack", "m.json", "--out", "o.json"]
        self.assertTrue(B.parse_args(base + ["--no-news"]).no_news)
        self.assertFalse(B.parse_args(base).no_news)


if __name__ == "__main__":
    unittest.main(verbosity=2)
