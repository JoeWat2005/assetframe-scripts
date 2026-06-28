"""Offline unit tests for scripts/analytics/store: ledger_db, calibrate, sync_backtest.

Deterministic + offline: no real network / Neon / Anthropic / R2 / boto3 / subprocess. The only
real I/O is a local temp SQLite file (ledger_db's own mirror, which is the module's whole job) and
temp CSV/JSON fixtures; sync_backtest's Neon access is mocked via engine_ops.connect.

These target the GAPS left by test_ledger_db / test_calibrate / test_sandbox / test_engine_config /
test_horizon_calibration: the pure coercion/parse helpers, CLI/dispatch branches, history-table
append-vs-replace semantics, and assorted edge cases (empty/None/garbage/whitespace/boundary).

Run:  python -m pytest tests/test_analytics_store_unit.py -q
"""
import contextlib
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_db as L
import calibrate as K
import sync_backtest as SB


@contextlib.contextmanager
def _chdir(d):
    old = os.getcwd()
    os.chdir(str(d))
    try:
        yield
    finally:
        os.chdir(old)


def _write_ledger_csv(path, rows):
    cols = [c for c, _ in L.COLUMNS]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# =========================================================== ledger_db._coerce
class TestLedgerCoerce(unittest.TestCase):
    def test_integer_truncates_via_float(self):
        self.assertEqual(L._coerce("INTEGER", "2.9"), 2)   # int(float("2.9"))
        self.assertEqual(L._coerce("INTEGER", "5"), 5)
        self.assertEqual(L._coerce("INTEGER", "-3"), -3)

    def test_integer_unparseable_is_none(self):
        self.assertIsNone(L._coerce("INTEGER", "abc"))

    def test_real_parses_and_rejects(self):
        self.assertEqual(L._coerce("REAL", "3.5"), 3.5)
        self.assertIsNone(L._coerce("REAL", "not-a-number"))

    def test_text_strips_and_blanks_to_none(self):
        self.assertEqual(L._coerce("TEXT", "  hi  "), "hi")
        self.assertIsNone(L._coerce("TEXT", ""))

    def test_none_and_whitespace_are_none_for_every_type(self):
        for t in ("TEXT", "INTEGER", "REAL"):
            self.assertIsNone(L._coerce(t, None))
            self.assertIsNone(L._coerce(t, "   "))


# ================================================ ledger_db.rebuild edge cases
class TestLedgerRebuildEdges(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "outcome_ledger.csv"
        self.db = self.tmp / "engine.sqlite"

    def test_empty_report_id_rows_are_not_deduped(self):
        # report_id coerces to NULL for a blank cell; SQLite allows many NULLs in a (non-INTEGER)
        # PK, so two blank-report_id rows BOTH survive — documents current behaviour.
        _write_ledger_csv(self.csv, [{"instrument": "A"}, {"instrument": "B"}])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["db_rows"], 2)
        self.assertEqual(summ["duplicates_dropped"], 0)

    def test_bad_numeric_cells_counted_but_whitespace_is_not(self):
        # "oops" in a REAL column is a bad cell; a whitespace-only cell coerces to NULL silently
        # (it is treated as empty, not as a parse failure).
        _write_ledger_csv(self.csv, [{"report_id": "AF-1", "confidence": "oops",
                                      "hit_rate_pct": "   "}])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["bad_numeric_cells"], 1)   # only "oops", not the blank hit_rate

    def test_zero_is_kept_not_treated_as_bad(self):
        _write_ledger_csv(self.csv, [{"report_id": "AF-1", "hits": "0", "misses": "0",
                                      "hit_rate_pct": "0"}])
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["bad_numeric_cells"], 0)
        con = L.connect(self.db)
        try:
            r = con.execute("SELECT hits, hit_rate_pct FROM ledger").fetchone()
        finally:
            con.close()
        self.assertEqual(r["hits"], 0)
        self.assertEqual(r["hit_rate_pct"], 0.0)

    def test_rebuild_creates_aux_tables(self):
        _write_ledger_csv(self.csv, [{"report_id": "AF-1"}])
        L.rebuild(self.csv, self.db)
        con = sqlite3.connect(str(self.db))
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        self.assertTrue({"ledger", "runs", "calibration_history", "asset_cache"} <= names)


