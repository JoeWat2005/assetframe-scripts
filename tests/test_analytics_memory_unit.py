"""Offline unit tests for scripts/analytics/memory/* — the ledger-derived memory layer.

Modules under test (all import flat via the package sys.path shim applied by conftest):
  _ledger_io        parse_dt / ticker_of / rate / load_rows   (shared no-look-ahead read)
  ledger_context    per-instrument context  (_agg/_type_breakdown/_streak/build_context/_patterns_and_notes/parse_args)
  research_memory   cross-instrument memory  (_breakdown/_cross_breakdown/_best_worst/_notes/build_memory/parse_args)
  memory_pack       token-bounded merge      (_approx_tokens/_load/build_pack + compaction)

These complement (do NOT duplicate) tests/test_ledger_context.py (load_rows no-look-ahead,
empty-context, basic breakdowns) and tests/test_scheduler.py (memory_pack neutral-without-history).
Everything here is deterministic and fully offline: the only I/O is temp CSV/JSON files; no
network / Neon / Anthropic / R2 / subprocess. Every build_pack call passes an EXPLICIT ledger path
so a real ledger/outcome_ledger.csv in the repo can never make the result non-deterministic.

Run:  python -m pytest tests/test_analytics_memory_unit.py -q
"""
import csv
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _ledger_io as IO
import ledger_context as LC
import research_memory as RM
import memory_pack as MP

FUTURE = datetime(2030, 1, 1, tzinfo=timezone.utc)

COLS = ["scored_at_utc", "report_id", "instrument", "view", "confidence",
        "window_end_utc", "results", "hits", "misses", "hit_rate_pct",
        "setup_filled", "setup_outcome", "partial", "conf_version", "conf_raw",
        "asset_class", "pred_type", "direction", "horizon", "market_regime"]


