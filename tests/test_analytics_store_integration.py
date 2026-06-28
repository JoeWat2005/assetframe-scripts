"""Phase-2 INTEGRATION tests for scripts/analytics/store — the REAL modules of the directory
WIRED TOGETHER over one shared artefact (the outcome-ledger CSV) plus the per-prediction sidecar,
exercised end-to-end with their real in-process dependencies.

Unlike test_analytics_store_unit.py (isolated coercion/parse/dispatch helpers), this file drives the
CROSS-MODULE FLOW and the producer<->consumer DATA CONTRACTS:

  * one realistic ledger CSV (header = score_report.LEDGER_COLS, the PRODUCER's own column order)
    -> ledger_db.rebuild() builds the sqlite mirror -> a real aggregate query reads it back, AND
       calibrate.load_points()/build_calibration() consume the SAME CSV and produce a monotone map,
       AND sync_backtest.read_sim_rows() maps the SAME rows for the Neon upsert. All three readers
       agree on the same source of truth.
  * score_report._write_scored_sidecar() (the REAL producer) writes a sidecar that
    sync_backtest.read_pred_rows()/map_pred() (the REAL consumer) reads back — proving the
    {pred_id, ptype, ptext, manual, sort, outcome} field-name contract still lines up after the
    refactor that split these into scripts/analytics/store and scripts/pipeline/scoring.

Only TRUE external boundaries are faked: the Neon psycopg connection (engine_ops.connect). Every
in-process module is real; the only real I/O is local temp CSV/JSON/SQLite under tmp dirs — the live
ledger/reports/data are never touched.

Run:  python -m pytest tests/test_analytics_store_integration.py -q
"""
import csv
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Mirror the existing tests' import style: the tests dir on sys.path for the flat module imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Also anchor the repo root + apply the scripts subpackage shim so the PRODUCER module
# (scripts.pipeline.scoring.score_report) is importable even if this file is run on its own.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import scripts  # noqa: F401  (side effect: subpackage sys.path shim)

import ledger_db as L
import calibrate as K
import sync_backtest as SB
from scripts.pipeline.scoring import score_report as SR


def _write_ledger(path, rows, header=None, encoding="utf-8"):
    """Write an outcome-ledger CSV using the PRODUCER's column order (score_report.LEDGER_COLS by
    default), filling absent cells with '' exactly as score_report's csv.writer would."""
    cols = header or SR.LEDGER_COLS
    with open(path, "w", newline="", encoding=encoding) as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


def _row(report_id, conf_raw, hits, misses, horizon, instrument="BTC/USD",
         asset_class="crypto", confidence=None):
    """One realistic scored-report ledger row (every column score_report writes)."""
    h, m = int(hits), int(misses)
    rate = round(100 * h / (h + m), 1) if (h + m) else ""
    return {
        "scored_at_utc": "2026-06-20 12:00", "report_id": report_id, "instrument": instrument,
        "view": "Bullish", "confidence": str(conf_raw if confidence is None else confidence),
        "window_end_utc": "2026-06-21 12:00", "results": "P1=Y P2=N",
        "hits": str(h), "misses": str(m), "hit_rate_pct": str(rate),
        "setup_filled": "yes", "setup_outcome": "t1", "partial": "no",
        "conf_version": "2", "conf_raw": str(conf_raw), "asset_class": asset_class,
        "pred_type": "level", "direction": "long", "horizon": horizon,
        "market_regime": "trending",
    }