# ============================================ ledger_db.connect auto-rebuild
class TestLedgerConnect(unittest.TestCase):
    def test_connect_rebuilds_when_db_missing(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "engine.sqlite"
        self.assertFalse(db.exists())
        # No CSV present either -> empty ledger, but the file + schema get created on demand.
        with _chdir(tmp):
            con = L.connect(db)
        try:
            self.assertTrue(db.exists())
            self.assertEqual(con.execute("SELECT COUNT(*) n FROM ledger").fetchone()["n"], 0)
        finally:
            con.close()


# ================================== ledger_db aux-table history semantics
class TestLedgerAuxHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "engine.sqlite"

    def test_record_run_insert_or_replace_keeps_one_row_per_run_id(self):
        self.assertTrue(L.record_run("AF-X", "production", "2026-06-24", "running", db_path=self.db))
        self.assertTrue(L.record_run("AF-X", "production", "2026-06-24", "ok", generated=5,
                                     db_path=self.db))
        con = sqlite3.connect(str(self.db))
        try:
            rows = con.execute("SELECT run_id, status, generated FROM runs").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("AF-X", "ok", 5)])   # replaced in place, latest wins

    def test_record_run_serializes_manifest_json(self):
        L.record_run("AF-Y", "production", "2026-06-24", "ok", manifest={"a": 1}, db_path=self.db)
        con = sqlite3.connect(str(self.db))
        try:
            mj = con.execute("SELECT manifest_json FROM runs WHERE run_id='AF-Y'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(json.loads(mj), {"a": 1})

    def test_record_run_is_best_effort_returns_false_on_bad_path(self):
        # A directory path can't be opened as a DB -> the audit write swallows it and returns False
        # (an audit table must NEVER block or crash a run).
        self.assertFalse(L.record_run("AF-Z", "m", "d", "ok", db_path=self.tmp))

    def test_record_calibration_appends_history(self):
        L.record_calibration("2", 10, {"v": 1}, db_path=self.db)
        L.record_calibration("2", 20, {"v": 1}, db_path=self.db)
        con = sqlite3.connect(str(self.db))
        try:
            rows = con.execute("SELECT conf_version, n_rows FROM calibration_history "
                               "ORDER BY id").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("2", 10), ("2", 20)])   # append-only, both snapshots kept

    def test_record_calibration_coerces_none_n_rows_to_zero(self):
        self.assertTrue(L.record_calibration("2", None, None, db_path=self.db))
        con = sqlite3.connect(str(self.db))
        try:
            r = con.execute("SELECT n_rows, map_json FROM calibration_history").fetchone()
        finally:
            con.close()
        self.assertEqual(r[0], 0)        # int(None or 0)
        self.assertIsNone(r[1])          # None map -> SQL NULL

    def test_cache_assets_is_singleton_upsert(self):
        L.cache_assets([{"id": "a"}, {"id": "b"}], db_path=self.db)
        L.cache_assets([{"id": "c"}], db_path=self.db)   # overwrites id=1, no second row
        con = sqlite3.connect(str(self.db))
        try:
            rows = con.execute("SELECT id, n_assets, assets_json FROM asset_cache").fetchall()
        finally:
            con.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 1)
        self.assertEqual(json.loads(rows[0][2]), [{"id": "c"}])

    def test_cache_assets_none_is_empty_list(self):
        self.assertTrue(L.cache_assets(None, db_path=self.db))
        con = sqlite3.connect(str(self.db))
        try:
            r = con.execute("SELECT n_assets, assets_json FROM asset_cache").fetchone()
        finally:
            con.close()
        self.assertEqual(r[0], 0)
        self.assertEqual(json.loads(r[1]), [])

    def test_ensure_aux_tables_is_idempotent(self):
        con = sqlite3.connect(str(self.db))
        try:
            L.ensure_aux_tables(con)
            L.ensure_aux_tables(con)   # second call must not raise
            con.commit()
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        self.assertTrue({"runs", "calibration_history", "asset_cache"} <= names)


