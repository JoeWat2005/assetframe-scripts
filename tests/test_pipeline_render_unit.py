"""Offline unit tests for scripts/pipeline/render/* (the report compile / QA / HTML / PDF
pipeline + the display-formatting leaf).

These modules were relocated into the `pipeline/render` second-level subgroup during the
engine restructure; their flat intra-package imports (`import report_pdf`, `from _paths import
ROOT`, `from taxonomy import PREDICTION_TYPES`) resolve via the conftest sys.path shim. This file
targets the GAPS the existing suite leaves: the pure render-agnostic helpers and the QA gate's
individual error/warn branches. Everything is deterministic and OFFLINE: no network, Neon, R2,
Anthropic or subprocess - the only I/O is a self-written temp CSV on the local filesystem.

Run:  python -m pytest tests/test_pipeline_render_unit.py -q
"""
import os
import sys
import json
import atexit
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # mirror existing render tests

# fpdf2 is present on the box / CI; guard so a missing dep skips rather than errors collection.
try:
    import report_pdf as rp
    import formatting as F
    import mvp_report_const as C
    import mvp_report_qa as Q
    import mvp_report_html as H
    import mvp_report_pdf as PDF
    import mvp_report as M
    _HAVE_RENDER = True
except Exception:                       # pragma: no cover
    _HAVE_RENDER = False

skip_render = unittest.skipUnless(_HAVE_RENDER, "render pipeline requires fpdf2 (present on box/CI)")


# --------------------------------------------------------------------------- shared fixtures
_TMP = tempfile.mkdtemp(prefix="af_render_test_")
atexit.register(lambda: __import__("shutil").rmtree(_TMP, ignore_errors=True))