# A 9-row ledger shared by the main flow: 6 intraday BTC rows (>= MIN_ROWS so the intraday sub-map is
# fitted) + 3 next_session ETH rows (< MIN_ROWS so that horizon is omitted from by_horizon). Realised
# hit-rate rises with conf_raw, so the global isotonic fit is already monotone.
_LEDGER_ROWS = [
    _row("AF-202606201201-BTC", 40, 2, 3, "intraday"),                       # .40
    _row("AF-202606201202-BTC", 50, 3, 3, "intraday"),                       # .50
    _row("AF-202606201203-BTC", 60, 6, 4, "intraday"),                       # .60
    _row("AF-202606201204-BTC", 70, 4, 1, "intraday"),                       # .80
    _row("AF-202606201205-BTC", 80, 9, 1, "intraday"),                       # .90
    _row("AF-202606201206-BTC", 90, 5, 0, "intraday"),                       # 1.0
    _row("AF-202606201207-ETH", 45, 1, 1, "next_session", "ETH/USD"),        # .50
    _row("AF-202606201208-ETH", 55, 3, 2, "next_session", "ETH/USD"),        # .60
    _row("AF-202606201209-ETH", 65, 3, 1, "next_session", "ETH/USD"),        # .75
]


class TestLedgerColumnContract(unittest.TestCase):
    """The mirror only stays a faithful mirror if its column set EXACTLY tracks the writer's. This
    guards the refactor-drift the module docstring promises ('Mirrors score_report.LEDGER_COLS')."""

    def test_ledger_db_columns_match_score_report_writer(self):
        self.assertEqual([name for name, _ in L.COLUMNS], SR.LEDGER_COLS)

    def test_sync_backtest_reads_only_real_ledger_columns(self):
        # Every ledger column sync_backtest.map_row pulls must exist in the writer's header, else a
        # rename on the writer side would silently null the admin backtest_results mirror.
        read = {"report_id", "instrument", "asset_class", "view", "confidence",
                "horizon", "window_end_utc", "results", "hits", "misses",
                "hit_rate_pct", "scored_at_utc"}
        self.assertTrue(read <= set(SR.LEDGER_COLS),
                        msg=f"sync_backtest reads columns absent from the writer: "
                            f"{read - set(SR.LEDGER_COLS)}")