# ============================================ ledger_db._stats + main dispatch
class TestLedgerStatsAndMain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "outcome_ledger.csv"
        self.db = self.tmp / "engine.sqlite"

    def test_stats_groups_by_instrument(self):
        _write_ledger_csv(self.csv, [
            {"report_id": "AF-1", "instrument": "GBP/USD", "hit_rate_pct": "40"},
            {"report_id": "AF-2", "instrument": "GBP/USD", "hit_rate_pct": "60"},
            {"report_id": "AF-3", "instrument": "BTC/USD", "hit_rate_pct": "80"},
        ])
        L.rebuild(self.csv, self.db)
        stats = L._stats(self.db)
        self.assertEqual(stats["total_rows"], 3)
        by = {r["instrument"]: r for r in stats["by_instrument"]}
        self.assertEqual(by["GBP/USD"]["n"], 2)
        self.assertEqual(by["GBP/USD"]["avg_hit"], 50.0)
        self.assertEqual(by["BTC/USD"]["n"], 1)

    def test_main_query_rejects_non_select(self):
        # The read-only guard returns 2 BEFORE any DB is opened, so this needs no monkeypatch.
        self.assertEqual(L.main(["query", "DELETE FROM ledger"]), 2)
        self.assertEqual(L.main(["query", "UPDATE ledger SET hits=0"]), 2)

    def test_main_query_requires_sql_arg(self):
        self.assertEqual(L.main(["query"]), 2)

    def test_main_unknown_command_returns_2(self):
        self.assertEqual(L.main(["frobnicate"]), 2)

    def test_main_query_happy_path_prints_json_rows(self):
        _write_ledger_csv(self.csv, [{"report_id": "AF-1", "instrument": "GBP/USD"}])
        L.rebuild(self.csv, self.db)
        buf = io.StringIO()
        # main's "query" path calls the module-global connect() with no args -> patch it to our temp DB.
        with mock.patch.object(L, "connect", lambda: _open_temp(self.db)), \
                contextlib.redirect_stdout(buf):
            rc = L.main(["query", "SELECT instrument FROM ledger"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), [{"instrument": "GBP/USD"}])

    def test_main_with_and_select_both_allowed(self):
        # WITH ... SELECT must pass the read-only guard (returns 0), proving the guard is not
        # SELECT-only. Patch connect to a throwaway in-memory-ish temp DB.
        _write_ledger_csv(self.csv, [{"report_id": "AF-1", "instrument": "X"}])
        L.rebuild(self.csv, self.db)
        with mock.patch.object(L, "connect", lambda: _open_temp(self.db)), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = L.main(["query", "WITH t AS (SELECT 1 AS one) SELECT one FROM t"])
        self.assertEqual(rc, 0)

    def test_main_rebuild_and_stats_dispatch(self):
        with mock.patch.object(L, "rebuild", return_value={"db_rows": 0}) as rb, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["rebuild"]), 0)
            self.assertEqual(L.main([]), 0)   # default command is rebuild
        self.assertEqual(rb.call_count, 2)
        with mock.patch.object(L, "_stats", return_value={"total_rows": 0}) as st, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(L.main(["stats"]), 0)
        st.assert_called_once()


def _open_temp(db_path):
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


