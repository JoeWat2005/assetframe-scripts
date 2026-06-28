"""Offline unit tests for scripts/pipeline/scoring/* — the GAPS not already covered by
test_taxonomy / test_score_report / test_confidence / test_scaffold_payload / test_audit_fixes /
test_multitimeframe / test_horizon_calibration / test_data_license.

Focus areas (each a distinct, previously-untested concern):
  * payload_sections.py leaf HTML builders (pure string assemblers) — direct unit tests.
  * confidence.py internal scorers (_trend/_momentum/_structure/_vol), _is_valid_calib_map,
    _thesis_source_cap, _in_window_event, _claim_traced, _hype_thesis, social notes, catalyst gaps.
  * score_report.py expect=False (V2 bearish phrasing) grading, load_bars corrupt-row skipping,
    calibration row-skip on unparseable confidence, _prediction_text, _write_scored_sidecar,
    score_setup short side, parse_args edge errors.
  * scaffold_payload.py _period_stamp (daily/weekly/monthly/backdated), read_last_bar,
    _quality, _lookback, _claims status normalization.
  * taxonomy.py derive/normalize branches + futures caret-stripping not exercised elsewhere.

All offline & deterministic: no network / Neon / Anthropic / R2 / boto3 / subprocess.

Run:  python -m pytest tests/test_pipeline_scoring_unit.py -q
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import payload_sections as PS
import confidence as C
import score_report as S
import scaffold_payload as SP
import taxonomy as T


# ===========================================================================
# payload_sections.py — leaf HTML builders (pure functions; only covered
# transitively before this file, except _source_audit_html via test_data_license)
# ===========================================================================

class TestScenarioMatrixHtml(unittest.TestCase):
    def test_row_values_and_table_wrapper(self):
        html = PS._scenario_matrix_html([
            {"case": "Bull", "trigger": "reclaim PP", "move": "+1%",
             "invalidation": "below S1", "confidence": "60", "watch": "vol"}])
        self.assertTrue(html.startswith("<table>"))
        self.assertTrue(html.endswith("</table>"))
        for token in ("Bull", "reclaim PP", "+1%", "below S1", "vol"):
            self.assertIn(token, html)

    def test_empty_rows_still_valid_table(self):
        html = PS._scenario_matrix_html([])
        self.assertIn("<th>Case</th>", html)
        self.assertTrue(html.endswith("</table>"))

    def test_missing_keys_default_to_blank(self):
        # a row with no keys must not KeyError — every cell falls back to ''
        html = PS._scenario_matrix_html([{}])
        self.assertIn("<td></td>", html)


class TestEventsHtml(unittest.TestCase):
    def test_in_window_and_gap_risk_rendering(self):
        html = PS._events_html([
            {"label": "CPI", "when": "13:30", "relevance": "high",
             "in_window": True, "gap_risk": True},
            {"label": "Fed speak", "in_window": False, "gap_risk": False}])
        # first event: in-window Yes, gap Yes ; second: No, '-'
        self.assertIn("<td>CPI</td>", html)
        self.assertIn("<td>Yes</td><td>Yes</td>", html)
        self.assertIn("<td>No</td><td>-</td>", html)


class TestTechnicalsHtml(unittest.TestCase):
    def test_distance_sign_and_classification(self):
        levels = [{"label": "R1", "value": 103.0, "cls": "resistance"},
                  {"label": "S1", "value": 97.0, "cls": "support"}]
        html = PS._technicals_html({}, levels, 100.0, {"technicals_note": "watch the open"})
        self.assertIn("<p>watch the open</p>", html)
        self.assertIn("+3.00", html)          # 103 - 100
        self.assertIn("-3.00", html)          # 97 - 100
        self.assertIn("Resistance", html)     # cls.title()
        self.assertIn("Support", html)

    def test_no_note_no_paragraph_prefix(self):
        html = PS._technicals_html({}, [], 100.0, {})
        self.assertFalse(html.startswith("<p>"))
        self.assertIn("<th>Level</th>", html)


class TestSetupsHtml(unittest.TestCase):
    def test_setup_row(self):
        html = PS._setups_html([{
            "name": "Long-biased", "direction": "long", "entry_lo": 99, "entry_hi": 100,
            "invalidation": 97, "t1": 104, "t2": 106, "rr": "T1 2.0x"}])
        self.assertIn("Long-biased", html)
        self.assertIn("Long", html)            # direction.title()
        self.assertIn("99 - 100", html)
        self.assertIn("104 / 106", html)
        self.assertIn("T1 2.0x", html)

    def test_missing_targets_render_none(self):
        # t1/t2 are read via .get -> 'None' string, never a KeyError
        html = PS._setups_html([{
            "name": "X", "direction": "short", "entry_lo": 10, "entry_hi": 11,
            "invalidation": 12, "rr": "n/a"}])
        self.assertIn("None / None", html)


class TestScorecardHtml(unittest.TestCase):
    def _conf(self, **over):
        base = {
            "components": [{"name": "Market", "weight": 50, "score": 0.512},
                           {"name": "Social adj.", "weight": 0, "score": -2.0}],
            "published": 63, "band": "Moderate", "raw": 61.0,
            "caps_applied": ["single_source_thesis->65"], "calibrated": False,
            "conf_version": 2,
        }
        base.update(over)
        return base

    def test_published_band_and_version(self):
        html = PS._scorecard_html(self._conf())
        self.assertIn("Published confidence: 63/100", html)
        self.assertIn("(Moderate)", html)
        self.assertIn("raw 61.0", html)
        self.assertIn("engine v2", html)

    def test_float_score_two_dp_and_weight_adj(self):
        html = PS._scorecard_html(self._conf())
        self.assertIn("0.51", html)            # 0.512 -> %.2f
        self.assertIn("<td>adj</td>", html)    # weight 0 -> 'adj'
        self.assertIn("<td>50%</td>", html)

    def test_caps_applied_line(self):
        html = PS._scorecard_html(self._conf())
        self.assertIn("Caps applied: single_source_thesis->65.", html)

    def test_no_caps_omits_line(self):
        html = PS._scorecard_html(self._conf(caps_applied=[]))
        self.assertNotIn("Caps applied:", html)

    def test_calibrated_vs_identity_wording(self):
        self.assertIn("identity", PS._scorecard_html(self._conf(calibrated=False)))
        self.assertIn("applied from the ledger",
                      PS._scorecard_html(self._conf(calibrated=True)))


class TestLedgerHtml(unittest.TestCase):
    def test_levels_joined(self):
        html = PS._ledger_html({}, [100, 103.5])
        self.assertIn("Ledger:", html)
        self.assertIn("100, 103.5", html)

    def test_empty_levels(self):
        html = PS._ledger_html({}, [])
        self.assertIn("Levels under test: .", html)


class TestSourceAuditGapsBranch(unittest.TestCase):
    # license/split-source branches are covered by test_data_license; the source_gaps
    # branch (from the flat brief key) is the remaining untested path here.
    def test_source_gaps_listed(self):
        html = PS._source_audit_html({"source_gaps": ["no options IV", "no short interest"]},
                                     {"provider": {"hourly": "yahoo"}}, 8)
        self.assertIn("Gaps:", html)
        self.assertIn("no options IV; no short interest", html)
        self.assertIn("Overall data quality: 8/10", html)

    def test_no_gaps_no_gaps_line(self):
        html = PS._source_audit_html({}, {"provider": {"hourly": "yahoo"}}, 9)
        self.assertNotIn("Gaps:", html)


# ===========================================================================
# confidence.py — internal scorers + validators not directly unit-tested
# ===========================================================================

class TestTrendScore(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(C._trend_score({"trend": {"alignment": "mixed signals"}}), 0.4)

    def test_range(self):
        self.assertEqual(C._trend_score({"trend": {"alignment": "range-bound"}}), 0.55)

    def test_clean_uptrend_and_downtrend(self):
        self.assertEqual(C._trend_score({"trend": {"long_term_daily": "Uptrend"}}), 0.85)
        self.assertEqual(C._trend_score({"trend": {"long_term_daily": "Downtrend"}}), 0.85)

    def test_no_trend_is_neutral(self):
        self.assertEqual(C._trend_score({}), 0.5)


class TestMomentumScore(unittest.TestCase):
    def test_macd_cross_agreement_scores_high(self):
        a = {"hourly": {"macd": {"cross": "bullish"}}}
        self.assertAlmostEqual(C._momentum_score(a, {"direction": "long"}), 0.8, places=6)

    def test_macd_cross_disagreement_scores_low(self):
        a = {"hourly": {"macd": {"cross": "bullish"}}}
        self.assertAlmostEqual(C._momentum_score(a, {"direction": "short"}), 0.3, places=6)

    def test_no_directional_side_is_neutral(self):
        self.assertEqual(C._momentum_score({"hourly": {"rsi14": 70}}, {"direction": "wait"}), 0.5)


class TestStructureScore(unittest.TestCase):
    def test_inner_band_only(self):
        a = {"stats_last_sessions": {"close_inside_inner_band_pct": 80}}
        self.assertAlmostEqual(C._structure_score(a, None, None), 0.8, places=6)

    def test_no_signal_is_neutral(self):
        self.assertEqual(C._structure_score({}, None, None), 0.5)


class TestVolScore(unittest.TestCase):
    def test_atr_equals_median_is_max_normality(self):
        a = {"daily": {"atr14": 5.0}, "stats_last_sessions": {"median_session_range": 5.0}}
        self.assertAlmostEqual(C._vol_score(a), 1.0, places=6)   # 1.15 clamped to 1.0

    def test_expansion_lowers_score(self):
        a = {"daily": {"atr14": 10.0}, "stats_last_sessions": {"median_session_range": 5.0}}
        self.assertAlmostEqual(C._vol_score(a), 0.65, places=6)  # ratio 2 -> 1.15-0.5

    def test_realized_vol_fallback(self):
        self.assertAlmostEqual(C._vol_score({"daily": {"realized_vol_20d_pct": 20}}),
                               0.85, places=6)


class TestIsValidCalibMap(unittest.TestCase):
    def test_valid_isotonic_map(self):
        self.assertTrue(C._is_valid_calib_map({"knots": [[0, 0], [50, 60], [100, 100]]}))

    def test_non_dict_rejected(self):
        self.assertFalse(C._is_valid_calib_map([[0, 0], [100, 100]]))
        self.assertFalse(C._is_valid_calib_map(None))

    def test_too_few_or_malformed_knots(self):
        self.assertFalse(C._is_valid_calib_map({"knots": [[0, 0]]}))
        self.assertFalse(C._is_valid_calib_map({"knots": "nope"}))
        self.assertFalse(C._is_valid_calib_map({"knots": [[0, 0], [1, 2, 3]]}))
        self.assertFalse(C._is_valid_calib_map({}))

    def test_bool_values_rejected(self):
        # bool is a subclass of int — the validator must explicitly exclude it.
        self.assertFalse(C._is_valid_calib_map({"knots": [[True, 0], [100, 100]]}))

    def test_x_must_be_strictly_ascending(self):
        self.assertFalse(C._is_valid_calib_map({"knots": [[0, 0], [0, 50]]}))

    def test_y_must_be_non_decreasing(self):
        self.assertFalse(C._is_valid_calib_map({"knots": [[0, 50], [100, 40]]}))

    def test_out_of_range_rejected(self):
        self.assertFalse(C._is_valid_calib_map({"knots": [[0, 0], [101, 100]]}))
        self.assertFalse(C._is_valid_calib_map({"knots": [[-1, 0], [100, 100]]}))


class TestThesisSourceCap(unittest.TestCase):
    def test_single_source_is_65(self):
        self.assertEqual(C._thesis_source_cap(
            {"claims": [{"used_in_thesis": True, "status": "single-source"}]}), 65)

    def test_unverified_stale_unavailable_is_55(self):
        for st in ("unverified", "stale", "unavailable"):
            self.assertEqual(C._thesis_source_cap(
                {"claims": [{"used_in_thesis": True, "status": st}]}), 55)

    def test_two_single_source_stays_65(self):
        self.assertEqual(C._thesis_source_cap({"claims": [
            {"used_in_thesis": True, "status": "single-source"},
            {"used_in_thesis": True, "status": "single-source"}]}), 65)

    def test_non_thesis_weak_claim_ignored(self):
        self.assertIsNone(C._thesis_source_cap(
            {"claims": [{"used_in_thesis": False, "status": "single-source"}]}))

    def test_no_claims_or_none(self):
        self.assertIsNone(C._thesis_source_cap({}))
        self.assertIsNone(C._thesis_source_cap(None))


class TestInWindowEvent(unittest.TestCase):
    def test_requires_both_in_window_and_gap_risk(self):
        self.assertTrue(C._in_window_event({"catalysts": [{"in_window": True, "gap_risk": True}]}))
        self.assertFalse(C._in_window_event({"catalysts": [{"in_window": True, "gap_risk": False}]}))
        self.assertFalse(C._in_window_event({"catalysts": [{"in_window": False, "gap_risk": True}]}))

    def test_empty_or_none(self):
        self.assertFalse(C._in_window_event({}))
        self.assertFalse(C._in_window_event(None))


class TestClaimTraced(unittest.TestCase):
    def test_url_substring_match(self):
        claim = {"source": "https://cnbc.com/a"}
        pack = {"items": [{"url": "https://cnbc.com/a"}]}
        self.assertTrue(C._claim_traced(claim, pack))

    def test_no_claim_source_is_false(self):
        self.assertFalse(C._claim_traced({}, {"items": [{"url": "https://x.com"}]}))

    def test_empty_pack_is_false(self):
        self.assertFalse(C._claim_traced({"source": "https://x.com"}, {"items": []}))


class TestHypeThesis(unittest.TestCase):
    def test_high_hype_drives_thesis(self):
        sp = {"aggregate": {"hype_risk": "high"}}
        self.assertTrue(C._hype_thesis({"social_context": {"drives_thesis": True}}, sp))

    def test_high_hype_but_not_driving(self):
        sp = {"aggregate": {"hype_risk": "high"}}
        self.assertFalse(C._hype_thesis({"social_context": {"drives_thesis": False}}, sp))

    def test_no_social_is_false(self):
        self.assertFalse(C._hype_thesis({"social_context": {"drives_thesis": True}}, None))


class TestSocialNotes(unittest.TestCase):
    def test_high_hype_note(self):
        pen, detail = C.social_adjustment({"aggregate": {"hype_risk": "high"}})
        self.assertEqual(pen, -5.0)
        self.assertIn("high hype risk", detail["notes"])

    def test_medium_hype_no_note(self):
        pen, detail = C.social_adjustment({"aggregate": {"hype_risk": "medium"}})
        self.assertEqual(pen, -2.0)
        self.assertEqual(detail["notes"], [])


class TestCatalystGaps(unittest.TestCase):
    def test_source_gaps_only_reduce_score(self):
        score, detail = C.catalyst_confidence({"source_gaps": ["a", "b"]})
        self.assertAlmostEqual(score, 0.7, places=6)   # 1.0 - 0.15*2
        self.assertEqual(detail["source_gaps"], 2)


# ===========================================================================
# score_report.py — gaps: expect=False grading, corrupt-row skip, calibration
# skip, _prediction_text, _write_scored_sidecar, short setup, parse_args errors
# ===========================================================================

def _bars(seq):
    return [{"t": datetime(2026, 6, 12, h, tzinfo=timezone.utc),
             "o": o, "h": hi, "l": lo, "c": c}
            for h, (o, hi, lo, c) in enumerate(seq)]


class TestExpectFalseGrading(unittest.TestCase):
    """V2 briefs phrase bearish/neutral calls as expect=False — the verdict must
    compare the raw condition to `expect`, not return the raw condition."""

    def setUp(self):
        self.bars = _bars([(10, 12, 9, 11), (11, 13, 10, 12), (12, 14, 11, 10)])

    def test_close_above_expect_false(self):
        # last close = 10. raw(10>9)=True ; expect False -> mismatch -> N
        self.assertEqual(S.score_prediction(
            {"type": "close_above", "level": 9, "expect": False}, self.bars), "N")
        # raw(10>11)=False ; expect False -> match -> Y
        self.assertEqual(S.score_prediction(
            {"type": "close_above", "level": 11, "expect": False}, self.bars), "Y")

    def test_touches_expect_false(self):
        # never reaches 99 -> raw False ; expect False -> Y ("not touched" came true)
        self.assertEqual(S.score_prediction(
            {"type": "touches", "level": 99, "expect": False}, self.bars), "Y")
        # 13.5 IS in range -> raw True ; expect False -> N
        self.assertEqual(S.score_prediction(
            {"type": "touches", "level": 13.5, "expect": False}, self.bars), "N")

    def test_expect_default_true_unchanged(self):
        # legacy predictions (no expect) behave exactly like raw-return
        self.assertEqual(S.score_prediction(
            {"type": "close_above", "level": 9}, self.bars), "Y")


class TestLoadBarsCorruptRows(unittest.TestCase):
    def _write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_skips_nonnumeric_and_short_rows(self):
        path = self._write([
            "Datetime,Open,High,Low,Close,Volume",     # header (col0 not 2 digits) -> skipped
            "2026-06-12 09:00,10,11,9,10,100",          # good
            "2026-06-12 10:00,oops,11,9,bad,100",       # non-numeric OHLC -> skipped, not a crash
            "2026-06-12 11:00,10,12",                   # too few cols -> skipped
            "2026-06-12 12:00,10,12,10,11,100",         # good
        ])
        bars = S.load_bars(path, S.parse_dt("2026-06-12 09:00"), S.parse_dt("2026-06-12 13:00"))
        self.assertEqual(len(bars), 2)
        self.assertEqual([b["c"] for b in bars], [10, 11])

    def test_bom_prefixed_file_parses(self):
        # utf-8-sig open must strip a leading BOM so the first data row's date is read.
        path = self._write(["﻿2026-06-12 09:00,10,11,9,10,100"])
        bars = S.load_bars(path, S.parse_dt("2026-06-12 08:00"), S.parse_dt("2026-06-12 13:00"))
        self.assertEqual(len(bars), 1)


class TestCalibrationSkipsUnparseable(unittest.TestCase):
    def test_blank_confidence_rows_excluded_from_buckets(self):
        rows = ([{"confidence": "60", "hits": "1", "misses": "0"}] * 8
                + [{"confidence": "", "hits": "0", "misses": "0"}] * 2)
        cal = S.calibration(rows)
        self.assertIsNotNone(cal)
        self.assertEqual(cal["n_reports"], 10)            # all rows counted in the header total
        self.assertEqual(cal["buckets"]["<=60"]["reports"], 8)   # blanks not bucketed
        self.assertNotIn("61-75", cal["buckets"])
        self.assertNotIn(">75", cal["buckets"])


class TestPredictionText(unittest.TestCase):
    def test_manual_uses_note_then_criteria(self):
        self.assertEqual(S._prediction_text({"type": "manual", "note": "GDP shock"}), "GDP shock")
        self.assertEqual(S._prediction_text({"type": "manual", "criteria": "fallback crit"}),
                         "fallback crit")

    def test_explicit_text_wins_for_auto(self):
        self.assertEqual(S._prediction_text(
            {"type": "close_above", "level": 100, "text": "settles above PP"}),
            "settles above PP")

    def test_typed_condition_rendering(self):
        txt = S._prediction_text({"type": "close_above", "level": 100, "expect": True})
        self.assertEqual(txt, "close_above level=100 expect=True")

    def test_range_inside_lo_hi_rendered(self):
        txt = S._prediction_text({"type": "range_inside", "lo": 10, "hi": 20})
        self.assertIn("lo=10", txt)
        self.assertIn("hi=20", txt)


class TestWriteScoredSidecar(unittest.TestCase):
    def setUp(self):
        self._orig = S.SCORED_DIR
        self._dir = tempfile.mkdtemp()
        from pathlib import Path
        S.SCORED_DIR = Path(self._dir)

    def tearDown(self):
        S.SCORED_DIR = self._orig
        __import__("shutil").rmtree(self._dir, ignore_errors=True)

    def test_entries_in_order_with_outcomes(self):
        preds = [{"id": "P1", "type": "close_above", "level": 1},
                 {"id": "P5", "type": "manual", "note": "x"}]
        S._write_scored_sidecar("AF-20260612-AAA", preds, {"P1": "Y", "P5": "MANUAL"})
        with open(os.path.join(self._dir, "AF-20260612-AAA.json"), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual([e["pred_id"] for e in data], ["P1", "P5"])
        self.assertEqual([e["sort"] for e in data], [0, 1])
        self.assertEqual(data[0]["outcome"], "Y")
        self.assertFalse(data[0]["manual"])
        self.assertTrue(data[1]["manual"])
        self.assertEqual(data[1]["outcome"], "MANUAL")

    def test_unresolved_manual_outcome_is_manual(self):
        preds = [{"id": "P9", "type": "manual", "note": "n"}]
        S._write_scored_sidecar("rid", preds, {})        # results has no P9
        data = json.loads(open(os.path.join(self._dir, "rid.json"), encoding="utf-8").read())
        self.assertEqual(data[0]["outcome"], "MANUAL")

    def test_report_id_is_basenamed_for_path_safety(self):
        S._write_scored_sidecar("../../evil", [{"id": "P1", "type": "manual"}], {})
        # the traversal components are stripped; only 'evil.json' is written under SCORED_DIR
        self.assertTrue(os.path.exists(os.path.join(self._dir, "evil.json")))


class TestScoreSetupShortSide(unittest.TestCase):
    def test_short_t1_first(self):
        # short fills when high >= entry_lo, then resolves on l<=t1 (down) vs h>=inval (up)
        bars = _bars([(99, 100, 98, 99), (96, 97, 94, 95)])
        s = {"direction": "short", "entry_lo": 100, "entry_hi": 101,
             "invalidation": 105, "t1": 95}
        self.assertEqual(S.score_setup(s, bars), ("yes", "t1-first"))

    def test_short_invalidation_first(self):
        bars = _bars([(99, 100, 98, 99), (104, 106, 103, 105)])
        s = {"direction": "short", "entry_lo": 100, "entry_hi": 101,
             "invalidation": 105, "t1": 90}
        self.assertEqual(S.score_setup(s, bars), ("yes", "invalidation-first"))


class TestParseArgsErrors(unittest.TestCase):
    def test_unknown_arg_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            S.parse_args(["--frobnicate"])
        self.assertEqual(cm.exception.code, 2)

    def test_hourly_without_value_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            S.parse_args(["--hourly"])
        self.assertEqual(cm.exception.code, 2)

    def test_flags_combine(self):
        opts = S.parse_args(["--force", "--force-rescore", "--dry-run", "--hourly", "x.csv"])
        self.assertTrue(opts["force"])
        self.assertTrue(opts["force_rescore"])
        self.assertTrue(opts["dry_run"])
        self.assertEqual(opts["hourly"], "x.csv")


# ===========================================================================
# scaffold_payload.py — gaps: _period_stamp, read_last_bar, _quality,
# _lookback, _claims status normalization
# ===========================================================================

class TestPeriodStamp(unittest.TestCase):
    def test_daily_live(self):
        self.assertEqual(SP._period_stamp(None, "2026-06-23 14:00", None), "20260623")
        self.assertEqual(SP._period_stamp("daily", "2026-06-23 14:00", None), "20260623")

    def test_daily_backdated_embeds_time(self):
        as_of = datetime(2026, 6, 23, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(SP._period_stamp("daily", "2026-06-23 14:30", as_of), "202606231430")

    def test_weekly_is_iso_week(self):
        stamp = SP._period_stamp("weekly", "2026-06-23 00:00", None)
        iso = datetime(2026, 6, 23).isocalendar()
        self.assertEqual(stamp, f"{iso[0]}W{iso[1]:02d}")
        self.assertRegex(stamp, r"^\d{4}W\d{2}$")

    def test_monthly_is_year_month(self):
        self.assertEqual(SP._period_stamp("monthly", "2026-06-23 00:00", None), "202606")


class TestReadLastBar(unittest.TestCase):
    def _write(self, lines):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        self.addCleanup(os.remove, path)
        return path

    def test_returns_last_close_and_timestamp(self):
        path = self._write([
            "Datetime,Open,High,Low,Close,Volume",
            "2026-06-12 09:00,10,11,9,10.0,100",
            "2026-06-12 10:00,10,12,10,11.25,100",
        ])
        close, ts = SP.read_last_bar(path)
        self.assertEqual(close, 11.25)
        self.assertEqual(ts, "2026-06-12 10:00")

    def test_no_data_rows_dies(self):
        path = self._write(["Datetime,Open,High,Low,Close,Volume"])
        with self.assertRaises(SystemExit) as cm:
            SP.read_last_bar(path)
        self.assertEqual(cm.exception.code, 2)


class TestQualityNormalization(unittest.TestCase):
    def test_none_defaults_acceptable(self):
        self.assertEqual(SP._quality(None), "Acceptable")
        self.assertEqual(SP._quality(""), "Acceptable")

    def test_valid_passthrough(self):
        self.assertEqual(SP._quality("High quality"), "High quality")
        self.assertEqual(SP._quality("No-trade"), "No-trade")

    def test_invalid_falls_back_to_acceptable(self):
        self.assertEqual(SP._quality("Amazing"), "Acceptable")


class TestLookback(unittest.TestCase):
    def test_shapes_shown_and_fetched(self):
        out = SP._lookback({"windows": {"daily_display": "1y", "daily_fetched": "2y",
                                        "hourly_display": "10d", "hourly_fetched": "60d"}})
        self.assertEqual(out["daily"], "1y shown / 2y fetched")
        self.assertEqual(out["intraday"], "10d shown / 60d fetched")

    def test_missing_windows_blank(self):
        out = SP._lookback({})
        self.assertIn("shown", out["daily"])


class TestClaimsNormalization(unittest.TestCase):
    def test_status_lowercased_and_validated(self):
        out = SP._claims([{"claim": "x", "status": "Confirmed", "source": "Reuters"}])
        self.assertEqual(out[0]["status"], "confirmed")
        self.assertFalse(out[0]["used_in_thesis"])

    def test_used_in_thesis_coerced_to_bool(self):
        out = SP._claims([{"claim": "x", "status": "multiple-source",
                           "used_in_thesis": 1, "source": "CNBC"}])
        self.assertIs(out[0]["used_in_thesis"], True)

    def test_defaults_for_missing_fields(self):
        out = SP._claims([{"status": "single-source"}])
        self.assertEqual(out[0]["claim"], "")
        self.assertEqual(out[0]["source"], "-")


class TestBuildPredictionsManual(unittest.TestCase):
    # build_predictions_spec direction branches are covered in test_audit_fixes; the
    # manual P6 emission (gated on brief.manual_prediction AND a canonical anchor) is not.
    def _by_id(self):
        return {"pp": {"value": 100}, "tail_lo": {"value": 95}, "tail_hi": {"value": 105},
                "r1": {"value": 103}, "r2": {"value": 106}, "s1": {"value": 97},
                "anchor": {"value": 99}}

    def test_manual_p6_emitted_when_present(self):
        preds, _lv = SP.build_predictions_spec(self._by_id(),
                                               {"manual_prediction": "GDP > 0.3%"}, "neutral")
        p6 = [p for p in preds if p["id"] == "P6"]
        self.assertEqual(len(p6), 1)
        self.assertEqual(p6[0]["type"], "manual")
        self.assertEqual(p6[0]["note"], "GDP > 0.3%")

    def test_no_manual_no_p6(self):
        preds, _lv = SP.build_predictions_spec(self._by_id(), {}, "neutral")
        self.assertFalse(any(p["id"] == "P6" for p in preds))


# ===========================================================================
# taxonomy.py — derive/normalize/futures branches not exercised elsewhere
# ===========================================================================

class TestTaxonomyExtraBranches(unittest.TestCase):
    def test_derive_regime_from_intraday_range(self):
        # 'range' detected via intraday_hourly even when alignment doesn't mention it
        a = {"trend": {"intraday_hourly": "Range day", "long_term_daily": "Uptrend"}}
        self.assertEqual(T.derive_market_regime(a), "range")

    def test_derive_trend_down(self):
        a = {"trend": {"long_term_daily": "Downtrend", "alignment": "aligned down"}}
        self.assertEqual(T.derive_market_regime(a), "trend_down")

    def test_normalize_none_text_uses_derived(self):
        a = {"trend": {"long_term_daily": "Uptrend", "alignment": "aligned"}}
        self.assertEqual(T.normalize_market_regime(None, a), "trend_up")

    def test_normalize_break_out_alias(self):
        self.assertEqual(T.normalize_market_regime("break out incoming", {}), "breakout")

    def test_futures_caret_root_stripped(self):
        # '^' index prefix is stripped before matching the index-futures roots
        self.assertEqual(T.asset_class_key("cme_futures", "^FTSE"), "index")

    def test_futures_brent_is_commodity(self):
        self.assertEqual(T.asset_class_key("cme_futures", "BZ=F"), "commodity")


class ReadLastBarGuard(unittest.TestCase):
    def test_non_numeric_close_dies_cleanly(self):
        # regression: a corrupt last bar must fail QA (die -> SystemExit), not raise a raw ValueError
        import os
        import tempfile
        import scaffold_payload as SP
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write("2026-06-17 20:00,1.0,2.0,0.5,notanumber\n")
            with self.assertRaises(SystemExit):
                SP.read_last_bar(p)
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