class TestLedgerCsvToDbAndCalibrate(unittest.TestCase):
    """The headline flow: one CSV -> sqlite mirror + query AND -> calibration map, agreeing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "outcome_ledger.csv"
        self.db = self.tmp / "engine.sqlite"
        _write_ledger(self.csv, _LEDGER_ROWS)

    def test_rebuild_summary_is_clean(self):
        summ = L.rebuild(self.csv, self.db)
        self.assertEqual(summ["csv_rows"], 9)
        self.assertEqual(summ["db_rows"], 9)            # all report_ids distinct -> nothing deduped
        self.assertEqual(summ["duplicates_dropped"], 0)
        self.assertEqual(summ["bad_numeric_cells"], 0)  # every numeric cell parsed

    def test_query_mirror_aggregates_match_source(self):
        L.rebuild(self.csv, self.db)
        con = L.connect(self.db)
        try:
            by_inst = {r["instrument"]: r["n"] for r in con.execute(
                "SELECT instrument, COUNT(*) n FROM ledger GROUP BY instrument").fetchall()}
            by_horizon = {r["horizon"]: r["n"] for r in con.execute(
                "SELECT horizon, COUNT(*) n FROM ledger GROUP BY horizon").fetchall()}
            # numeric coercion survived the round-trip: conf_raw is REAL, averageable in SQL
            avg_raw = con.execute("SELECT ROUND(AVG(conf_raw),1) a FROM ledger "
                                  "WHERE instrument='BTC/USD'").fetchone()["a"]
        finally:
            con.close()
        self.assertEqual(by_inst, {"BTC/USD": 6, "ETH/USD": 3})
        self.assertEqual(by_horizon, {"intraday": 6, "next_session": 3})
        self.assertAlmostEqual(avg_raw, 65.0, places=1)   # mean of 40,50,60,70,80,90

    def test_same_csv_feeds_calibrate_and_db_identically(self):
        # The conf_raw set the mirror stores must equal the x-values calibrate fitted on — both read
        # the same rows from the same file; any coercion/format drift would split these.
        L.rebuild(self.csv, self.db)
        con = L.connect(self.db)
        try:
            db_raw = {r[0] for r in con.execute("SELECT conf_raw FROM ledger").fetchall()}
        finally:
            con.close()
        pts = K.load_points(self.csv, 2)
        cal_raw = {p[0] for p in pts}
        self.assertEqual(len(pts), 9)
        self.assertEqual(db_raw, cal_raw)

    def test_build_calibration_is_monotone_with_endpoints_and_subhorizon(self):
        pts = K.load_points(self.csv, 2)
        cmap = K.build_calibration(pts)
        self.assertEqual(cmap["method"], "isotonic+shrinkage")
        self.assertEqual(cmap["version"], 2)
        self.assertEqual(cmap["conf_version"], K.CONF_VERSION)
        knots = cmap["knots"]
        xs = [k[0] for k in knots]
        ys = [k[1] for k in knots]
        # strictly ascending x (safe interpolation) + clamped endpoints at 0 and 100
        self.assertTrue(all(xs[i] < xs[i + 1] for i in range(len(xs) - 1)), xs)
        self.assertEqual(xs[0], 0.0)
        self.assertEqual(xs[-1], 100.0)
        # the calibration guarantee: published rate is monotone non-decreasing in raw score
        self.assertTrue(all(ys[i] <= ys[i + 1] for i in range(len(ys) - 1)), ys)
        # per-horizon sub-maps: intraday has >= MIN_ROWS rows so it is fitted; next_session (3 rows)
        # is omitted, so confidence.compute falls back to the global map for it.
        self.assertIn("by_horizon", cmap)
        self.assertIn("intraday", cmap["by_horizon"])
        self.assertNotIn("next_session", cmap["by_horizon"])

    def test_calibration_history_snapshot_lands_in_same_engine_db(self):
        # calibrate's audit hook (ledger_db.record_calibration) and the mirror share engine.sqlite;
        # wire them through the real cross-module call and read the history row back.
        L.rebuild(self.csv, self.db)
        cmap = K.build_calibration(K.load_points(self.csv, 2))
        self.assertTrue(L.record_calibration(cmap.get("conf_version"), cmap.get("n_rows"),
                                             cmap, db_path=self.db))
        con = sqlite3.connect(str(self.db))
        try:
            n_rows, mj = con.execute(
                "SELECT n_rows, map_json FROM calibration_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # the mirror table and the history table coexist in one DB (rebuild kept aux tables)
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        self.assertEqual(n_rows, 9)
        self.assertEqual(json.loads(mj)["method"], "isotonic+shrinkage")
        self.assertTrue({"ledger", "calibration_history"} <= tables)


class TestIsotonicMonotoneFromJaggedLedger(unittest.TestCase):
    """The reason store/calibrate uses isotonic regression: even a ledger whose realised hit-rates
    bounce around (the realistic case) must yield a MONOTONE published map. This property is only
    visible across load_points -> build_calibration, not in either alone."""

    def test_jagged_realised_rates_pava_to_monotone_map(self):
        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "led.csv"
        # conf_raw ascends; realised rate deliberately zig-zags (.75,.25,.5,.25,.8,.4)
        rows = [
            _row("AF-1-BTC", 30, 3, 1, "intraday"),
            _row("AF-2-BTC", 40, 1, 3, "intraday"),
            _row("AF-3-BTC", 50, 1, 1, "intraday"),
            _row("AF-4-BTC", 60, 1, 3, "intraday"),
            _row("AF-5-BTC", 70, 4, 1, "intraday"),
            _row("AF-6-BTC", 80, 2, 3, "intraday"),
        ]
        _write_ledger(csv_path, rows)
        pts = K.load_points(csv_path, 2)
        self.assertEqual(len(pts), 6)
        ys = [k[1] for k in K.build_calibration(pts)["knots"]]
        self.assertTrue(all(ys[i] <= ys[i + 1] for i in range(len(ys) - 1)),
                        msg=f"isotonic fit produced a NON-monotone map: {ys}")


class TestYoungLedgerIdentityButStillMirrored(unittest.TestCase):
    """A ledger with < MIN_ROWS scored rows: calibrate must refuse to fit (identity map, no
    'correcting' on noise) while ledger_db still mirrors every row faithfully. The two modules
    legitimately diverge here — the mirror keeps all rows, the fit declines — and that's correct."""

    def test_under_min_rows_identity_map_but_all_rows_in_mirror(self):
        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "led.csv"
        db = tmp / "engine.sqlite"
        rows = [_row(f"AF-{i}-BTC", 50 + i, 1, 1, "intraday") for i in range(3)]  # 3 < MIN_ROWS
        _write_ledger(csv_path, rows)
        summ = L.rebuild(csv_path, db)
        self.assertEqual(summ["db_rows"], 3)               # mirror keeps them all
        cmap = K.build_calibration(K.load_points(csv_path, 2))
        self.assertEqual(cmap["method"], "identity")        # fit declines on a young ledger
        self.assertEqual(cmap["shrinkage_w"], 0.0)
        self.assertEqual(cmap["knots"], [[0.0, 0.0], [100.0, 100.0]])
        self.assertNotIn("by_horizon", cmap)               # nothing reaches MIN_ROWS


