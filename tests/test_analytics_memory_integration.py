"""Phase 2 INTEGRATION tests for scripts/analytics/memory/* — the ledger-derived memory layer
WIRED TOGETHER, not isolated functions.

Flow under test (the real production path that feeds the brief writer / critic):

    real outcome-ledger CSV
        -> _ledger_io.load_rows            (one no-look-ahead read, attaches _hits/_misses/_ticker/_wend)
        -> ledger_context.build_context    (per-INSTRUMENT priors as-of a date)
        -> research_memory.build_memory     (cross-INSTRUMENT memory as-of the same date)
        -> memory_pack.build_pack           (merges global + asset_class + instrument into one bounded pack)

These assert the CROSS-MODULE data contracts + the no-look-ahead invariant that only emerges when the
modules combine — NOT the per-function behaviour already covered by tests/test_analytics_memory_unit.py
and tests/test_ledger_context.py. Everything in-process is real; the ONLY fakes are temp CSV/JSON files
(no network / Neon / Anthropic / R2 / subprocess). Every build_pack call passes an EXPLICIT ledger path
so the repo's real ledger/outcome_ledger.csv can never make a result non-deterministic.

The ledger row format mirrors production exactly: the 20 LEDGER_COLS written by score_report.py and
report_id "ADV-YYYYMMDD-TICKER" (e.g. the real "ADV-20260612-GBPJPY"), so _ticker = ticker_of(report_id).

Run:  python -m pytest tests/test_analytics_memory_integration.py -q
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _ledger_io as IO
import ledger_context as LC
import research_memory as RM
import memory_pack as MP

# Exactly the columns score_report.py's LEDGER_COLS writes (verified against the real ledger header).
COLS = ["scored_at_utc", "report_id", "instrument", "view", "confidence",
        "window_end_utc", "results", "hits", "misses", "hit_rate_pct",
        "setup_filled", "setup_outcome", "partial", "conf_version", "conf_raw",
        "asset_class", "pred_type", "direction", "horizon", "market_regime"]


def _row(report_id, wend, hits, misses, instrument="Apple Inc.", asset_class="equity",
         pred_type="breakout", direction="bullish", regime="trend_up"):
    return {
        "scored_at_utc": "2026-01-01 00:00", "report_id": report_id, "instrument": instrument,
        "view": "x", "confidence": "60", "window_end_utc": wend, "results": "P1=Y",
        "hits": str(hits), "misses": str(misses), "hit_rate_pct": "50", "setup_filled": "no",
        "setup_outcome": "n/a", "partial": "no", "conf_version": "2", "conf_raw": "60",
        "asset_class": asset_class, "pred_type": pred_type, "direction": direction,
        "horizon": "next_session", "market_regime": regime,
    }


class _LedgerTmp(unittest.TestCase):
    def _write_ledger(self, rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        self.addCleanup(os.remove, path)
        return path


# A small, realistic mixed-instrument ledger reused across the main flow tests.
#   AAPL (equity): 4 scored rows visible before AS_OF -> 12 hits / 4 misses = 75.0%
#   MSFT (equity): 1 scored row  visible -> 2 hits / 2 misses
#   BTC  (crypto): 1 scored row  visible -> 0 hits / 4 misses (a different class, drags GLOBAL below class)
#   + a boundary row exactly AT as_of and a future row that BOTH must be excluded (strict <).
AS_OF = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)


def _mixed_rows():
    return [
        _row("ADV-20260601-AAPL", "2026-06-01 20:00", 4, 0),                 # W (100%)
        _row("ADV-20260602-AAPL", "2026-06-02 20:00", 3, 1),                 # W (75%)
        _row("ADV-20260603-AAPL", "2026-06-03 20:00", 1, 3),                 # L (25%)
        _row("ADV-20260604-AAPL", "2026-06-04 20:00", 4, 0),                 # W (100%)
        _row("ADV-20260601-MSFT", "2026-06-01 20:00", 2, 2, instrument="Microsoft"),
        _row("ADV-20260601-BTC", "2026-06-01 20:00", 0, 4, instrument="Bitcoin",
             asset_class="crypto", pred_type="trend_follow", direction="bearish", regime="trend_down"),
        # window_end EXACTLY at as_of -> excluded by the strict `< as_of` no-look-ahead rule
        _row("ADV-20260605-AAPL", "2026-06-05 00:00", 4, 0),
        # window closes AFTER as_of -> a big loss that must NOT leak backward into the prior
        _row("ADV-20260610-AAPL", "2026-06-10 20:00", 0, 4),
    ]


class TestLedgerToPackFlow(_LedgerTmp):
    """The full load_rows -> build_context/build_memory -> build_pack pipeline on one ledger."""

    def setUp(self):
        self.ledger = self._write_ledger(_mixed_rows())
        self.asset = {"instrument": "Apple Inc.", "ticker": "AAPL", "asset_class": "equity"}
        self.pack = MP.build_pack(self.asset, as_of=AS_OF, ledger=self.ledger)

    def test_load_rows_no_look_ahead_feeds_the_whole_pack(self):
        # The shared read drops the == as_of boundary row AND the future row; everything downstream
        # therefore sees exactly 6 rows. This is the contract every layer inherits.
        loaded = IO.load_rows(self.ledger, AS_OF)
        self.assertEqual([r["report_id"] for r in loaded],
                         ["ADV-20260601-AAPL", "ADV-20260601-MSFT", "ADV-20260601-BTC",
                          "ADV-20260602-AAPL", "ADV-20260603-AAPL", "ADV-20260604-AAPL"])
        # rows arrive oldest-first and carry the derived keys the downstream modules rely on
        self.assertTrue(all({"_hits", "_misses", "_ticker", "_wend"} <= set(r) for r in loaded))
        self.assertEqual(self.pack["global"]["total_scored_reports"], len(loaded))

    def test_three_layers_aggregate_at_distinct_scopes_from_one_row_set(self):
        # The same 6 filtered rows roll up THREE different ways. Three distinct numbers prove the
        # instrument (LC), asset_class (RM by_asset_class) and global (RM overall) layers are each
        # scoped correctly and not accidentally sharing a denominator.
        inst = self.pack["instrument_history"]
        cls = self.pack["asset_class_history"]
        glob = self.pack["global"]
        self.assertEqual((inst["hit_rate_pct"], inst["reports"]), (75.0, 4))   # AAPL only:   12/16
        self.assertEqual((cls["hit_rate_pct"], cls["reports"]), (70.0, 5))     # all equity:  14/20
        self.assertEqual((glob["overall_hit_rate_pct"], glob["total_scored_reports"]), (58.3, 6))
        # sanity: the three layers are genuinely different scopes
        self.assertEqual(IO.rate(12, 4), inst["hit_rate_pct"])
        self.assertEqual(IO.rate(14, 6), cls["hit_rate_pct"])
        self.assertEqual(IO.rate(14, 10), glob["overall_hit_rate_pct"])

    def test_build_pack_matches_a_hand_wired_pipeline(self):
        # build_pack must produce the SAME instrument layer as manually chaining the real modules
        # (load_rows -> build_context) on the same inputs — i.e. no silent field/shape drift inside.
        rows = IO.load_rows(self.ledger, AS_OF)
        ctx = LC.build_context(self.asset["instrument"], rows,
                               ticker=self.asset["ticker"], asset_class=self.asset["asset_class"])
        inst = self.pack["instrument_history"]
        self.assertEqual(inst["hit_rate_pct"], ctx["instrument_hit_rate"])
        self.assertEqual(inst["reports"], ctx["historical_prediction_count"])
        self.assertEqual(inst["prediction_type_hit_rates"], ctx["prediction_type_hit_rates"])
        self.assertEqual(inst["recent_streak"], ctx["recent_streak"])
        self.assertEqual(self.pack["instrument"], ctx["instrument"])
        self.assertEqual(self.pack["ticker"], ctx["ticker"])

    def test_instrument_type_rates_and_streak_reflect_only_visible_rows(self):
        inst = self.pack["instrument_history"]
        # AAPL is entirely 'breakout': 12 hits / 4 misses = 75.0 (the post-as_of loss is absent)
        self.assertEqual(inst["prediction_type_hit_rates"], {"breakout": 75.0})
        # per-report outcomes oldest->newest: W,W,L,W  => current run is a single W
        self.assertEqual(inst["recent_streak"]["direction"], "W")
        self.assertEqual(inst["recent_streak"]["length"], 1)
        self.assertEqual(inst["recent_streak"]["recent_results"], [100.0, 75.0, 25.0, 100.0])

    def test_asset_class_best_pattern_is_scoped_to_this_class_only(self):
        # RM ranks best patterns across EVERY dimension (pred_type/regime/direction/asset_class/cross);
        # memory_pack._class_rel must surface ONLY the asset_class==equity row for an equity pack.
        best = self.pack["asset_class_history"]["best"]
        self.assertTrue(best, "a 70% equity class at n=5 should surface as a best pattern")
        for p in best:
            self.assertEqual(p["dimension"], "asset_class")
            self.assertEqual(p["pattern"], "equity")
        self.assertEqual(self.pack["asset_class_history"]["worst"], [])  # nothing <50% at n>=4

    def test_lessons_carry_the_instrument_display_name_from_the_asset_dict(self):
        # build_pack passes name=asset['instrument'] into build_context, so the note text must use the
        # LONG display name, not the ticker — a contract the brief writer depends on.
        self.assertEqual(self.pack["lessons_for_ai"],
                         ["Apple Inc.: 4 scored report(s), 75.0% hit rate to date."])

    def test_pack_is_within_token_budget(self):
        b = self.pack["budget"]
        self.assertTrue(b["within_budget"])
        self.assertLessEqual(b["approx_tokens"], b["limit"])
        self.assertEqual(b["limit"], MP.TOKEN_BUDGET)


class TestNoLookAheadIsEndToEnd(_LedgerTmp):
    """Moving `as_of` past a row changes the PRIOR — proves as_of flows build_pack -> load_rows ->
    every layer, not just that load_rows filters in isolation."""

    def setUp(self):
        self.ledger = self._write_ledger(_mixed_rows())
        self.asset = {"instrument": "Apple Inc.", "ticker": "AAPL", "asset_class": "equity"}

    def test_prior_shifts_when_as_of_admits_later_rows(self):
        early = MP.build_pack(self.asset, as_of=AS_OF, ledger=self.ledger)
        # as_of after 2026-06-10 admits the boundary row (06-05 00:00, 4/0) AND the future loss (0/4):
        # AAPL becomes 6 rows -> 16 hits / 8 misses = 66.7% over 6 reports.
        late = MP.build_pack(self.asset, as_of=datetime(2026, 6, 11, tzinfo=timezone.utc),
                             ledger=self.ledger)
        self.assertEqual((early["instrument_history"]["hit_rate_pct"],
                          early["instrument_history"]["reports"]), (75.0, 4))
        self.assertEqual((late["instrument_history"]["hit_rate_pct"],
                          late["instrument_history"]["reports"]), (66.7, 6))
        self.assertNotEqual(early["instrument_history"]["hit_rate_pct"],
                            late["instrument_history"]["hit_rate_pct"])

    def test_naive_as_of_is_treated_as_utc_and_still_filters(self):
        # a NAIVE as_of must be coerced to UTC by build_pack and then used in the strict comparison;
        # 2026-06-05 (naive) still excludes the 06-10 future loss -> prior stays 75.0/4.
        pack = MP.build_pack(self.asset, as_of=datetime(2026, 6, 5), ledger=self.ledger)
        self.assertTrue(pack["as_of_utc"].endswith("UTC"))
        self.assertEqual((pack["instrument_history"]["hit_rate_pct"],
                          pack["instrument_history"]["reports"]), (75.0, 4))


class TestExactTickerContractThroughPack(_LedgerTmp):
    """The exact-ticker instrument match must survive the whole pipeline: a substring collision
    (querying 'ES' against a 'TESLA' row) must NOT leak into the instrument layer of the pack."""

    def test_substring_ticker_does_not_leak_into_instrument_history(self):
        ledger = self._write_ledger([
            _row("ADV-20260601-TESLA", "2026-06-01 20:00", 2, 0, instrument="Tesla"),
        ])
        pack = MP.build_pack({"instrument": "S&P 500 E-mini", "ticker": "ES", "asset_class": "index"},
                             as_of=datetime(2030, 1, 1, tzinfo=timezone.utc), ledger=ledger)
        # 'ES' is a substring of 'TESLA' but ticker_of('ADV-20260601-TESLA') == 'TESLA' != 'ES'
        self.assertEqual(pack["instrument_history"]["reports"], 0)
        self.assertIsNone(pack["instrument_history"]["hit_rate_pct"])
        # and the row's class (equity) must not bleed into an 'index' pack's class layer
        self.assertEqual(pack["asset_class_history"]["reports"], 0)
        self.assertIsNone(pack["asset_class_history"]["hit_rate_pct"])
        # but it is still counted GLOBALLY (overall hit rate spans all instruments)
        self.assertEqual(pack["global"]["total_scored_reports"], 1)


class TestCalibrationMapFlowsIntoGlobalLayer(_LedgerTmp):
    """ledger/calibration_map.json (written elsewhere in the pipeline) must surface through
    memory_pack._load into the pack's global.calibration block under the method/shrinkage_w/n_rows
    keys the brief consumes."""

    def test_calibration_block_is_populated_from_file(self):
        fd, cp = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, cp)
        Path(cp).write_text(json.dumps({"method": "beta_binomial", "shrinkage_w": 10,
                                        "n_rows": 42, "ignored_extra": "x"}),
                            encoding="utf-8")
        orig = MP.CALIB_MAP
        MP.CALIB_MAP = Path(cp)
        self.addCleanup(lambda: setattr(MP, "CALIB_MAP", orig))

        ledger = self._write_ledger([_row("ADV-20260601-AAPL", "2026-06-01 20:00", 4, 0)])
        pack = MP.build_pack({"instrument": "Apple Inc.", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=datetime(2030, 1, 1, tzinfo=timezone.utc), ledger=ledger)
        self.assertEqual(pack["global"]["calibration"],
                         {"method": "beta_binomial", "shrinkage_w": 10, "n_rows": 42})

    def test_missing_calibration_map_degrades_to_null_block(self):
        # point at a path that does not exist -> _load returns None -> all-None block, pack still builds
        orig = MP.CALIB_MAP
        MP.CALIB_MAP = Path(tempfile.gettempdir()) / "assetframe_no_such_calib_map.json"
        self.addCleanup(lambda: setattr(MP, "CALIB_MAP", orig))
        ledger = self._write_ledger([_row("ADV-20260601-AAPL", "2026-06-01 20:00", 4, 0)])
        pack = MP.build_pack({"instrument": "Apple Inc.", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=datetime(2030, 1, 1, tzinfo=timezone.utc), ledger=ledger)
        self.assertEqual(pack["global"]["calibration"],
                         {"method": None, "shrinkage_w": None, "n_rows": None})


class TestEmptyLedgerDegradesGracefullyThroughPack(_LedgerTmp):
    """Day-one: an empty/young ledger must still yield a valid, bounded, neutral pack."""

    def test_empty_ledger_neutral_pack(self):
        ledger = self._write_ledger([])   # header only, zero rows
        pack = MP.build_pack({"instrument": "Apple Inc.", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=AS_OF, ledger=ledger)
        self.assertEqual(pack["global"]["total_scored_reports"], 0)
        self.assertIsNone(pack["global"]["overall_hit_rate_pct"])
        self.assertEqual(pack["instrument_history"]["reports"], 0)
        self.assertIsNone(pack["instrument_history"]["hit_rate_pct"])
        self.assertEqual(pack["asset_class_history"]["best"], [])
        self.assertTrue(pack["budget"]["within_budget"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