def _write_csv(name="hourly.csv", n=40, close=100.0):
    """A flat OHLC series whose last close == `close` (so the QA price triple-equality holds)."""
    path = os.path.join(_TMP, name)
    base = datetime(2026, 5, 1, 12, 0)
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close\n")
        for i in range(n):
            d = (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M")
            f.write(f"{d},{close - 1},{close + 1},{close - 1.5},{close}\n")
    return path


def _series(n, start_close=100.0, step=1.0):
    base = datetime(2026, 1, 1, 12, 0)
    return [{"d": (base + timedelta(days=i)).strftime("%Y-%m-%d %H:%M"),
             "o": 100.0, "h": 101.0, "l": 99.0, "c": start_close + i * step} for i in range(n)]


_CSV = _write_csv() if _HAVE_RENDER else None


def _valid_payload(csv=None):
    """A complete payload that passes run_qa cleanly. Returns a fresh dict each call so per-test
    mutations can't leak between tests."""
    csv = csv or _CSV
    levels = [
        {"id": "r1", "value": 105.0, "cls": "resistance", "label": "R1"},
        {"id": "e1", "value": 99.0, "cls": "entry", "label": "Entry lo"},
        {"id": "e2", "value": 101.0, "cls": "entry", "label": "Entry hi"},
        {"id": "inv", "value": 96.0, "cls": "invalidation", "label": "Invalidation"},
        {"id": "t1", "value": 110.0, "cls": "target", "label": "T1"},
        {"id": "t2", "value": 115.0, "cls": "target", "label": "T2"},
        {"id": "sup", "value": 98.0, "cls": "support", "label": "Support"},
    ]
    setups = [{"name": "Long breakout", "direction": "long", "entry_lo": 99.0, "entry_hi": 101.0,
               "invalidation": 96.0, "t1": 110.0, "t2": 115.0, "rr": "T1 2.0x; T2 3.0x"}]
    chart = {"csv": csv, "display_days": 14, "smas": [20], "rsi": True, "rsi_tag": "hourly",
             "support": [98.0], "resistance": [105.0]}
    return {
        "title": "WTI next session", "subtitle": "Crude oil intelligence",
        "status": "Wait", "risk": "Medium", "report_id": "AF-2026-0628-WTI",
        "confidence": 55, "out_dir": _TMP,
        "meta": {
            "last_price": "100.0 (last completed bar)", "data_quality_score": 8,
            "prediction_window_start_utc": "2026-06-28 13:00",
            "prediction_window_end_utc": "2026-06-29 13:00",
            "latest_bar_timestamp_utc": "2026-06-28 12:00",
            "market_session_type": "RTH", "market_close_utc": "2026-06-28 21:00",
            "next_major_event": "EIA inventories", "prediction_type": "breakout",
            "high_impact_claims": [],
        },
        "canonical": {
            "last_price": {"value": 100.0, "bar_complete": True},
            "levels": levels, "setups": setups,
            "ladder": ["r1", "e1", "e2", "inv", "t1", "t2", "sup"],
            "ledger_levels": [110.0, 96.0],
        },
        "free": {
            "chart": {"csv": csv, "display_days": 14, "support": [98.0], "resistance": [105.0]},
            "cards": [["Last price", "100.0"]],
            "bullets_html": "<ul><li><b>View</b><br>Range holds into EIA.</li></ul>",
            "scenarios_html": "<table><tr><th>Case</th><th>Path</th></tr>"
                              "<tr><td>Up</td><td>break 105</td></tr></table>",
            "timeline_events": [{"t": "Wed 13:00 UK", "label": "Window opens"},
                                {"t": "Thu 13:00 UK", "label": "Window closes (scored)"}],
            "teaser": "Pro adds R:R, the ladder, invalidation levels and the source audit.",
            "disclaimer": "Not personal financial advice.",
        },
        "pro": {
            "exec": [["Bias", "Neutral"], ["Setup", "Long breakout"]],
            "overview": ["Crude is coiling under 105.", "EIA is the catalyst."],
            "verdict": {"line": "Wait for the break.", "best": "Long over 105",
                        "risk": "Fade below 96", "stand_aside": "Chop in 98-101"},
            "catalyst_status": "EIA pending; no confirmed surprise.",
            "charts": [chart],
            "sentiment": {"fear_greed": {"value": 60, "label": "Greed", "source": "CNN"},
                          "rows": [["Survey", "Bullish", "Crowd leaning long"]],
                          "note": "Sentiment context only - market conversation, not a fact."},
            "sections": [
                {"heading": "Market summary", "html": "<p>Balanced tape under resistance.</p>"},
                {"heading": "Source audit", "html": "<p>Prices from exchange feed.</p>"},
            ],
            "source_confidence": [["Overall", "High"], ["Price", "Exchange"]],
            "disclaimer": "AssetFrame never places trades. Not personal advice.",
        },
    }


# =========================================================================== formatting.py
@skip_render
class TestFormatting(unittest.TestCase):
    def test_dp_boundaries(self):
        self.assertEqual(F._dp(0.5), 5)      # sub-1 FX/crypto
        self.assertEqual(F._dp(1.0), 4)      # boundary >=1
        self.assertEqual(F._dp(1.3406), 4)
        self.assertEqual(F._dp(9.99), 4)
        self.assertEqual(F._dp(10.0), 2)     # boundary >=10
        self.assertEqual(F._dp(45000), 2)

    def test_dp_uses_absolute_value(self):
        self.assertEqual(F._dp(-20), 2)
        self.assertEqual(F._dp(-0.4), 5)

    def test_to_display_summer_is_bst(self):
        out = F.to_display("2026-06-15 14:30")
        self.assertIn("15 Jun 2026 14:30 UTC", out)
        self.assertIn("BST", out)           # zoneinfo gives the correct summer abbrev

    def test_to_display_winter_is_gmt_not_bst(self):
        out = F.to_display("2026-01-15 14:30")
        self.assertIn("GMT", out)
        self.assertNotIn("BST", out)

    def test_to_display_bad_input_returned_unchanged(self):
        self.assertEqual(F.to_display("not-a-date"), "not-a-date")

    def test_to_london_dt_bad_input_is_none(self):
        self.assertIsNone(F._to_london_dt("garbage"))

    def test_ld_short_valid_and_invalid(self):
        self.assertTrue(F._ld_short("2026-06-15 14:30").endswith("UK"))
        self.assertEqual(F._ld_short("xx"), "xx")


# =========================================================================== report_pdf.py
@skip_render
class TestReportPdfPure(unittest.TestCase):
    def test_S_sanitizes_to_cp1252(self):
        self.assertEqual(rp.S("a→b ≤ c ✓ — x"), "a->b <= c OK - x")

    def test_S_replaces_unmappable(self):
        # an unmapped char outside cp1252 falls back to '?', never raises
        self.assertNotIn("★", rp.S("★"))

    def test_hex_rgb_with_and_without_hash(self):
        self.assertEqual(rp.hex_rgb("#0B3D6E"), (11, 61, 110))
        self.assertEqual(rp.hex_rgb("FFFFFF"), (255, 255, 255))

    def test_read_series_parses_skips_header_and_sorts(self):
        path = os.path.join(_TMP, "rs.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("date,open,high,low,close\n")
            f.write("2026-03-02 12:00,2,3,1,2.5\n")
            f.write("2026-03-01 12:00,1,2,0,1.5\n")     # out of order on purpose
        rows = rp.read_series(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["d"][:10] for r in rows], ["2026-03-01", "2026-03-02"])
        self.assertEqual(rows[0]["c"], 1.5)

    def test_read_series_ignores_short_and_nondate_rows(self):
        path = os.path.join(_TMP, "rs2.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("garbage,row\n")                    # < 5 cols
            f.write("notadate,1,2,3,4\n")               # col0 not a date
            f.write("2026-03-01,1,2,0,1.5\n")           # valid
        rows = rp.read_series(path)
        self.assertEqual(len(rows), 1)

    def test_sma_line_cold_and_warm(self):
        self.assertEqual(rp.sma_line([1, 2, 3], 5), [None, None, None])
        self.assertEqual(rp.sma_line([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_rsi_line_too_short_is_all_none(self):
        self.assertTrue(all(v is None for v in rp.rsi_line([float(i) for i in range(14)])))

    def test_rsi_line_only_gains_is_100(self):
        self.assertEqual(rp.rsi_line([float(i) for i in range(20)])[14], 100.0)

    def test_rsi_line_only_losses_is_0(self):
        self.assertEqual(rp.rsi_line([float(20 - i) for i in range(20)])[14], 0.0)

    def test_rsi_line_flat_series_no_div_by_zero(self):
        # average loss == 0 -> guarded to 100.0 (no ZeroDivisionError)
        self.assertEqual(rp.rsi_line([100.0] * 20)[14], 100.0)

    def test_fmt_decimal_half_up_and_grouping(self):
        # binary float .1f would round 1707.55 to 1707.5 (half-even); Decimal half-up -> 1707.6
        self.assertEqual(rp.fmt(1707.55, 1707.55), "1,707.6")
        self.assertEqual(rp.fmt(1.34062, 1.34), "1.3406")   # ref<50 -> 4dp
        self.assertEqual(rp.fmt(45000, 45000), "45,000.0")  # ref>=500 -> 1dp

    def test_fmt_level_plain_rounds_large_to_integer(self):
        self.assertEqual(rp.fmt_level(1646.4, 1646.4, plain=True), "1,646")
        self.assertEqual(rp.fmt_level(1646.4, 1646.4, plain=False), "1,646.4")

    def test_parse_dt_str_datetime_vs_date(self):
        self.assertEqual(rp.parse_dt_str("2026-06-28 14:30"), datetime(2026, 6, 28, 14, 30))
        self.assertEqual(rp.parse_dt_str("2026-06-28"), datetime(2026, 6, 28, 0, 0))

    def test_crop_index_window_zero_and_empty(self):
        rows = _series(8)
        self.assertGreater(rp.crop_index(rows, 3), 0)
        self.assertEqual(rp.crop_index(rows, 0), 0)       # no display_days -> warm-up off
        self.assertEqual(rp.crop_index([], 3), 0)

    def test_xlabel_modes(self):
        self.assertEqual(rp.xlabel("2026-06-28 14:30", "auto"), "06/28 14")
        self.assertEqual(rp.xlabel("2026-06-28", "auto"), "06-28")
        self.assertEqual(rp.xlabel("2026-06-28 14:30", "date"), "06-28")

    def test_plain_strips_tags_and_unescapes(self):
        self.assertEqual(rp.plain("<b>Hi</b> &amp; <i>there</i>"), "Hi & there")

    def test_num_cell_matches(self):
        for ok in ("1,234.5", "-5%", "~100", "n/a", "-", "3x", "12bps"):
            self.assertTrue(rp.NUM_CELL.match(ok), ok)
        self.assertIsNone(rp.NUM_CELL.match("abc"))


@skip_render
class TestPrepChart(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def tearDown(self):
        rp.WARN[:] = []

    def test_missing_when_series_shorter_than_sma(self):
        _, segs, _, notes = rp.prep_chart(_series(15), {"smas": [20]})
        self.assertEqual(segs[0][2], "missing")
        self.assertTrue(any("unavailable" in n for n in notes))

    def test_ok_when_window_fully_warmed(self):
        _, segs, _, notes = rp.prep_chart(_series(40), {"smas": [20], "display_days": 5})
        self.assertEqual(segs[0][2], "ok")
        self.assertEqual(notes, [])

    def test_partial_when_window_starts_before_warmup(self):
        _, segs, _, notes = rp.prep_chart(_series(25), {"smas": [20], "display_days": 14})
        self.assertEqual(segs[0][2], "partial")
        self.assertTrue(any("starts late" in n for n in notes))
        self.assertTrue(any("SMA20" in w for w in rp.WARN))   # disclosed in the global WARN log

    def test_rsi_partial_warns(self):
        rp.prep_chart(_series(25), {"rsi": True, "display_days": 14})
        self.assertTrue(any("RSI14 partial" in w for w in rp.WARN))


@skip_render
class TestChartSvg(unittest.TestCase):
    def test_chart_svg_structure_and_label(self):
        svg = rp.chart_svg(_series(40, step=0.5), {"smas": [20], "display_days": 5,
                                                   "label": "Hourly chart", "support": [105.0]})
        self.assertTrue(svg.startswith("<svg"))
        self.assertTrue(svg.rstrip().endswith("</svg>"))
        self.assertIn("Hourly chart", svg)
        self.assertIn("<polyline", svg)       # warmed SMA drawn as a polyline


# =========================================================================== mvp_report_const.py
@skip_render
class TestConstHelpers(unittest.TestCase):
    def test_items_to_html_escapes_label_only(self):
        # the label is escaped; the text is rendered raw on purpose (it carries authored HTML)
        out = C._items_to_html([{"label": "A & B", "text": "<i>kept</i>"}])
        self.assertIn("<b>A &amp; B</b>", out)
        self.assertIn("<i>kept</i>", out)
        self.assertTrue(out.startswith("<ul>") and out.endswith("</ul>"))

    def test_section_body_variants(self):
        self.assertEqual(C._section_body({"items": [{"label": "L", "text": "T"}], "html": "<p>x</p>"}),
                         "<ul><li><b>L</b><br>T</li></ul><p>x</p>")
        self.assertEqual(C._section_body({"html": "<p>x</p>"}), "<p>x</p>")
        self.assertEqual(C._section_body({}), "")

    def test_pct_from_sign_and_dp(self):
        self.assertEqual(C._pct_from(110, 100), "+10.0%")     # >=3% -> 1dp
        self.assertEqual(C._pct_from(101, 100), "+1.00%")     # <3% -> 2dp
        self.assertEqual(C._pct_from(95, 100), "-5.0%")
        self.assertEqual(C._pct_from(100, 100, dp=3), "+0.000%")

    def test_ladder_dp(self):
        far = [{"value": 110}]    # 10% away
        near = [{"value": 101}]   # 1% away
        self.assertEqual(C._ladder_dp(far, 100), 1)
        self.assertEqual(C._ladder_dp(near, 100), 2)

    def test_ladder_geometry_sorting_band_and_bounds(self):
        levels = [{"id": "a", "value": 110, "cls": "resistance", "label": "R"},
                  {"id": "b", "value": 90, "cls": "entry", "label": "E1"},
                  {"id": "c", "value": 92, "cls": "entry", "label": "E2"}]
        rows, y_last, band = C.ladder_geometry(levels, 100)
        self.assertEqual([r["value"] for r in rows], [110, 92, 90])      # high -> low
        for r in rows:
            self.assertTrue(0.0 <= r["y"] <= 1.0)
        self.assertAlmostEqual(y_last, 0.5, places=2)
        self.assertIsNotNone(band)                                       # entry levels present
        self.assertLess(band[0], band[1])

    def test_ladder_geometry_no_band_without_entry(self):
        levels = [{"id": "a", "value": 110, "cls": "resistance", "label": "R"}]
        _, _, band = C.ladder_geometry(levels, 100)
        self.assertIsNone(band)

    def test_glossary_core_terms_always_present(self):
        p = {"pro": {"charts": []}, "meta": {},
             "canonical": {"levels": [{"label": "Support", "cls": "support"}]}}
        rows = C._glossary_rows(p)
        self.assertEqual(rows[0][0], "Support / Resistance:")

    def test_glossary_conditional_terms(self):
        p = {"pro": {"charts": [{"smas": [20, 50], "rsi": True}]},
             "meta": {},
             "canonical": {"levels": [{"label": "PP", "cls": "support"},
                                      {"label": "Entry", "cls": "entry"},
                                      {"label": "Inval", "cls": "invalidation"},
                                      {"label": "T1", "cls": "target"}],
                           "setups": [{"name": "x"}]}}
        labels = [r[0] for r in C._glossary_rows(p)]
        self.assertIn("Pivots (PP, R1-R3, S1-S3):", labels)
        self.assertIn("SMA 20/50:", labels)
        self.assertIn("RSI(14):", labels)
        self.assertIn("Invalidation:", labels)
        self.assertIn("R:R:", labels)

    def test_report_quality_rows_pass_and_fail(self):
        rp.WARN[:] = []
        p = {"meta": {"data_quality_score": 7}, "pro": {"source_confidence": [["Overall", "High"]]}}
        ok = dict(C._report_quality_rows(p, {"header_price_matches_chart": True,
                                             "free_pro_split_enforced": True}))
        self.assertEqual(ok["Data quality score"], "7/10")
        self.assertEqual(ok["Canonical price alignment"], "Pass")
        self.assertEqual(ok["Free/Pro split"], "Pass")
        bad = dict(C._report_quality_rows(p, {"header_price_matches_chart": False,
                                              "free_pro_split_enforced": False}))
        self.assertEqual(bad["Canonical price alignment"], "CHECK FAILED")

    def test_fundamentals_rows_money_units_and_fallbacks(self):
        rows, cat, src = C._fundamentals_rows({
            "valuation": {"market_capitalization": 2.5e12, "trailing_pe": "n/a"},
            "margins": {"gross_margin": "bad"},
            "latest_earnings": {"date": "2026-06-10"}})
        d = dict(rows)
        self.assertEqual(d["Market cap"], "2.50T")          # T unit
        self.assertEqual(d["P/E (ttm)"], "n/a")             # non-numeric ratio falls through
        self.assertEqual(d["Gross margin"], "bad")          # non-numeric pct falls through
        self.assertTrue(any("Latest earnings" in c for c in cat))

    def test_fundamentals_rows_none_for_empty(self):
        self.assertEqual(C._fundamentals_rows(None), (None, None, None))
        self.assertEqual(C._fundamentals_rows({}), (None, None, None))


# =========================================================================== mvp_report_qa.py
@skip_render
class TestQaRegexHelpers(unittest.TestCase):
    def test_num_in_levels_tolerance(self):
        self.assertTrue(Q._num_in_levels(100.0, [100.0, 90.0]))
        self.assertTrue(Q._num_in_levels(100.0000001, [100.0]))
        self.assertFalse(Q._num_in_levels(50.0, [100.0, 90.0]))

    def test_rr_ok_accepts_approved_forms(self):
        for ok in ("T1 1.5x; T2 2.1x", "T1 below 1.0x; T2 2.0x",
                   "No valid R:R - excluded", "No valid R:R - setup excluded"):
            self.assertTrue(Q.RR_OK.match(ok), ok)

    def test_rr_ok_rejects_unapproved(self):
        for bad in ("1.5 / 2.1", "R:R 1.5", "T1 2.0x"):
            self.assertIsNone(Q.RR_OK.match(bad), bad)

    def test_rr_ok_accepts_na_target_form(self):
        # FIXED: RR_OK now accepts the "n/a" target form _fmt_rr legitimately emits (missing target),
        # so a valid build no longer hard-aborts on it.
        self.assertIsNotNone(Q.RR_OK.match("T1 2.0x; T2 n/a"))
        self.assertIsNotNone(Q.RR_OK.match("T1 n/a; T2 3.0x"))
        self.assertIsNotNone(Q.RR_OK.match("T1 below 1.0x; T2 n/a"))
        self.assertIsNotNone(Q.RR_OK.match("T1 1.5x; T2 2.1x"))   # existing valid forms still match

    def test_rr_bad_detects_negative_looking(self):
        self.assertTrue(Q.RR_BAD.search("~ -3"))
        self.assertIsNone(Q.RR_BAD.search("T1 1.5x; T2 2.1x"))


@skip_render
class TestRunQaHappyPath(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def test_clean_payload_has_no_errors(self):
        qa, errs, warns = Q.run_qa(_valid_payload())
        self.assertEqual(errs, [], f"unexpected QA errors: {errs}")

    def test_clean_payload_qa_flags_true(self):
        qa, errs, warns = Q.run_qa(_valid_payload())
        for key in ("header_price_matches_chart", "free_pro_split_enforced", "no_lookahead",
                    "levels_match_setups", "setups_match_ladder", "ledger_levels_match_tables",
                    "timestamps_normalized_utc", "rr_format_unambiguous", "prediction_type_valid",
                    "asset_session_rules_applied"):
            self.assertTrue(qa[key], key)
        self.assertFalse(qa["visual_inspection_passed"])   # only stamped post-render


@skip_render
class TestRunQaErrorBranches(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def _errs(self, mutate):
        p = _valid_payload()
        mutate(p)
        _, errs, _ = Q.run_qa(p)
        return errs

    def test_setup_price_not_canonical(self):
        def m(p):
            p["canonical"]["setups"][0]["t1"] = 999.0
        self.assertTrue(any("not in canonical levels" in e for e in self._errs(m)))

    def test_bad_rr_string_flagged(self):
        def m(p):
            p["canonical"]["setups"][0]["rr"] = "1.5 / 2.1"
        self.assertTrue(any("rr string not in approved format" in e for e in self._errs(m)))

    def test_free_chart_more_than_three_levels(self):
        def m(p):
            p["free"]["chart"]["support"] = [1.0, 2.0]
            p["free"]["chart"]["resistance"] = [3.0, 4.0]
        self.assertTrue(any("exceeds 3 labelled levels" in e for e in self._errs(m)))

    def test_banned_language(self):
        def m(p):
            p["pro"]["sections"][0]["html"] = "<p>this is a sure trade</p>"
        self.assertTrue(any("banned language" in e for e in self._errs(m)))

    def test_lookahead_detected(self):
        def m(p):
            p["meta"]["prediction_window_start_utc"] = "2026-06-28 10:00"   # before bar - 1h
        self.assertTrue(any("lookahead" in e for e in self._errs(m)))

    def test_missing_session_fields(self):
        def m(p):
            p["meta"].pop("market_session_type")
        self.assertTrue(any("session fields missing" in e for e in self._errs(m)))

    def test_invalid_prediction_type(self):
        def m(p):
            p["meta"]["prediction_type"] = "frobnicate"
        self.assertTrue(any("not in taxonomy" in e for e in self._errs(m)))

    def test_confidence_breakdown_mismatch(self):
        def m(p):
            p["confidence"] = 55
            p["confidence_breakdown"] = {"published": 40}
        self.assertTrue(any("confidence_breakdown.published" in e for e in self._errs(m)))

    def test_ledger_level_not_canonical(self):
        def m(p):
            p["canonical"]["ledger_levels"] = [123.456]
        self.assertTrue(any("ledger level" in e for e in self._errs(m)))


@skip_render
class TestRunQaWarnBranches(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def test_social_signal_without_soft_framing_warns(self):
        p = _valid_payload()
        p["pro"]["sections"][0]["html"] = "<p>social sentiment is wildly bullish here</p>"
        p["pro"]["sentiment"]["note"] = "readings only"     # drop the soft framing
        qa, errs, warns = Q.run_qa(p)
        self.assertFalse(qa["social_labelled_soft"])
        self.assertTrue(any("market conversation" in w for w in warns))

    def test_missing_overview_warns(self):
        p = _valid_payload()
        p["pro"].pop("overview")
        _, _, warns = Q.run_qa(p)
        self.assertTrue(any("pro.overview" in w for w in warns))


# =========================================================================== mvp_report.py
@skip_render
class TestNormalize(unittest.TestCase):
    def test_strip_dashes_li_preserves_negative_numbers(self):
        out, n = M._strip_dashes("<li>- Hello world</li><li>-0.2% drop</li>")
        self.assertEqual(out, "<li>Hello world</li><li>-0.2% drop</li>")
        self.assertEqual(n, 1)

    def test_strip_dashes_after_br(self):
        out, n = M._strip_dashes("<br>- After break")
        self.assertEqual(out, "<br>After break")
        self.assertEqual(n, 1)

    def test_normalize_payload_counts_and_sets_label_style(self):
        p = {"free": {"bullets_html": "<li>- A</li>", "scenarios_html": "<li>- B</li>",
                      "chart": {}},
             "pro": {"sections": [{"html": "<li>- C</li>"}, {"html": "no dash"}]}}
        n = M._normalize_payload(p)
        self.assertEqual(n, 3)
        self.assertEqual(p["free"]["chart"]["label_style"], "plain")
        self.assertEqual(p["free"]["bullets_html"], "<li>A</li>")

    def test_normalize_payload_handles_none_chart(self):
        self.assertEqual(M._normalize_payload({"free": {"chart": None}, "pro": {}}), 0)


@skip_render
class TestBuildMetadata(unittest.TestCase):
    def test_metadata_structure(self):
        p = _valid_payload()
        qa, _, warns = Q.run_qa(p)
        meta = M.build_metadata(p, qa, [], [], warns)
        self.assertEqual(meta["report_timezone"], "UTC")
        self.assertEqual(meta["brand"], C.BRAND)
        self.assertEqual(meta["qa_checks"], qa)
        self.assertTrue(meta["indicator_warmup_confirmed"])     # no warm-up warnings passed
        self.assertTrue(meta["partial_indicators_hidden"])
        self.assertEqual(set(meta["paths"]), {
            "free_pdf", "pro_pdf", "metadata_json", "preview_png", "free_html", "pro_html"})
        self.assertEqual(meta["source_confidence"], {"Overall": "High", "Price": "Exchange"})
        self.assertTrue(meta["plain_english_overview_included"])
        self.assertTrue(meta["sentiment_block_included"])

    def test_metadata_warmup_warnings_propagate(self):
        p = _valid_payload()
        qa, _, _ = Q.run_qa(p)
        meta = M.build_metadata(p, qa, ["SMA20 starts late (x)"], [], [])
        self.assertFalse(meta["indicator_warmup_confirmed"])
        self.assertIn("SMA20 starts late (x)", meta["indicator_warmup_warnings"])


# =========================================================================== mvp_report_html.py
@skip_render
class TestHtmlHelpers(unittest.TestCase):
    def test_timeline_html_gap_and_arrows(self):
        out = H._timeline_html([{"t": "09:00", "label": "Open"},
                                {"t": "16:00", "label": "Close", "gap": True}])
        self.assertIn('class="tl gap"', out)
        self.assertIn("&rsaquo;", out)                  # arrow between chips
        self.assertIn("Open", out)

    def test_gauge_svg_contains_value(self):
        out = H._gauge_svg(55)
        self.assertTrue(out.startswith("<svg"))
        self.assertIn("55/100", out)

    def test_cards_html_escapes(self):
        self.assertIn("V &amp; x", H._cards_html([("K", "V & x")]))

    def test_info_box_html_label_and_plain_rows(self):
        out = H._info_box_html("Title", [("Lab", "txt"), (None, "plain")])
        self.assertIn("<b>Lab</b> txt", out)
        self.assertIn(">plain<", out)
        self.assertIn('class="infobox"', out)
        self.assertIn('class="teaser"', H._info_box_html("T", [], accent_bg=True))

    def test_fg_svg_clamps_out_of_range_value(self):
        out = H._fg_svg({"value": 120, "label": "Greed", "source": "CNN", "asof": "2026-06-28"})
        self.assertIn("100 - Greed", out)               # clamped to 100
        self.assertIn("Source: CNN", out)

    def test_ladder_svg_smoke(self):
        levels = [{"id": "a", "value": 110, "cls": "resistance", "label": "R"},
                  {"id": "b", "value": 95, "cls": "entry", "label": "Entry"}]
        out = H.ladder_svg(levels, {"value": 100.0})
        self.assertTrue(out.startswith("<svg"))
        self.assertIn("LAST", out)
        self.assertIn("Entry", out)

    def test_sentiment_html_sections(self):
        out = H._sentiment_html({"fear_greed": {"value": 60, "label": "Greed"},
                                 "rows": [["Survey", "Bullish", "context"]],
                                 "note": "soft signal only"})
        self.assertIn("Sentiment", out)
        self.assertIn("Survey", out)
        self.assertIn("soft signal only", out)


@skip_render
class TestHtmlBuilders(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def test_build_free_html_smoke(self):
        html = H.build_free_html(_valid_payload())
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("Snapshot", html)
        self.assertIn("WTI next session", html)
        self.assertTrue(html.rstrip().endswith("</html>"))

    def test_build_pro_html_smoke(self):
        p = _valid_payload()
        qa, _, _ = Q.run_qa(p)
        html = H.build_pro_html(p, qa)
        self.assertIn("ASSETFRAME PRO", html)
        self.assertIn("Source confidence", html)        # injected before the source-audit section
        self.assertIn("Report quality", html)
        self.assertIn("Glossary", html)


# =========================================================================== mvp_report_pdf.py
@skip_render
class TestPdfHelpers(unittest.TestCase):
    def _pdf(self):
        pdf = rp.Report(format="A4")
        pdf.add_page()
        return pdf

    def test_wrap_text_single_line(self):
        self.assertEqual(PDF.wrap_text(self._pdf(), "short text", 7.4, 80), ["short text"])

    def test_wrap_text_empty_returns_single_blank(self):
        self.assertEqual(PDF.wrap_text(self._pdf(), "", 7.4, 80), [""])

    def test_wrap_text_truncates_with_ellipsis(self):
        lines = PDF.wrap_text(self._pdf(), " ".join(["word"] * 40), 7.0, 20, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("..."))


@skip_render
class TestPdfBuilders(unittest.TestCase):
    def setUp(self):
        rp.WARN[:] = []

    def test_build_free_pdf_emits_valid_pdf(self):
        out = PDF.build_free(_valid_payload()).output()
        self.assertEqual(bytes(out[:5]), b"%PDF-")

    def test_build_pro_pdf_emits_valid_pdf(self):
        p = _valid_payload()
        qa, _, _ = Q.run_qa(p)
        out = PDF.build_pro(p, qa).output()
        self.assertEqual(bytes(out[:5]), b"%PDF-")


if __name__ == "__main__":
    unittest.main(verbosity=2)
