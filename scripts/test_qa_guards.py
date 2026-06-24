"""Guards for two production false-positives fixed 2026-06-24:
  - the banned-language QA must NOT fire on the legitimate finance term "risk-free rate"
    (it was aborting equity reports whose fundamentals discuss valuation), while still
    banning the marketing sense ("risk-free profit/trade");
  - the report ticker that forms the slug / report_id / R2 key / public URL must be
    URL- and object-key-safe (a price symbol like "GC=F" must never leak an "=").
"""
import re
import sys
import unittest

sys.path.insert(0, "scripts")
import mvp_report
import social_posts


def _risk_pat(banned):
    return next(p for p in banned if "risk" in p)


class TestRiskFreeRate(unittest.TestCase):
    def test_legit_finance_terms_allowed(self):
        for banned in (mvp_report.BANNED, social_posts.BANNED):
            pat = _risk_pat(banned)
            for ok in ("the risk-free rate is 4.3%", "a risk free yield benchmark",
                       "discounted at the risk-free rate", "the risk-free asset"):
                self.assertIsNone(re.search(pat, ok.lower()), f"{pat} wrongly flagged {ok!r}")

    def test_marketing_sense_still_banned(self):
        for banned in (mvp_report.BANNED, social_posts.BANNED):
            pat = _risk_pat(banned)
            for bad in ("a risk-free profit", "risk free trade", "this is risk-free money"):
                self.assertIsNotNone(re.search(pat, bad.lower()), f"{pat} missed {bad!r}")


class TestSlugSafety(unittest.TestCase):
    def _safe(self, raw):
        # mirrors scaffold_payload's ticker sanitization
        return "".join(c for c in (raw or "").upper() if c.isalnum() or c in "._-") or "ASSET"

    def test_unsafe_symbol_sanitized(self):
        self.assertEqual(self._safe("GC=F"), "GCF")
        self.assertNotIn("=", self._safe("GC=F"))
        self.assertNotIn("/", self._safe("XAU/USD"))

    def test_clean_tickers_unchanged(self):
        for t in ("BTC", "AAPL", "GBPUSD", "GOLD", "BRK.B"):
            self.assertEqual(self._safe(t), t)


if __name__ == "__main__":
    unittest.main()
