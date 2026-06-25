"""Fundamentals render: the shared extractor + the HTML twin (the PDF twin uses the same rows).
Figures come straight from the canonical block, so the report can never show a fabricated number.

Offline, stdlib only.  Run:  python scripts/test_fundamentals_render.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import mvp_report as M
    _HAVE_RENDER = True
except Exception:                       # mvp_report needs fpdf2 (installed on the box / CI)
    _HAVE_RENDER = False

FUND = {
    "source": "twelvedata", "symbol": "AAPL",
    "valuation": {"market_capitalization": 4359060534113, "trailing_pe": 36.12,
                  "forward_pe": 31.04, "peg_ratio": 1.61, "price_to_sales_ttm": 9.7},
    "margins": {"gross_margin": 0.49, "operating_margin": 0.32, "profit_margin": 0.27,
                "return_on_equity_ttm": 1.41},
    "profile": {"sector": "Technology", "industry": "Consumer Electronics"},
    "latest_earnings": {"date": "2026-06-10", "eps_actual": -1.36, "eps_estimate": -0.77,
                        "surprise_prc": 76.62},
    "fetched_utc": "2026-06-22 20:00",
}


@unittest.skipUnless(_HAVE_RENDER, "mvp_report requires fpdf2 (present on the box / CI)")
class TestFundamentalsRows(unittest.TestCase):
    def test_rows_extracted_and_formatted(self):
        rows, cat, src = M._fundamentals_rows(FUND)
        d = dict(rows)
        self.assertEqual(d["Market cap"], "4.36T")
        self.assertEqual(d["P/E (ttm)"], "36.1")
        self.assertEqual(d["Net margin"], "27.0%")
        self.assertEqual(d["ROE"], "141.0%")
        self.assertTrue(any("Latest earnings" in c and "surprise" in c for c in cat))
        self.assertIn("Twelve Data", src)
        self.assertIn("Technology", src)

    def test_none_for_empty(self):
        self.assertEqual(M._fundamentals_rows(None), (None, None, None))
        self.assertEqual(M._fundamentals_rows({}), (None, None, None))


@unittest.skipUnless(_HAVE_RENDER, "mvp_report requires fpdf2 (present on the box / CI)")
class TestFundamentalsHtml(unittest.TestCase):
    def test_section_rendered_from_canonical(self):
        html = M._fundamentals_html(FUND)
        for token in ("Fundamentals", "4.36T", "36.1", "Net margin", "Latest earnings", "Twelve Data"):
            self.assertIn(token, html)

    def test_empty_when_absent(self):
        self.assertEqual(M._fundamentals_html(None), "")
        self.assertEqual(M._fundamentals_html({}), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