class TestBomToleranceAcrossReaders(unittest.TestCase):
    """The ledger writer emits plain utf-8, but a hand-edit (Excel) can prepend a BOM. ledger_db and
    sync_backtest open the file utf-8-SIG; calibrate opens plain utf-8 but reads no first-column
    field — so all three readers agree on row/point counts even with a BOM present."""

    def test_all_three_readers_agree_on_bom_prefixed_ledger(self):
        tmp = Path(tempfile.mkdtemp())
        csv_path = tmp / "led_bom.csv"
        _write_ledger(csv_path, _LEDGER_ROWS, encoding="utf-8-sig")   # write WITH a BOM
        db = tmp / "engine.sqlite"
        self.assertEqual(L.rebuild(csv_path, db)["db_rows"], 9)
        self.assertEqual(len(K.load_points(csv_path, 2)), 9)
        self.assertEqual(len(SB.read_sim_rows(csv_path)), 9)


class TestSidecarProducerToSyncConsumer(unittest.TestCase):
    """The REAL producer (score_report._write_scored_sidecar) -> the REAL consumer
    (sync_backtest.read_pred_rows/map_pred): one wired test that proves the per-prediction sidecar
    field-name + verdict contract has not drifted across the two subdirs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.scored = self.tmp / "scored"
        # Redirect the producer's hard-coded SCORED_DIR into tmp so we never touch data/predictions.
        self._orig = SR.SCORED_DIR
        SR.SCORED_DIR = self.scored

    def tearDown(self):
        SR.SCORED_DIR = self._orig

    def test_real_sidecar_round_trips_through_sync_backtest(self):
        report_id = "AF-202606201201-BTC"
        predictions = [
            {"id": "P1", "type": "level", "text": "H4 close above 70000"},
            {"id": "P2", "type": "manual", "note": "CPI prints hot"},
            {"id": "P3", "type": "touch", "touch": 71000},
            {"id": "P4", "type": "level", "text": "tags 68000"},   # graded-absent -> outcome null
        ]
        results = {"P1": "Y", "P3": "N"}   # P2 manual unresolved -> MANUAL; P4 absent -> None
        SR._write_scored_sidecar(report_id, predictions, results)
        self.assertTrue((self.scored / f"{report_id}.json").exists())

        rows = SB.read_pred_rows(self.scored)
        # one tuple per prediction, in PRED_COLS order, sorted by the sidecar's `sort` index
        self.assertEqual([r[1] for r in rows], ["P1", "P2", "P3", "P4"])   # pred_id (PK 2)
        by_id = {r[1]: r for r in rows}
        self.assertEqual(SB.PRED_COLS,
                         ["report_id", "pred_id", "ptype", "ptext", "manual", "outcome", "sort"])
        # report_id (PK 1) comes from the filename stem for every row
        self.assertTrue(all(r[0] == report_id for r in rows))
        # ptype mirrors the prediction type; manual flag tracks type=='manual'
        self.assertEqual(by_id["P1"][2], "level")
        self.assertEqual(by_id["P1"][3], "H4 close above 70000")     # explicit ptext preserved
        self.assertIs(by_id["P1"][4], False)
        self.assertEqual(by_id["P1"][5], "Y")                         # graded verdict carried through
        self.assertEqual(by_id["P1"][6], 0)                           # sort index
        self.assertEqual(by_id["P2"][2], "manual")
        self.assertIs(by_id["P2"][4], True)
        self.assertEqual(by_id["P2"][5], "MANUAL")                    # unresolved manual -> MANUAL
        self.assertEqual(by_id["P3"][5], "N")
        self.assertIsNone(by_id["P4"][5])                            # JSON null round-trips to None

    def test_map_pred_directly_matches_sidecar_entry_shape(self):
        # the exact dict shape score_report writes, fed straight to the pure mapper
        entry = {"pred_id": "P7", "ptype": "level", "ptext": "x", "manual": False,
                 "sort": 5, "outcome": "Y"}
        t = SB.map_pred("AF-9-BTC", entry)
        self.assertEqual(t, ("AF-9-BTC", "P7", "level", "x", False, "Y", 5))


class _FakeConn:
    """Stand-in for the Neon psycopg connection context manager — the ONE faked external boundary."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