# =================================================== calibrate pure helpers
class TestCalibrateHelpers(unittest.TestCase):
    def test_clamp_bounds(self):
        self.assertEqual(K._clamp(-5), 0.0)
        self.assertEqual(K._clamp(150), 100.0)
        self.assertEqual(K._clamp(50), 50)
        self.assertEqual(K._clamp(7, lo=10, hi=20), 10)

    def test_project_drops_horizon_tag(self):
        self.assertEqual(K._project([(70.0, 0.5, 3, "intraday"), (40.0, 0.2, 1, "next_session")]),
                         [(70.0, 0.5, 3), (40.0, 0.2, 1)])

    def test_merge_duplicate_x_guards_zero_weight(self):
        # a defensively-passed zero-weight point must not divide-by-zero; it yields the 0.5 fallback.
        merged = K._merge_duplicate_x([(50.0, 1.0, 0)])
        self.assertEqual(merged, [(50.0, 0.5, 0.0)])

    def test_pava_empty_is_empty(self):
        self.assertEqual(K.pava([], []), [])

    def test_pava_single_point_passthrough(self):
        self.assertEqual(K.pava([0.42], [3]), [0.42])


# =================================================== calibrate.load_points edges
class TestCalibrateLoadPointsEdges(unittest.TestCase):
    def _ledger(self, rows):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        cols = ["conf_version", "conf_raw", "confidence", "hits", "misses", "horizon"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        self.addCleanup(os.remove, path)
        return path

    def test_blank_conf_version_excluded_when_version_requested(self):
        p = self._ledger([
            {"conf_version": "", "conf_raw": "60", "hits": "3", "misses": "1", "horizon": "x"},
        ])
        self.assertEqual(K.load_points(p, 2), [])           # blank != "2" -> excluded

    def test_row_with_no_numeric_score_skipped(self):
        p = self._ledger([
            {"conf_version": "2", "conf_raw": "", "confidence": "", "hits": "3", "misses": "1"},
        ])
        self.assertEqual(K.load_points(p, 2), [])           # float("") -> ValueError -> skip

    def test_non_integer_hits_skips_row(self):
        p = self._ledger([
            {"conf_version": "2", "conf_raw": "60", "hits": "two", "misses": "1"},
        ])
        self.assertEqual(K.load_points(p, 2), [])

    def test_horizon_tag_captured_as_fourth_element(self):
        p = self._ledger([
            {"conf_version": "2", "conf_raw": "70", "hits": "3", "misses": "1",
             "horizon": "intraday"},
        ])
        pts = K.load_points(p, 2)
        self.assertEqual(len(pts), 1)
        self.assertEqual(pts[0][3], "intraday")
        self.assertAlmostEqual(pts[0][1], 0.75, places=6)

    def test_conf_raw_zero_is_used_not_skipped(self):
        # "0" is a legitimate raw score; the `or` fallback must not discard it.
        p = self._ledger([
            {"conf_version": "2", "conf_raw": "0", "confidence": "55", "hits": "1", "misses": "1"},
        ])
        pts = K.load_points(p, 2)
        self.assertEqual(pts[0][0], 0.0)


# =================================================== calibrate.parse_args
class TestCalibrateParseArgs(unittest.TestCase):
    def test_defaults(self):
        o = K.parse_args([])
        self.assertEqual(o["ledger"], K.DEFAULT_LEDGER)
        self.assertEqual(o["out"], K.DEFAULT_OUT)
        self.assertEqual(o["conf_version"], K.CONF_VERSION)
        self.assertEqual(o["n_full"], K.N_FULL)
        self.assertEqual(o["min_rows"], K.MIN_ROWS)
        self.assertFalse(o["dry_run"])

    def test_overrides_and_types(self):
        o = K.parse_args(["--ledger", "a.csv", "--out", "b.json", "--n-full", "12",
                          "--min-rows", "3", "--dry-run"])
        self.assertEqual(o["ledger"], Path("a.csv"))
        self.assertEqual(o["out"], Path("b.json"))
        self.assertEqual(o["n_full"], 12)
        self.assertEqual(o["min_rows"], 3)
        self.assertTrue(o["dry_run"])

    def test_conf_version_all_and_blank_mean_none(self):
        self.assertIsNone(K.parse_args(["--conf-version", "all"])["conf_version"])
        self.assertIsNone(K.parse_args(["--conf-version", ""])["conf_version"])
        self.assertEqual(K.parse_args(["--conf-version", "3"])["conf_version"], 3)

    def test_unknown_argument_exits_2(self):
        with self.assertRaises(SystemExit) as ctx:
            K.parse_args(["--nope"])
        self.assertEqual(ctx.exception.code, 2)


# =================================================== calibrate.main
class TestCalibrateMain(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ledger = self.tmp / "led.csv"
        cols = ["conf_version", "conf_raw", "hits", "misses", "horizon"]
        with open(self.ledger, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({"conf_version": "2", "conf_raw": "70", "hits": "3", "misses": "1",
                        "horizon": "next_session"})

    def test_dry_run_prints_map_and_writes_nothing(self):
        out = self.tmp / "map.json"
        buf = io.StringIO()
        argv = ["calibrate.py", "--ledger", str(self.ledger), "--out", str(out), "--dry-run"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
            K.main()
        self.assertFalse(out.exists())                       # dry-run writes no file
        printed = json.loads(buf.getvalue())
        self.assertEqual(printed["version"], 2)              # build_calibration map echoed to stdout

    def test_writes_map_file_when_not_dry_run(self):
        out = self.tmp / "map.json"
        argv = ["calibrate.py", "--ledger", str(self.ledger), "--out", str(out)]
        # chdir into a throwaway tree so the best-effort engine.sqlite history write (relative path)
        # lands there, not in the repo.
        with _chdir(self.tmp), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(io.StringIO()):
            K.main()
        self.assertTrue(out.exists())
        cmap = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(cmap["version"], 2)
        self.assertIn("knots", cmap)


# =================================================== sync_backtest coercion helpers
class TestSyncCoercion(unittest.TestCase):
    def test_int_or_none(self):
        self.assertIsNone(SB._int_or_none(None))
        self.assertIsNone(SB._int_or_none(""))
        self.assertIsNone(SB._int_or_none("  "))
        self.assertIsNone(SB._int_or_none("abc"))
        self.assertEqual(SB._int_or_none("2"), 2)
        self.assertEqual(SB._int_or_none("2.0"), 2)     # int(float("2.0"))
        self.assertEqual(SB._int_or_none("  3 "), 3)

    def test_num_or_none(self):
        self.assertIsNone(SB._num_or_none(""))
        self.assertIsNone(SB._num_or_none(None))
        self.assertIsNone(SB._num_or_none("x"))
        self.assertEqual(SB._num_or_none("66.7"), 66.7)
        self.assertEqual(SB._num_or_none("2"), 2.0)

    def test_bool_or_false(self):
        self.assertTrue(SB._bool_or_false(True))
        self.assertTrue(SB._bool_or_false("true"))
        self.assertTrue(SB._bool_or_false("TRUE"))
        self.assertTrue(SB._bool_or_false("yes"))
        self.assertTrue(SB._bool_or_false("1"))
        self.assertTrue(SB._bool_or_false(1))
        self.assertFalse(SB._bool_or_false(False))
        self.assertFalse(SB._bool_or_false("false"))
        self.assertFalse(SB._bool_or_false(0))
        self.assertFalse(SB._bool_or_false(None))
        self.assertFalse(SB._bool_or_false("maybe"))

    def test_int_or_zero(self):
        self.assertEqual(SB._int_or_zero("5"), 5)
        self.assertEqual(SB._int_or_zero(3), 3)
        self.assertEqual(SB._int_or_zero(2.7), 2)
        self.assertEqual(SB._int_or_zero(None), 0)
        self.assertEqual(SB._int_or_zero("x"), 0)
        # documents the int()-not-float() quirk: "2.0" is NOT parsed (-> 0), unlike _int_or_none.
        self.assertEqual(SB._int_or_zero("2.0"), 0)

    def test_ticker_from_report_id(self):
        self.assertEqual(SB._ticker_from_report_id("AF-202606171200-BTC"), "BTC")
        self.assertEqual(SB._ticker_from_report_id("AF-20260617-GBPJPY"), "GBPJPY")
        self.assertEqual(SB._ticker_from_report_id("NODASH"), "NODASH")  # rsplit on no '-'
        self.assertEqual(SB._ticker_from_report_id(""), "")
        self.assertEqual(SB._ticker_from_report_id(None), "")
        self.assertEqual(SB._ticker_from_report_id("  AF-1-ES  "), "ES")  # stripped first


# =================================================== sync_backtest.map_row edges
class TestSyncMapRow(unittest.TestCase):
    def test_strips_whitespace_on_text_fields(self):
        t = SB.map_row({"report_id": "AF-1-ES", "instrument": "  ES=F  ",
                        "asset_class": " future ", "view": " Bullish ",
                        "horizon": " intraday ", "window_end_utc": " 2026 ",
                        "results": " hit ", "scored_at_utc": " ts "})
        self.assertEqual(t[2], "ES=F")
        self.assertEqual(t[3], "future")
        self.assertEqual(t[4], "Bullish")
        self.assertEqual(t[6], "intraday")
        self.assertEqual(t[7], "2026")
        self.assertEqual(t[8], "hit")
        self.assertEqual(t[12], "ts")

    def test_missing_optional_fields_default_empty_or_none(self):
        t = SB.map_row({"report_id": "AF-1-ES"})
        self.assertEqual(t[0], "AF-1-ES")
        self.assertEqual(t[1], "ES")
        self.assertEqual(t[2], "")        # instrument missing -> ""
        self.assertIsNone(t[5])           # confidence missing -> None
        self.assertIsNone(t[9])           # hits missing -> None

    def test_row_without_report_id_returns_none(self):
        self.assertIsNone(SB.map_row({"report_id": "   "}))
        self.assertIsNone(SB.map_row({}))


# =================================================== sync_backtest.read_sim_rows
class TestSyncReadSimRows(unittest.TestCase):
    def test_skips_rows_with_no_report_id(self):
        tmp = Path(tempfile.mkdtemp())
        p = tmp / "sim.csv"
        cols = [c for c, _ in L.COLUMNS]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerow({c: "" for c in cols})                       # blank report_id -> skipped
            row = {c: "" for c in cols}
            row["report_id"] = "AF-202606171200-BTC"
            row["instrument"] = "BTC-USD"
            w.writerow(row)
        rows = SB.read_sim_rows(p)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "AF-202606171200-BTC")

    def test_missing_file_is_empty(self):
        self.assertEqual(SB.read_sim_rows(Path("nope/sim.csv")), [])


# =================================================== sync_backtest.map_pred edges
class TestSyncMapPred(unittest.TestCase):
    def test_numeric_pred_id_coerced_to_string(self):
        t = SB.map_pred("R1", {"pred_id": 7, "ptype": "x", "sort": 1})
        self.assertEqual(t[1], "7")

    def test_pred_id_whitespace_stripped(self):
        t = SB.map_pred("R1", {"pred_id": "  P3  "})
        self.assertEqual(t[1], "P3")

    def test_missing_optional_text_fields_default_empty(self):
        t = SB.map_pred("R1", {"pred_id": "P1"})
        self.assertEqual(t[2], "")        # ptype
        self.assertEqual(t[3], "")        # ptext
        self.assertIs(t[4], False)        # manual
        self.assertIsNone(t[5])           # outcome
        self.assertEqual(t[6], 0)         # sort

    def test_string_outcome_is_stripped(self):
        t = SB.map_pred("R1", {"pred_id": "P1", "outcome": "  Y  "})
        self.assertEqual(t[5], "Y")

    def test_no_pred_id_returns_none(self):
        self.assertIsNone(SB.map_pred("R1", {"ptype": "manual"}))
        self.assertIsNone(SB.map_pred("R1", {"pred_id": "   "}))


# =================================================== sync_backtest.read_pred_rows edges
class TestSyncReadPredRows(unittest.TestCase):
    def test_skips_non_dict_entries_within_a_valid_list(self):
        d = Path(tempfile.mkdtemp())
        (d / "AF-1.json").write_text(json.dumps([
            "not-a-dict",
            {"pred_id": "P1", "ptype": "x", "sort": 0},
            123,
        ]), encoding="utf-8")
        rows = SB.read_pred_rows(d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "P1")

    def test_report_id_comes_from_filename_stem(self):
        d = Path(tempfile.mkdtemp())
        # report_id inside the entry is ignored; the filename stem is authoritative.
        (d / "AF-FROM-STEM.json").write_text(json.dumps([
            {"pred_id": "P1", "report_id": "WRONG"}
        ]), encoding="utf-8")
        rows = SB.read_pred_rows(d)
        self.assertEqual(rows[0][0], "AF-FROM-STEM")

    def test_malformed_or_non_list_files_skipped(self):
        d = Path(tempfile.mkdtemp())
        (d / "bad.json").write_text("{ broken", encoding="utf-8")
        (d / "obj.json").write_text('{"pred_id": "P1"}', encoding="utf-8")   # dict, not list
        self.assertEqual(SB.read_pred_rows(d), [])

    def test_missing_dir_is_empty(self):
        self.assertEqual(SB.read_pred_rows(Path("does/not/exist")), [])


# =================================================== sync_backtest.sync (DB mocked)
class _FakeConn:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class TestSyncUpsert(unittest.TestCase):
    def test_sync_upserts_each_mapped_row_into_backtest_results(self):
        rows = [
            ("AF-1-BTC", "BTC", "BTC-USD", "crypto", "Bullish", 70, "intraday",
             "2026", "hit", 2, 1, 66.7, "ts1"),
            ("AF-2-ES", "ES", "ES=F", "future", "Bearish", None, "next_session",
             "2026", "miss", 0, 1, 0.0, "ts2"),
        ]
        c = _FakeConn()
        with mock.patch.object(SB, "read_sim_rows", return_value=rows), \
                mock.patch.object(SB.engine_ops, "connect", return_value=c):
            n = SB.sync()
        self.assertEqual(n, 2)
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(all("backtest_results" in sql.lower() for sql, _ in c.calls))
        self.assertEqual(c.calls[0][1], rows[0])      # exact value-tuple forwarded as params

    def test_sync_no_rows_never_opens_connection(self):
        with mock.patch.object(SB, "read_sim_rows", return_value=[]), \
                mock.patch.object(SB.engine_ops, "connect") as conn:
            self.assertEqual(SB.sync(), 0)
        conn.assert_not_called()

    def test_upsert_sql_targets_report_id_conflict(self):
        sql = SB._UPSERT_SQL.lower()
        self.assertIn("insert into backtest_results", sql)
        self.assertIn("on conflict (report_id) do update set", sql)
        # one placeholder per TABLE_COLS column.
        self.assertEqual(sql.count("%s"), len(SB.TABLE_COLS))

    def test_pred_upsert_sql_preserves_manual_outcome(self):
        sql = SB._PRED_UPSERT_SQL.lower()
        self.assertIn("on conflict (report_id, pred_id) do update set", sql)
        self.assertIn("coalesce(backtest_predictions.outcome, excluded.outcome)", sql)
        self.assertEqual(sql.count("%s"), len(SB.PRED_COLS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
