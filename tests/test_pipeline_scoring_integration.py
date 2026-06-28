"""INTEGRATION tests for scripts/pipeline/scoring/* — the REAL modules of the scoring
directory WIRED TOGETHER (scaffold_payload -> taxonomy + confidence + sessions ->
predictions.json -> score_report -> outcome ledger), exercising the CROSS-MODULE
DATA CONTRACTS rather than isolated functions (those live in test_pipeline_scoring_unit.py).

THE core chain under test:
  1. A fixture engine analysis (tests/test_fixtures/BTC_analysis.json) + the real
     research brief (BTC_research_brief.json) are compiled by scaffold_payload.main()
     into a canonical payload AND a predictions file. This invokes the REAL
     build_levels / build_setups / build_predictions_spec / compute_confidence /
     taxonomy.build_taxonomy / sessions.get_window across module boundaries — no fakes.
  2. score_report.main() reads that exact predictions file and grades the typed
     predictions against a small SYNTHETIC hourly candle CSV we control, appending one
     row to ledger/outcome_ledger.csv.
  3. We assert the Y/N/NT/MANUAL verdicts AND the ledger row shape, and that the
     confidence + taxonomy scaffold computed flow byte-for-byte into the ledger.

Only true external boundaries would be faked — but THIS flow has none (sessions /
confidence / taxonomy are pure, stdlib-only). All file writes are redirected into a
per-test tmp working directory (cwd is chdir'd there and restored), so the real
data/ , ledger/ and reports/ trees are never touched. ASSETFRAME_SANDBOX is forced
off so the live (non-sim) code path is exercised.

Run:  python -m pytest tests/test_pipeline_scoring_integration.py -q
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scaffold_payload as SP
import score_report as S
import taxonomy as T
import confidence as C  # noqa: F401  (imported in-loop by scaffold; here to assert it's the real one)

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
FIX = HERE / "test_fixtures"


# A window safely in the past (so score_report scores it for real instead of previewing an
# open window) yet anchored relative to "now" so the suite is robust to the wall clock.
_BASE = (datetime.now(timezone.utc) - timedelta(days=10)).replace(
    hour=14, minute=0, second=0, microsecond=0)


def _ts(hours):
    return (_BASE + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M")


# Four synthetic hourly bars (o, h, l, c) stamped _BASE+0..+3h. The window [_BASE, _BASE+4h)
# includes all four (load_bars is start-inclusive, end-exclusive); tail gap is exactly one bar.
# BEARISH-CONFIRMED: price drifts below PP (64868.38), stays inside the outer bands
# (62042.10 .. 66342.28), never touches R1 (65698.29), holds the floor (63638.42).
BEARISH_BARS = [
    (64500, 64900, 64300, 64600),
    (64600, 64950, 64200, 64400),
    (64400, 64800, 64100, 64300),
    (64300, 64700, 64000, 64200),
]
# BEARISH-BROKEN (price rips up): closes above PP, breaks the outer band high, touches R1.
BULLISH_BARS = [
    (64800, 65200, 64700, 65100),
    (65100, 65900, 65000, 65800),
    (65800, 66500, 65700, 66400),
    (66400, 66600, 65900, 65000),
]

WINDOW_END = _ts(4)


class _ChainHarness(unittest.TestCase):
    """Builds a throwaway repo-shaped working dir, drops the fixtures + a synthetic CSV, and
    drives the REAL scaffold_payload.main() then score_report.main() against it."""

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="af_scoring_int_"))
        self._cwd = os.getcwd()
        # force the live (non-sandbox) path regardless of the ambient environment
        self._sb = os.environ.pop("ASSETFRAME_SANDBOX", None)
        for sub in ("data/analysis", "data/briefs", "data/candles",
                    "data/predictions", "data/payloads", "ledger"):
            (self.work / sub).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / "BTC_analysis.json", self.work / "data/analysis/BTC_analysis.json")

    def tearDown(self):
        os.chdir(self._cwd)
        if self._sb is not None:
            os.environ["ASSETFRAME_SANDBOX"] = self._sb
        shutil.rmtree(self.work, ignore_errors=True)

    # --- fixture authoring -------------------------------------------------
    def write_brief(self, overrides=None):
        brief = json.loads((FIX / "BTC_research_brief.json").read_text(encoding="utf-8-sig"))
        if overrides:
            brief.update(overrides)
        (self.work / "data/briefs/BTC_research_brief.json").write_text(
            json.dumps(brief), encoding="utf-8")

    def write_csv(self, bars, start_hour=0):
        path = self.work / "data/candles/BTC_hourly.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Datetime", "Open", "High", "Low", "Close", "Volume"])
            for i, (o, h, l, c) in enumerate(bars):
                w.writerow([_ts(start_hour + i), o, h, l, c, 1000])
        # a daily CSV must exist too (read_last_bar fallback if hourly were empty)
        shutil.copy(path, self.work / "data/candles/BTC_daily.csv")
        return path

    # --- module drivers (REAL main() of each) ------------------------------
    def scaffold(self, as_of, window_end=WINDOW_END, pred_out=None):
        argv = ["scaffold_payload.py", "BTC", "--session-profile", "crypto_24_7",
                "--as-of", as_of, "--window-end", window_end]
        if pred_out:
            argv += ["--predictions", pred_out]
        os.chdir(self.work)
        _saved = sys.argv
        try:
            sys.argv = argv               # scaffold_payload.main reads sys.argv directly
            SP.main()
        finally:
            sys.argv = _saved
            os.chdir(self._cwd)
        rel = pred_out or "data/predictions/BTC_predictions.json"
        return json.loads((self.work / rel).read_text(encoding="utf-8"))

    def score(self, pred_rel="data/predictions/BTC_predictions.json", extra=None):
        os.chdir(self.work)
        try:
            sys.argv = ["score_report.py", str(self.work / pred_rel)] + (extra or [])
            S.main()
        finally:
            os.chdir(self._cwd)

    def ledger_rows(self):
        led = self.work / "ledger/outcome_ledger.csv"
        if not led.exists():
            return []
        return list(csv.DictReader(led.read_text(encoding="utf-8").splitlines()))

    def results_map(self, row):
        """'P1=Y P2=Y ...' ledger cell -> {'P1':'Y', ...}."""
        return dict(tok.split("=", 1) for tok in row["results"].split())


# ===========================================================================
# 1. Full chain: thesis-confirmed verdicts + complete ledger row shape
# ===========================================================================

class TestThesisConfirmedChain(_ChainHarness):
    def test_verdicts_and_full_ledger_row(self):
        self.write_brief()
        self.write_csv(BEARISH_BARS)
        preds = self.scaffold(_ts(0))
        # scaffold emitted the V2 directional predictions for a genuine bear view
        ids = [p["id"] for p in preds["predictions"]]
        self.assertEqual(ids, ["P1", "P2", "P3", "P4", "P5", "P6"])

        self.score()
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1, "exactly one ledger row appended for one report")
        row = rows[0]
        verdicts = self.results_map(row)

        # bearish thesis HELD: settles below PP (Y), inside bands (Y), R1 untouched (Y),
        # floor held (Y), R1 never touched so the after-touch test is NT, manual stays MANUAL.
        self.assertEqual(verdicts, {"P1": "Y", "P2": "Y", "P3": "Y", "P4": "Y",
                                    "P5": "NT", "P6": "MANUAL"})
        # NT + MANUAL are excluded from the hit rate denominator
        self.assertEqual(row["hits"], "4")
        self.assertEqual(row["misses"], "0")
        self.assertEqual(row["hit_rate_pct"], "100.0")

        # ledger row shape: every declared column present with the cross-module-sourced value
        self.assertEqual(set(row.keys()), set(S.LEDGER_COLS))
        self.assertEqual(row["report_id"], preds["report_id"])
        self.assertEqual(row["report_id"], "AF-" + _BASE.strftime("%Y%m%d%H%M") + "-BTC")
        self.assertEqual(row["instrument"], "Bitcoin / US Dollar")
        self.assertEqual(row["window_end_utc"], WINDOW_END)
        self.assertEqual(row["partial"], "no")
        # taxonomy block (built by taxonomy.build_taxonomy in scaffold) lands in the ledger
        self.assertEqual(row["asset_class"], "crypto")
        self.assertEqual(row["pred_type"], "range_hold")
        self.assertEqual(row["direction"], "bearish")
        self.assertEqual(row["horizon"], "next_session")
        self.assertEqual(row["market_regime"], "trend_down")
        self.assertEqual(row["conf_version"], "2")


# ===========================================================================
# 2. Full chain: thesis-broken -> misses, incl. the expect=False -> N path
# ===========================================================================

class TestThesisBrokenChain(_ChainHarness):
    def test_bullish_surprise_produces_misses(self):
        self.write_brief()
        self.write_csv(BULLISH_BARS)
        self.scaffold(_ts(0))
        self.score()
        row = self.ledger_rows()[0]
        verdicts = self.results_map(row)

        # The bearish-phrased predictions (expect=False on P1 settle-below-PP and P3 R1-not-
        # touched) FAIL when price rips up: each raw condition is True but expect is False -> N.
        self.assertEqual(verdicts["P1"], "N")   # settles ABOVE PP -> "below PP" false
        self.assertEqual(verdicts["P2"], "N")   # breaks the outer band high
        self.assertEqual(verdicts["P3"], "N")   # R1 IS touched -> "not touched" false
        self.assertEqual(verdicts["P4"], "Y")   # floor still held
        self.assertEqual(verdicts["P5"], "Y")   # first R1 touch did not close above R2
        self.assertEqual(verdicts["P6"], "MANUAL")
        self.assertEqual(row["hits"], "2")
        self.assertEqual(row["misses"], "3")
        self.assertEqual(row["hit_rate_pct"], "40.0")


# ===========================================================================
# 3. Data contract: the `expect` field scaffold writes is what score_report grades
# ===========================================================================

class TestExpectFieldContract(_ChainHarness):
    def test_bearish_predictions_carry_expect_false_on_directional(self):
        self.write_brief()
        self.write_csv(BEARISH_BARS)
        preds = self.scaffold(_ts(0))
        by_id = {p["id"]: p for p in preds["predictions"]}
        # the cross-module V2 contract: a bear view phrases the directional calls as expect=False;
        # the symmetric range/floor/ceiling calls stay expect=True.
        self.assertIs(by_id["P1"]["expect"], False)   # "settles below PP"
        self.assertIs(by_id["P3"]["expect"], False)   # "R1 not touched"
        self.assertIs(by_id["P2"]["expect"], True)
        self.assertIs(by_id["P4"]["expect"], True)
        self.assertIs(by_id["P5"]["expect"], True)
        # P1 grades against a real bar set the SAME way score_report.main does it
        bars = S.load_bars(str(self.work / "data/candles/BTC_hourly.csv"),
                           S.parse_dt(preds["window_start_utc"]),
                           S.parse_dt(preds["window_end_utc"]))
        self.assertEqual(len(bars), 4)
        self.assertEqual(S.score_prediction(by_id["P1"], bars), "Y")  # close below PP held


# ===========================================================================
# 4. Direction branch: a neutral brief emits NO directional predictions, and that
#    propagates all the way into the ledger's direction column
# ===========================================================================

class TestNeutralBranchPropagation(_ChainHarness):
    def test_neutral_drops_p1_p3_and_tags_ledger_neutral(self):
        # neutral view: build_predictions_spec must NOT register P1 (settle-vs-PP) or P3 (R1 touch)
        self.write_brief({"directional_view": "neutral", "market_regime": "range",
                          "primary_prediction": {"type": "range_hold"}})
        self.write_csv(BULLISH_BARS)
        preds = self.scaffold(_ts(0))
        ids = [p["id"] for p in preds["predictions"]]
        self.assertNotIn("P1", ids)
        self.assertNotIn("P3", ids)
        self.assertEqual(ids, ["P2", "P4", "P5", "P6"])
        self.assertEqual(preds["taxonomy"]["direction"], "neutral")

        self.score()
        row = self.ledger_rows()[0]
        self.assertEqual(row["direction"], "neutral")
        self.assertEqual(row["market_regime"], "range")
        # only the symmetric predictions were graded
        self.assertEqual(set(self.results_map(row)), {"P2", "P4", "P5", "P6"})


# ===========================================================================
# 5. Confidence + taxonomy flow scaffold -> predictions.json -> ledger identically
# ===========================================================================

class TestConfidenceAndTaxonomyFlow(_ChainHarness):
    def test_published_confidence_and_taxonomy_match_end_to_end(self):
        self.write_brief()
        self.write_csv(BEARISH_BARS)
        preds = self.scaffold(_ts(0))
        self.score()
        row = self.ledger_rows()[0]

        # confidence is computed ONCE by confidence.compute_confidence in scaffold and must
        # appear unchanged in both the predictions file and the ledger (no recompute/drift).
        self.assertEqual(str(preds["confidence"]), row["confidence"])
        self.assertEqual(str(preds["conf_raw"]), row["conf_raw"])
        self.assertEqual(str(preds["conf_version"]), row["conf_version"])
        # and it is a sane published score in 0..100
        self.assertTrue(0 <= int(row["confidence"]) <= 100)

        # taxonomy validated by taxonomy.build_taxonomy is the same object the ledger stores
        tax = preds["taxonomy"]
        self.assertEqual(tax["asset_class"], row["asset_class"])
        self.assertEqual(tax["prediction_type"], row["pred_type"])
        self.assertEqual(tax["direction"], row["direction"])
        self.assertEqual(tax["horizon"], row["horizon"])
        self.assertEqual(tax["market_regime"], row["market_regime"])
        # every taxonomy value is a canonical member of its vocabulary (no free text leaked)
        self.assertIn(tax["prediction_type"], T.PREDICTION_TYPES)
        self.assertIn(tax["direction"], T.DIRECTIONS)
        self.assertIn(tax["horizon"], T.HORIZONS)
        self.assertIn(tax["asset_class"], T.ASSET_CLASS_KEYS)
        self.assertIn(tax["market_regime"], T.MARKET_REGIMES)


# ===========================================================================
# 6. Append-only ledger + report_id dedup across the real writer
# ===========================================================================

class TestLedgerAppendOnlyAndDedup(_ChainHarness):
    def test_two_reports_append_then_rescore_dedups(self):
        self.write_brief()
        self.write_csv(BEARISH_BARS)   # bars at +0..+3h; both windows below cover real bars

        # report A: window [+0h, +4h)  -> report_id stamp ...HHMM at +0h
        self.scaffold(_ts(0), pred_out="data/predictions/A.json")
        self.score("data/predictions/A.json")
        led = self.work / "ledger/outcome_ledger.csv"
        after_a = led.read_text(encoding="utf-8")
        self.assertEqual(len(self.ledger_rows()), 1)

        # report B: a DISTINCT as-of minute -> distinct report_id -> a second appended row
        self.scaffold(_ts(1), window_end=WINDOW_END, pred_out="data/predictions/B.json")
        self.score("data/predictions/B.json")
        after_b = led.read_text(encoding="utf-8")
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 2)
        # append-only: report A's bytes are a prefix of the post-B file, header preserved
        self.assertTrue(after_b.startswith(after_a))
        self.assertEqual(after_b.splitlines()[0].split(","), S.LEDGER_COLS)
        self.assertNotEqual(rows[0]["report_id"], rows[1]["report_id"])

        # re-scoring report A (same report_id) must be DEDUPED, not double-counted
        self.score("data/predictions/A.json")
        self.assertEqual(len(self.ledger_rows()), 2, "dedup: no duplicate row for an already-scored id")
        self.assertEqual(led.read_text(encoding="utf-8"), after_b, "ledger bytes unchanged on dedup")


# ===========================================================================
# 7. Manual prediction round-trips: scaffold emits it from the brief, the scorer
#    resolves it via --manual and it counts toward the hit rate
# ===========================================================================

class TestManualPredictionRoundTrip(_ChainHarness):
    def test_manual_p6_emitted_then_resolved_via_cli(self):
        self.write_brief()
        self.write_csv(BULLISH_BARS)
        preds = self.scaffold(_ts(0))
        p6 = next(p for p in preds["predictions"] if p["id"] == "P6")
        self.assertEqual(p6["type"], "manual")           # scaffold mapped brief.manual_prediction
        self.assertIn("note", p6)

        # validate_manual (score_report) accepts P6 because scaffold tagged it manual-type
        self.score(extra=["--manual", "P6=Y"])
        row = self.ledger_rows()[0]
        verdicts = self.results_map(row)
        self.assertEqual(verdicts["P6"], "Y")            # resolved, no longer MANUAL
        # bullish bars already give P4,P5 = Y; P6=Y makes 3 hits / 3 misses
        self.assertEqual(row["hits"], "3")
        self.assertEqual(row["misses"], "3")


# ===========================================================================
# 8. Structural invariant: every ledger row has exactly the declared columns and
#    the header is the canonical LEDGER_COLS (catches a column-count drift between
#    the writer's row list and the header).
# ===========================================================================

class TestLedgerRowStructuralInvariant(_ChainHarness):
    def test_row_field_count_matches_header(self):
        self.write_brief()
        self.write_csv(BEARISH_BARS)
        self.scaffold(_ts(0))
        self.score()
        raw = (self.work / "ledger/outcome_ledger.csv").read_text(encoding="utf-8")
        parsed = list(csv.reader(raw.splitlines()))
        self.assertEqual(parsed[0], S.LEDGER_COLS)
        for data_row in parsed[1:]:
            self.assertEqual(len(data_row), len(S.LEDGER_COLS),
                             "data row column count must equal the header width")


if __name__ == "__main__":
    unittest.main(verbosity=2)