class TestSyncWiredToFakeNeon(unittest.TestCase):
    """read_sim_rows/read_pred_rows (real file I/O + real mapping) -> sync()/sync_predictions(),
    with ONLY engine_ops.connect faked. Asserts the value-tuples forwarded to the UPSERTs are
    exactly the mapped rows derived from the real CSV/sidecar files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "sim_outcome_ledger.csv"
        self.scored = self.tmp / "scored"
        self.scored.mkdir()
        _write_ledger(self.csv, _LEDGER_ROWS[:2])   # two real sandbox rows
        SR.SCORED_DIR = self.scored
        SR._write_scored_sidecar("AF-202606201201-BTC", [
            {"id": "P1", "type": "level", "text": "above 70000"},
            {"id": "P2", "type": "manual", "note": "CPI hot"},
        ], {"P1": "Y"})

    def test_sync_forwards_real_mapped_rows_to_backtest_results(self):
        expected = SB.read_sim_rows(self.csv)
        self.assertEqual(len(expected), 2)
        conn = _FakeConn()
        with mock.patch.object(SB.engine_ops, "connect", return_value=conn):
            n = SB.sync(path=self.csv)
        self.assertEqual(n, 2)
        self.assertEqual([c[1] for c in conn.calls], expected)   # exact tuples, in order
        self.assertTrue(all("backtest_results" in sql.lower() for sql, _ in conn.calls))

    def test_sync_predictions_forwards_real_sidecar_rows(self):
        expected = SB.read_pred_rows(self.scored)
        self.assertEqual(len(expected), 2)                        # P1 + P2 from the real sidecar
        conn = _FakeConn()
        with mock.patch.object(SB.engine_ops, "connect", return_value=conn):
            n = SB.sync_predictions(scored_dir=self.scored)
        self.assertEqual(n, 2)
        self.assertEqual([c[1] for c in conn.calls], expected)
        self.assertTrue(all("backtest_predictions" in sql.lower() for sql, _ in conn.calls))
        # the consumer preserves a hand-graded admin outcome on re-sync (COALESCE old, new)
        self.assertIn("coalesce(backtest_predictions.outcome, excluded.outcome)",
                      SB._PRED_UPSERT_SQL.lower())

    def test_empty_sim_ledger_never_opens_a_connection(self):
        empty = self.tmp / "empty.csv"
        _write_ledger(empty, [])
        with mock.patch.object(SB.engine_ops, "connect") as conn:
            self.assertEqual(SB.sync(path=empty), 0)
        conn.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