def _row(report_id, wend, hits, misses, instrument="Apple", asset_class="equity",
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
    """Base: write a temp ledger CSV that is auto-removed."""

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

    def _load(self, rows, as_of=FUTURE):
        return IO.load_rows(self._write_ledger(rows), as_of)


# --------------------------------------------------------------------------- #
# _ledger_io
# --------------------------------------------------------------------------- #
class TestParseDt(unittest.TestCase):
    def test_valid_minute_precision_is_utc(self):
        dt = IO.parse_dt("2026-06-10 20:00")
        self.assertEqual(dt, datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_seconds_and_trailing_text_truncated_to_16_chars(self):
        # only the first 16 chars ("YYYY-MM-DD HH:MM") are parsed
        self.assertEqual(IO.parse_dt("2026-06-10 20:00:59 stuff"),
                         datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))

    def test_leading_trailing_whitespace_stripped(self):
        self.assertEqual(IO.parse_dt("   2026-06-10 20:00   "),
                         datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))

    def test_malformed_returns_none(self):
        self.assertIsNone(IO.parse_dt("not-a-date"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(IO.parse_dt(""))

    def test_none_returns_none_via_attributeerror_guard(self):
        self.assertIsNone(IO.parse_dt(None))


class TestTickerOf(unittest.TestCase):
    def test_extracts_last_dash_segment_uppercased(self):
        self.assertEqual(IO.ticker_of("AF-20260610-aapl"), "AAPL")

    def test_strips_inner_whitespace(self):
        self.assertEqual(IO.ticker_of("  AF-x-tsla  "), "TSLA")

    def test_no_dash_returns_whole_string_upper(self):
        self.assertEqual(IO.ticker_of("nodash"), "NODASH")

    def test_none_returns_empty_string(self):
        self.assertEqual(IO.ticker_of(None), "")

    def test_empty_returns_empty(self):
        self.assertEqual(IO.ticker_of(""), "")


class TestRate(unittest.TestCase):
    def test_all_hits_is_100(self):
        self.assertEqual(IO.rate(2, 0), 100.0)

    def test_even_split_is_50(self):
        self.assertEqual(IO.rate(1, 1), 50.0)

    def test_zero_total_is_none(self):
        self.assertIsNone(IO.rate(0, 0))

    def test_rounds_to_one_decimal(self):
        self.assertEqual(IO.rate(1, 2), 33.3)


class TestLoadRowsEdgeCases(_LedgerTmp):
    def test_missing_file_returns_empty_list(self):
        self.assertEqual(IO.load_rows("does/not/exist.csv", FUTURE), [])

    def test_non_integer_hits_row_is_skipped(self):
        rows = self._load([
            _row("AF-1-AAPL", "2026-06-10 20:00", "abc", 0),   # int("abc") -> ValueError -> skip
            _row("AF-2-AAPL", "2026-06-11 20:00", 2, 0),
        ])
        self.assertEqual([r["report_id"] for r in rows], ["AF-2-AAPL"])

    def test_blank_hits_and_misses_default_to_zero(self):
        rows = self._load([_row("AF-1-AAPL", "2026-06-10 20:00", "", "")])
        self.assertEqual(rows[0]["_hits"], 0)
        self.assertEqual(rows[0]["_misses"], 0)

    def test_derived_keys_are_attached(self):
        rows = self._load([_row("AF-1-AAPL", "2026-06-10 20:00", 3, 1)])
        r = rows[0]
        self.assertEqual(r["_hits"], 3)
        self.assertEqual(r["_misses"], 1)
        self.assertEqual(r["_ticker"], "AAPL")
        self.assertEqual(r["_wend"], datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc))


# --------------------------------------------------------------------------- #
# ledger_context helpers
# --------------------------------------------------------------------------- #
class TestLedgerContextHelpers(unittest.TestCase):
    def test_agg_empty_is_zero_none(self):
        self.assertEqual(LC._agg([]), (0, None))

    def test_agg_sums_hits_and_misses(self):
        rows = [{"_hits": 3, "_misses": 1}, {"_hits": 1, "_misses": 1}]
        n, hr = LC._agg(rows)
        self.assertEqual(n, 2)
        self.assertEqual(hr, IO.rate(4, 2))   # 66.7

    def test_type_breakdown_skips_blank_pred_type(self):
        rows = [{"_hits": 3, "_misses": 1, "pred_type": "breakout"},
                {"_hits": 1, "_misses": 1, "pred_type": ""},
                {"_hits": 2, "_misses": 0, "pred_type": "pullback"}]
        rates, counts = LC._type_breakdown(rows)
        self.assertEqual(set(rates), {"breakout", "pullback"})
        self.assertEqual(counts, {"breakout": 1, "pullback": 1})
        self.assertEqual(rates["breakout"], 75.0)

    def test_streak_empty_rows(self):
        self.assertEqual(LC._streak([]),
                         {"direction": None, "length": 0, "recent_results": []})

    def test_streak_direction_is_most_recent_run(self):
        # oldest -> newest: W, W, L  => current run is a single L
        rows = [{"_hits": 2, "_misses": 0}, {"_hits": 2, "_misses": 0},
                {"_hits": 0, "_misses": 2}]
        st = LC._streak(rows, recent_k=2)
        self.assertEqual(st["direction"], "L")
        self.assertEqual(st["length"], 1)
        self.assertEqual(st["recent_results"], [100.0, 0.0])

    def test_streak_counts_consecutive_wins(self):
        rows = [{"_hits": 0, "_misses": 2}, {"_hits": 2, "_misses": 0},
                {"_hits": 2, "_misses": 0}]
        st = LC._streak(rows)
        self.assertEqual((st["direction"], st["length"]), ("W", 2))

    def test_streak_tie_outcome_classified_as_T(self):
        rows = [{"_hits": 1, "_misses": 1}]
        self.assertEqual(LC._streak(rows)["direction"], "T")


class TestLedgerContextBuildContext(_LedgerTmp):
    def test_ticker_substring_does_not_leak_other_instruments(self):
        # querying ticker 'ES' must NOT pick up a TESLA row even though 'ES' is a substring.
        rows = self._load([_row("AF-1-TESLA", "2026-06-10 20:00", 2, 0)])
        ctx = LC.build_context("ES", rows, ticker="ES", asset_class="equity")
        self.assertEqual(ctx["historical_prediction_count"], 0)
        self.assertIsNone(ctx["instrument_hit_rate"])

    def test_type_scope_falls_back_to_asset_class_when_instrument_has_no_types(self):
        rows = self._load([
            _row("AF-1-AAPL", "2026-06-01 20:00", 1, 1, pred_type=""),          # instrument, no type
            _row("AF-2-MSFT", "2026-06-02 20:00", 3, 1, pred_type="breakout"),  # same class, has type
        ])
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity")
        self.assertEqual(ctx["prediction_type_scope"], "asset_class")
        self.assertIn("breakout", ctx["prediction_type_hit_rates"])

    def test_drift_emitted_only_with_enough_recent_history(self):
        # recent_k=2 => drift needs inst_n >= 4. Build 4 rows: 3 wins then a loss tail.
        rows = self._load([
            _row("AF-1-AAPL", "2026-06-01 20:00", 2, 0),
            _row("AF-2-AAPL", "2026-06-02 20:00", 2, 0),
            _row("AF-3-AAPL", "2026-06-03 20:00", 0, 2),
            _row("AF-4-AAPL", "2026-06-04 20:00", 0, 2),
        ])
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity", recent_k=2)
        self.assertIsNotNone(ctx["recent_drift"])
        self.assertEqual(ctx["recent_drift"]["last_k"], 2)
        # last 2 rows are losses (0%) vs 50% instrument overall -> negative delta
        self.assertLess(ctx["recent_drift"]["delta_vs_instrument"], 0)

    def test_no_drift_when_history_too_short(self):
        rows = self._load([_row("AF-1-AAPL", "2026-06-01 20:00", 2, 0)])
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity", recent_k=2)
        self.assertIsNone(ctx["recent_drift"])

    def test_similar_setup_history_is_capped_to_recent_k(self):
        rows = self._load([_row(f"AF-{i}-AAPL", f"2026-06-{i:02d} 20:00", 1, 0)
                           for i in range(1, 6)])
        ctx = LC.build_context("Apple", rows, ticker="AAPL", asset_class="equity", recent_k=2)
        self.assertEqual(len(ctx["similar_setup_history"]), 2)

    def test_ticker_defaults_to_name_uppercased(self):
        ctx = LC.build_context("aapl", [], ticker=None, asset_class="equity")
        self.assertEqual(ctx["ticker"], "AAPL")


class TestPatternsAndNotes(unittest.TestCase):
    def test_no_history_note_when_inst_n_zero(self):
        s, f, n = LC._patterns_and_notes("Apple", "AAPL", 0, None, {}, {}, None)
        self.assertEqual(s, [])
        self.assertEqual(f, [])
        self.assertTrue(any("No scored history" in x for x in n))

    def test_success_pattern_requires_rate_ge_65_and_n_ge_4(self):
        gtr = {"breakout": 70.0, "thin": 90.0}
        gtc = {"breakout": 5, "thin": 2}     # thin has n<4 -> ignored
        s, f, n = LC._patterns_and_notes("Apple", "AAPL", 10, 60.0, gtr, gtc, None)
        self.assertEqual(len(s), 1)
        self.assertIn("breakout", s[0])

    def test_failure_pattern_when_rate_below_45(self):
        gtr = {"pullback": 40.0}
        gtc = {"pullback": 6}
        s, f, n = LC._patterns_and_notes("Apple", "AAPL", 10, 60.0, gtr, gtc, None)
        self.assertEqual(len(f), 1)
        self.assertIn("cut conviction", f[0])

    def test_drift_note_added_when_delta_at_least_minus_10(self):
        drift = {"last_k": 2, "recent_hit_rate": 30.0, "delta_vs_instrument": -15.0}
        s, f, n = LC._patterns_and_notes("Apple", "AAPL", 10, 55.0, {}, {}, drift)
        self.assertTrue(any("sliding" in x for x in n))

    def test_drift_note_absent_when_delta_small(self):
        drift = {"last_k": 2, "recent_hit_rate": 52.0, "delta_vs_instrument": -3.0}
        s, f, n = LC._patterns_and_notes("Apple", "AAPL", 10, 55.0, {}, {}, drift)
        self.assertFalse(any("sliding" in x for x in n))


class TestLedgerContextParseArgs(unittest.TestCase):
    def test_defaults(self):
        opts = LC.parse_args([])
        self.assertIsNone(opts["ticker"])
        self.assertEqual(opts["recent_k"], LC.RECENT_K)
        self.assertFalse(opts["print"])

    def test_all_flags_parsed(self):
        opts = LC.parse_args(["--ticker", "ES", "--asset-class", "fx",
                              "--recent-k", "5", "--print"])
        self.assertEqual(opts["ticker"], "ES")
        self.assertEqual(opts["asset_class"], "fx")
        self.assertEqual(opts["recent_k"], 5)
        self.assertTrue(opts["print"])

    def test_unknown_argument_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            LC.parse_args(["--bogus"])
        self.assertEqual(cm.exception.code, 2)


# --------------------------------------------------------------------------- #
# research_memory helpers
# --------------------------------------------------------------------------- #
class TestResearchMemoryBreakdowns(unittest.TestCase):
    def test_breakdown_groups_and_skips_blank(self):
        rows = [{"_hits": 4, "_misses": 0, "pred_type": "breakout"},
                {"_hits": 1, "_misses": 1, "pred_type": "breakout"},
                {"_hits": 1, "_misses": 1, "pred_type": ""}]
        bd = RM._breakdown(rows, "pred_type")
        self.assertEqual(set(bd), {"breakout"})
        self.assertEqual(bd["breakout"]["reports"], 2)
        self.assertEqual(bd["breakout"]["hits"], 5)
        self.assertEqual(bd["breakout"]["misses"], 1)
        self.assertEqual(bd["breakout"]["hit_rate_pct"], IO.rate(5, 1))

    def test_cross_breakdown_requires_both_dimensions(self):
        rows = [{"_hits": 2, "_misses": 0, "pred_type": "breakout", "market_regime": "trend_up"},
                {"_hits": 1, "_misses": 1, "pred_type": "breakout", "market_regime": ""}]
        cb = RM._cross_breakdown(rows, "pred_type", "market_regime")
        self.assertEqual(list(cb), ["breakout x trend_up"])
        self.assertEqual(cb["breakout x trend_up"]["reports"], 1)


class TestResearchMemoryBestWorst(unittest.TestCase):
    def _table(self, rate, n=4):
        return {"reports": n, "hits": 0, "misses": 0, "hit_rate_pct": rate}

    def test_best_is_ge_60_worst_is_below_50_and_disjoint(self):
        bd = {"asset_class": {"a": self._table(60.0), "b": self._table(50.0),
                              "c": self._table(40.0)}}
        best, worst = RM._best_worst(bd, 4)
        self.assertEqual([p["pattern"] for p in best], ["a"])      # 60 -> best
        self.assertEqual([p["pattern"] for p in worst], ["c"])     # 40 -> worst
        # the 50% pattern 'b' is in neither list
        self.assertNotIn("b", [p["pattern"] for p in best] + [p["pattern"] for p in worst])

    def test_min_n_guard_excludes_thin_cells(self):
        bd = {"asset_class": {"thin": self._table(90.0, n=3)}}   # n < 4
        best, worst = RM._best_worst(bd, 4)
        self.assertEqual(best, [])
        self.assertEqual(worst, [])

    def test_none_hit_rate_cells_ignored(self):
        bd = {"asset_class": {"x": self._table(None, n=10)}}
        best, worst = RM._best_worst(bd, 4)
        self.assertEqual(best, [])
        self.assertEqual(worst, [])

    def test_best_sorted_by_rate_then_reports(self):
        bd = {"asset_class": {"hi": self._table(80.0), "mid": self._table(70.0)}}
        best, _ = RM._best_worst(bd, 4)
        self.assertEqual([p["pattern"] for p in best], ["hi", "mid"])


class TestResearchMemoryNotes(unittest.TestCase):
    def test_empty_history_note(self):
        notes = RM._notes(0, None, [], [], 4)
        self.assertTrue(any("No scored history" in n for n in notes))

    def test_too_few_reports_warning(self):
        notes = RM._notes(2, 50.0, [], [], 4)
        self.assertTrue(any("Too few scored reports" in n for n in notes))

    def test_best_and_worst_lines_rendered(self):
        best = [{"pattern": "breakout", "dimension": "prediction_type",
                 "hit_rate_pct": 80.0, "reports": 5}]
        worst = [{"pattern": "fade", "dimension": "prediction_type",
                  "hit_rate_pct": 30.0, "reports": 5}]
        notes = RM._notes(10, 55.0, best, worst, 4)
        self.assertTrue(any("Works well: breakout" in n for n in notes))
        self.assertTrue(any("Underperforms: fade" in n for n in notes))


class TestResearchMemoryBuildMemory(_LedgerTmp):
    def test_overall_hit_rate_and_dimension_tables(self):
        rows = self._load([
            _row("AF-1-AAPL", "2026-06-01 20:00", 4, 0, asset_class="equity", direction="bullish"),
            _row("AF-2-MSFT", "2026-06-02 20:00", 2, 2, asset_class="equity", direction="bearish"),
        ])
        mem = RM.build_memory(rows, FUTURE)
        self.assertEqual(mem["total_scored_reports"], 2)
        self.assertEqual(mem["overall_hit_rate_pct"], IO.rate(6, 2))   # 75.0
        self.assertIn("equity", mem["by_asset_class"])
        self.assertEqual(mem["by_asset_class"]["equity"]["reports"], 2)
        self.assertIn("bullish", mem["by_direction"])

    def test_min_n_param_threads_into_best_worst(self):
        rows = self._load([_row("AF-1-AAPL", "2026-06-01 20:00", 4, 0)])
        # default min_n=4 with a single report -> no patterns
        self.assertEqual(RM.build_memory(rows, FUTURE)["best_patterns"], [])
        # min_n=1 -> the single 100% report surfaces
        loose = RM.build_memory(rows, FUTURE, min_n=1)
        self.assertTrue(any(p["pattern"] == "breakout" for p in loose["best_patterns"]))


class TestResearchMemoryParseArgs(unittest.TestCase):
    def test_defaults(self):
        opts = RM.parse_args([])
        self.assertEqual(opts["min_n"], RM.MIN_N)
        self.assertFalse(opts["print"])
        self.assertIsNone(opts["out"])

    def test_flags_parsed(self):
        opts = RM.parse_args(["--min-n", "7", "--print"])
        self.assertEqual(opts["min_n"], 7)
        self.assertTrue(opts["print"])

    def test_unknown_argument_exits_2(self):
        with self.assertRaises(SystemExit) as cm:
            RM.parse_args(["--nope"])
        self.assertEqual(cm.exception.code, 2)


# --------------------------------------------------------------------------- #
# memory_pack
# --------------------------------------------------------------------------- #
class TestMemoryPackHelpers(unittest.TestCase):
    def test_approx_tokens_is_quarter_of_json_length(self):
        obj = {"a": "x" * 40}
        self.assertEqual(MP._approx_tokens(obj), len(json.dumps(obj)) // 4)

    def test_load_missing_returns_none(self):
        self.assertIsNone(MP._load("no/such/file.json"))

    def test_load_malformed_json_returns_none(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, p)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertIsNone(MP._load(p))

    def test_load_handles_utf8_bom(self):
        fd, p = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, p)
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write(json.dumps({"method": "beta", "n_rows": 12}))
        self.assertEqual(MP._load(p), {"method": "beta", "n_rows": 12})


class TestMemoryPackBuildPack(_LedgerTmp):
    def _ledger_with_equity_history(self):
        return self._write_ledger([
            _row(f"AF-{i}-AAPL", f"2026-06-0{i} 20:00", 4, 0) for i in range(1, 5)
        ])

    def test_no_history_is_bounded_and_neutral(self):
        # explicit non-existent ledger -> no rows -> neutral; deterministic regardless of repo state
        pack = MP.build_pack({"ticker": "X", "asset_class": "fx"},
                             as_of=FUTURE, ledger="no/such/ledger.csv")
        self.assertEqual(pack["global"]["total_scored_reports"], 0)
        self.assertIsNone(pack["instrument_history"]["hit_rate_pct"])
        self.assertTrue(pack["budget"]["within_budget"])
        self.assertLessEqual(pack["budget"]["approx_tokens"], pack["budget"]["limit"])

    def test_instrument_and_global_layers_reflect_history(self):
        pack = MP.build_pack({"instrument": "Apple", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=FUTURE, ledger=self._ledger_with_equity_history())
        self.assertEqual(pack["global"]["total_scored_reports"], 4)
        self.assertEqual(pack["instrument_history"]["reports"], 4)
        self.assertEqual(pack["instrument_history"]["hit_rate_pct"], 100.0)
        self.assertEqual(pack["asset_class_history"]["hit_rate_pct"], 100.0)

    def test_asset_class_history_best_only_contains_matching_class_patterns(self):
        pack = MP.build_pack({"instrument": "Apple", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=FUTURE, ledger=self._ledger_with_equity_history())
        best = pack["asset_class_history"]["best"]
        self.assertTrue(best)   # a 100% equity class at n=4 should surface
        for p in best:
            self.assertEqual(p["dimension"], "asset_class")
            self.assertEqual(p["pattern"], "equity")

    def test_naive_as_of_is_treated_as_utc(self):
        pack = MP.build_pack({"ticker": "X"}, as_of=datetime(2000, 1, 1),
                             ledger="no/such/ledger.csv")
        self.assertTrue(pack["as_of_utc"].endswith("UTC"))
        self.assertTrue(pack["as_of_utc"].startswith("2000-01-01"))

    def test_default_as_of_uses_now_and_still_builds(self):
        pack = MP.build_pack({"ticker": "X"}, ledger="no/such/ledger.csv")
        self.assertIn("as_of_utc", pack)
        self.assertEqual(pack["global"]["total_scored_reports"], 0)

    def test_tiny_budget_triggers_compaction_of_class_patterns(self):
        pack = MP.build_pack({"instrument": "Apple", "ticker": "AAPL", "asset_class": "equity"},
                             as_of=FUTURE, ledger=self._ledger_with_equity_history(),
                             token_budget=10)
        ach = pack["asset_class_history"]
        # first compaction step caps best/worst to one entry each
        self.assertLessEqual(len(ach["best"]), 1)
        self.assertLessEqual(len(ach["worst"]), 1)
        self.assertEqual(pack["budget"]["limit"], 10)
        # within_budget flag is consistent with the recorded token estimate
        self.assertEqual(pack["budget"]["within_budget"],
                         pack["budget"]["approx_tokens"] <= 10)

    def test_name_falls_back_through_instrument_ticker_id(self):
        # no instrument/ticker -> name comes from id; build still succeeds offline
        pack = MP.build_pack({"id": "gold-spot", "asset_class": "commodity"},
                             as_of=FUTURE, ledger="no/such/ledger.csv")
        self.assertEqual(pack["asset_class"], "commodity")
        self.assertEqual(pack["global"]["total_scored_reports"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
