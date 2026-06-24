"""Tests for the isolated SANDBOX mode (ASSETFRAME_SANDBOX=1).

A sandbox run must redirect every persistent write under sim/ subtrees and must NEVER
touch production:
  * score_report.LEDGER   -> ledger/sim/outcome_ledger.csv      (backups follow LEDGER.parent)
  * scaffold predictions  -> data/predictions/sim/<N>_predictions.json (no explicit --predictions)
  * scaffold report dir   -> reports/sim/<date>/<TICKER>
  * run_daily score_step  -> SKIPS the calibrate/research_memory/ledger_db refresh
  * engine_ops backtest   -> appends --sandbox, SKIPS the publish chain, tags results

When ASSETFRAME_SANDBOX is UNSET, every path is the live one (byte-identical to before).

These are pure-logic tests: no live DB, no OCI, no subprocess. score_report is reimported
under each env state (its LEDGER is bound at import); the scaffold sim-path logic is exercised
directly so we don't have to drive the whole payload pipeline.
"""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import engine_ops as E


def _reload_score_report():
    """Reimport score_report so its module-level LEDGER re-reads ASSETFRAME_SANDBOX."""
    import score_report
    return importlib.reload(score_report)


# ----------------------------------------------------------- score_report.LEDGER
class LedgerRedirect(unittest.TestCase):
    def test_ledger_under_sim_when_sandbox_set(self):
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "1"}, clear=False):
            S = _reload_score_report()
            self.assertEqual(S.LEDGER, Path("ledger/sim/outcome_ledger.csv"))
            # the backups dir follows LEDGER.parent, so it is sandboxed automatically.
            self.assertEqual(S.LEDGER.parent / "backups", Path("ledger/sim/backups"))

    def test_ledger_is_live_when_sandbox_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "ASSETFRAME_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True):
            S = _reload_score_report()
            self.assertEqual(S.LEDGER, Path("ledger/outcome_ledger.csv"))
            self.assertEqual(S.LEDGER.parent / "backups", Path("ledger/backups"))

    def test_ledger_live_when_sandbox_not_exactly_one(self):
        # only the exact string "1" arms the sandbox — any other value is the live path.
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "0"}, clear=False):
            S = _reload_score_report()
            self.assertEqual(S.LEDGER, Path("ledger/outcome_ledger.csv"))

    @classmethod
    def tearDownClass(cls):
        # leave score_report in its default (env-unset) state for any later test module.
        env = {k: v for k, v in os.environ.items() if k != "ASSETFRAME_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True):
            _reload_score_report()


# --------------------------------------------------- scaffold sim-path logic
# Mirrors the exact branch scaffold_payload.py uses for pred_out + out_dir defaults.
def _scaffold_pred_default(name, sandbox):
    return (f"data/predictions/sim/{name}_predictions.json" if sandbox
            else f"data/predictions/{name}_predictions.json")


def _scaffold_out_dir(report_date, ticker, sandbox):
    return (f"reports/sim/{report_date}/{ticker}" if sandbox
            else f"reports/{report_date}/{ticker}")


class ScaffoldSimPaths(unittest.TestCase):
    def test_predictions_default_sandboxed(self):
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "1"}, clear=False):
            sb = os.environ.get("ASSETFRAME_SANDBOX") == "1"
            self.assertEqual(_scaffold_pred_default("GBPJPY", sb),
                             "data/predictions/sim/GBPJPY_predictions.json")
            self.assertEqual(_scaffold_out_dir("2026-06-12", "GBPJPY", sb),
                             "reports/sim/2026-06-12/GBPJPY")

    def test_predictions_default_live_when_unset(self):
        env = {k: v for k, v in os.environ.items() if k != "ASSETFRAME_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True):
            sb = os.environ.get("ASSETFRAME_SANDBOX") == "1"
            self.assertEqual(_scaffold_pred_default("GBPJPY", sb),
                             "data/predictions/GBPJPY_predictions.json")
            self.assertEqual(_scaffold_out_dir("2026-06-12", "GBPJPY", sb),
                             "reports/2026-06-12/GBPJPY")

    def test_scaffold_source_uses_env_guard(self):
        # Guard against a regression: the live source must gate BOTH defaults on the env var.
        src = Path(HERE, "scaffold_payload.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("ASSETFRAME_SANDBOX") == "1"', src)
        self.assertIn("data/predictions/sim/", src)
        self.assertIn("reports/sim/", src)


# --------------------------------------------------- run_daily wiring (static checks)
class RunDailyWiring(unittest.TestCase):
    """run_daily.py must (a) parse --sandbox, (b) arm ASSETFRAME_SANDBOX + repoint PRED_DIR
    first, and (c) skip the memory refresh under sandbox. Verified against the source so the
    test needs no network/subprocess."""
    src = Path(HERE, "run_daily.py").read_text(encoding="utf-8")

    def test_sandbox_flag_parsed(self):
        self.assertIn('a == "--sandbox"', self.src)
        self.assertIn('"sandbox": False', self.src)

    def test_sandbox_arms_env_and_pred_dir(self):
        self.assertIn('os.environ["ASSETFRAME_SANDBOX"] = "1"', self.src)
        self.assertIn("global PRED_DIR", self.src)
        self.assertIn('"data" / "predictions" / "sim"', self.src)
        self.assertIn('manifest["sandbox"] = True', self.src)

    def test_memory_refresh_skipped_in_sandbox(self):
        self.assertIn('{"skipped": "sandbox"}', self.src)
        # calibrate/research_memory/ledger_db only run in the non-sandbox branch.
        self.assertIn('os.environ.get("ASSETFRAME_SANDBOX") == "1"', self.src)


# --------------------------------------------------- engine_ops backtest handler
class BacktestHandler(unittest.TestCase):
    def test_run_backtest_is_allow_listed(self):
        self.assertIn("run_backtest", E.ALLOWED_COMMANDS)
        self.assertIn("run_backtest", E._COMMAND_HANDLERS)

    def test_run_backtest_requires_asset(self):
        ok, msg, _l, restart = E._cmd_run_backtest(None, {"as_of": "2026-06-17 12:00"})
        self.assertFalse(ok)
        self.assertIn("asset", msg.lower())
        self.assertFalse(restart)

    def test_run_backtest_requires_as_of(self):
        ok, msg, _l, _r = E._cmd_run_backtest(None, {"assets": ["btc"]})
        self.assertFalse(ok)
        self.assertIn("as_of", msg.lower())

    def test_run_backtest_rejects_malformed_as_of(self):
        ok, msg, _l, _r = E._cmd_run_backtest(None, {"assets": ["btc"], "as_of": "nope"})
        self.assertFalse(ok)
        self.assertIn("yyyy-mm-dd", msg.lower())

    def test_run_backtest_delegates_to_batch(self):
        with mock.patch.object(E, "run_backtest_batch", return_value="backtest-X") as rbb:
            ok, msg, _l, _r = E._cmd_run_backtest(
                None, {"assets": [" BTC ", "ES"], "as_of": "2026-06-17 12:00"})
        self.assertTrue(ok)
        rbb.assert_called_once()
        args, kwargs = rbb.call_args
        # assets lowercased/trimmed; as_of trimmed to YYYY-MM-DD HH:MM; days defaults to 1.
        self.assertEqual(args[1], ["btc", "es"])
        self.assertEqual(args[2], "2026-06-17 12:00")
        self.assertEqual(kwargs.get("days"), 1)
        self.assertIn("backtest-X", msg)

    def test_run_backtest_passes_days_through(self):
        with mock.patch.object(E, "run_backtest_batch", return_value="backtest-Y") as rbb:
            ok, msg, _l, _r = E._cmd_run_backtest(
                None, {"assets": ["btc"], "as_of": "2026-06-17 12:00", "days": 5})
        self.assertTrue(ok)
        self.assertEqual(rbb.call_args.kwargs.get("days"), 5)
        self.assertIn("5 days", msg)

    def test_run_backtest_clamps_days_to_max(self):
        with mock.patch.object(E, "run_backtest_batch", return_value="backtest-Z") as rbb:
            E._cmd_run_backtest(None, {"assets": ["btc"], "as_of": "2026-06-17 12:00", "days": 999})
        self.assertEqual(rbb.call_args.kwargs.get("days"), E.MAX_BACKTEST_DAYS)

    def test_run_backtest_rejects_days_below_one(self):
        ok, msg, _l, _r = E._cmd_run_backtest(
            None, {"assets": ["btc"], "as_of": "2026-06-17 12:00", "days": 0})
        self.assertFalse(ok)
        self.assertIn(">= 1", msg)

    def test_run_backtest_rejects_non_integer_days(self):
        ok, msg, _l, _r = E._cmd_run_backtest(
            None, {"assets": ["btc"], "as_of": "2026-06-17 12:00", "days": "lots"})
        self.assertFalse(ok)
        self.assertIn("integer", msg.lower())


# ------------------------------------------ run_and_record sandbox isolation
class _NoLock:
    class Locked(Exception):
        pass

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecConn:
    """Minimal connection that swallows every execute (run_and_record only writes status rows)."""
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

        class _Cur:
            def fetchone(self_):
                return None

            def fetchall(self_):
                return []
        return _Cur()


class RunAndRecordSandbox(unittest.TestCase):
    def test_sandbox_appends_flag_and_skips_publish(self):
        captured = {}

        def _fake_exec(conn, args, request_id):
            captured["args"] = args
            return "done", {"generated": 1}, None, "log"

        with mock.patch.object(E, "_FileLock", _NoLock), \
             mock.patch.object(E, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(E, "_publish_chain") as pub, \
             mock.patch.object(E, "_read_run_manifest", return_value=(None, None)):
            run_id = E.run_and_record(_RecConn(), trigger="backtest",
                                      scope={"assets": ["btc"], "as_of": "2026-06-17 12:00"},
                                      sandbox=True)
        # --sandbox forwarded to run_daily; publish chain NEVER called.
        self.assertIn("--sandbox", captured["args"])
        pub.assert_not_called()
        self.assertTrue(run_id)

    def test_non_sandbox_still_publishes(self):
        with mock.patch.object(E, "_FileLock", _NoLock), \
             mock.patch.object(E, "_exec_run_daily",
                               return_value=("done", {"generated": 1}, None, "log")), \
             mock.patch.object(E, "_publish_chain", return_value=(True, None, "plog")) as pub, \
             mock.patch.object(E, "_read_run_manifest", return_value=(None, None)):
            captured_args = []
            orig = E.scope_to_run_args
            with mock.patch.object(E, "scope_to_run_args",
                                   side_effect=lambda s: captured_args.extend(orig(s)) or orig(s)):
                E.run_and_record(_RecConn(), trigger="manual",
                                 scope={"assets": ["btc"]}, request_id="r1")
        pub.assert_called_once()
        self.assertNotIn("--sandbox", captured_args)


# --------------------------------------------------- multi-day backtest batch
class _NoLockBT:
    class Locked(Exception):
        pass

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecConnBT:
    """Connection that records executes; run_backtest_batch only writes status/result rows."""
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

        class _Cur:
            def fetchone(self_):
                return None

            def fetchall(self_):
                return []
        return _Cur()


class BacktestBatchDays(unittest.TestCase):
    def test_backdated_as_of_counts_back_same_hhmm(self):
        self.assertEqual(E._backdated_as_of("2026-06-17 12:00", 0), "2026-06-17 12:00")
        self.assertEqual(E._backdated_as_of("2026-06-17 12:00", 1), "2026-06-16 12:00")
        self.assertEqual(E._backdated_as_of("2026-06-17 12:00", 3), "2026-06-14 12:00")
        # crosses a month boundary cleanly.
        self.assertEqual(E._backdated_as_of("2026-07-01 09:30", 2), "2026-06-29 09:30")

    def test_days_loop_produces_n_distinct_backdated_as_ofs(self):
        seen_as_ofs = []

        def _fake_exec(conn, args, request_id):
            # capture the --as-of run_daily was handed for this day.
            self.assertIn("--sandbox", args)
            idx = args.index("--as-of")
            seen_as_ofs.append(args[idx + 1])
            return "done", {"generated": 1, "score": {"scored": 1}}, None, "log"

        with mock.patch.object(E, "_FileLock", _NoLockBT), \
             mock.patch.object(E, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(E, "_run_sync_backtest", return_value=(True, "synced 4")):
            run_id = E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=4)
        # 4 days -> 4 distinct, descending backdated as_ofs (day 0..3).
        self.assertEqual(seen_as_ofs,
                         ["2026-06-17 12:00", "2026-06-16 12:00",
                          "2026-06-15 12:00", "2026-06-14 12:00"])
        self.assertEqual(len(set(seen_as_ofs)), 4)
        self.assertTrue(run_id.startswith("backtest-"))

    def test_distinct_backdated_report_ids_across_days(self):
        # report_id = AF-YYYYMMDDHHMM-TICKER for a backdated run, so different days -> distinct ids.
        ids = []
        for k in range(3):
            as_of = E._backdated_as_of("2026-06-17 12:00", k)
            stamp = as_of.replace("-", "").replace(":", "").replace(" ", "")  # YYYYMMDDHHMM
            ids.append(f"AF-{stamp}-BTC")
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(ids[0], "AF-202606171200-BTC")
        self.assertEqual(ids[1], "AF-202606161200-BTC")

    def test_batch_runs_one_day_for_days_one(self):
        calls = []

        def _fake_exec(conn, args, request_id):
            calls.append(args)
            return "done", {"generated": 1, "score": {"scored": 1}}, None, "log"

        with mock.patch.object(E, "_FileLock", _NoLockBT), \
             mock.patch.object(E, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(E, "_run_sync_backtest", return_value=(True, "synced 1")) as sync:
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=1)
        self.assertEqual(len(calls), 1)            # exactly one day
        sync.assert_called_once()                  # sync still runs once for a single day

    def test_batch_syncs_once_after_all_days(self):
        with mock.patch.object(E, "_FileLock", _NoLockBT), \
             mock.patch.object(E, "_exec_run_daily",
                               return_value=("done", {"score": {"scored": 2}}, None, "log")), \
             mock.patch.object(E, "_run_sync_backtest", return_value=(True, "synced 6")) as sync:
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=3)
        sync.assert_called_once()                  # ONE sync for the whole 3-day batch

    def test_batch_clamps_days_to_max(self):
        n_days = []

        def _fake_exec(conn, args, request_id):
            n_days.append(1)
            return "done", {}, None, "log"

        with mock.patch.object(E, "_FileLock", _NoLockBT), \
             mock.patch.object(E, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(E, "_run_sync_backtest", return_value=(True, "ok")):
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=999)
        self.assertEqual(len(n_days), E.MAX_BACKTEST_DAYS)

    def test_batch_records_one_backtest_run_row(self):
        c = _RecConnBT()
        with mock.patch.object(E, "_FileLock", _NoLockBT), \
             mock.patch.object(E, "_exec_run_daily",
                               return_value=("done", {"score": {"scored": 1}}, None, "log")), \
             mock.patch.object(E, "_run_sync_backtest", return_value=(True, "ok")):
            E.run_backtest_batch(c, ["btc"], "2026-06-17 12:00", days=2)
        sql_log = " || ".join(s.lower() for s, _ in c.executed)
        # exactly one engine_runs INSERT with trigger 'backtest'.
        self.assertIn("insert into engine_runs", sql_log)
        self.assertEqual(sql_log.count("insert into engine_runs"), 1)
        self.assertIn("'backtest'", sql_log)
        # the final UPDATE carries the batch summary (sandbox=true, days, total_scored).
        upd = [p for s, p in c.executed if "update engine_runs set status" in s.lower()]
        self.assertTrue(upd)
        results_json = upd[-1][1]   # (status, results_json, errors, log, id)
        self.assertIn('"sandbox": true', results_json.lower())
        self.assertIn('"days": 2', results_json)
        self.assertIn("total_scored", results_json)


# --------------------------------------------------- sync_backtest column mapping
class SyncBacktestMapping(unittest.TestCase):
    """The CSV->backtest_results row mapping is pure logic — test it without any DB."""

    def setUp(self):
        import sync_backtest
        self.SB = sync_backtest

    def test_maps_ledger_columns_to_table_tuple(self):
        row = {
            "scored_at_utc": "2026-06-17T13:05:00Z", "report_id": "AF-202606171200-BTC",
            "instrument": "BTC-USD", "view": "Bullish", "confidence": "72",
            "window_end_utc": "2026-06-17T18:00:00Z", "results": "hit-hit-miss",
            "hits": "2", "misses": "1", "hit_rate_pct": "66.7",
            "asset_class": "crypto", "horizon": "intraday",
        }
        t = self.SB.map_row(row)
        # TABLE_COLS order: report_id, ticker, instrument, asset_class, view, confidence,
        #                   horizon, window_end, results, hits, misses, hit_rate, scored_at
        self.assertEqual(t[0], "AF-202606171200-BTC")    # report_id
        self.assertEqual(t[1], "BTC")                    # ticker = report_id.rsplit('-',1)[-1]
        self.assertEqual(t[2], "BTC-USD")                # instrument
        self.assertEqual(t[3], "crypto")                 # asset_class
        self.assertEqual(t[4], "Bullish")                # view
        self.assertEqual(t[5], 72)                       # confidence -> int
        self.assertEqual(t[6], "intraday")               # horizon
        self.assertEqual(t[7], "2026-06-17T18:00:00Z")   # window_end <- window_end_utc
        self.assertEqual(t[8], "hit-hit-miss")           # results
        self.assertEqual(t[9], 2)                        # hits -> int
        self.assertEqual(t[10], 1)                       # misses -> int
        self.assertEqual(t[11], 66.7)                    # hit_rate (numeric) <- hit_rate_pct
        self.assertEqual(t[12], "2026-06-17T13:05:00Z")  # scored_at <- scored_at_utc

    def test_blank_numeric_cells_become_none(self):
        row = {"report_id": "AF-202606171200-ES", "confidence": "", "hits": "",
               "misses": "", "hit_rate_pct": ""}
        t = self.SB.map_row(row)
        self.assertIsNone(t[5])    # confidence
        self.assertIsNone(t[9])    # hits
        self.assertIsNone(t[10])   # misses
        self.assertIsNone(t[11])   # hit_rate

    def test_ticker_is_last_dash_segment(self):
        self.assertEqual(self.SB._ticker_from_report_id("AF-202606171200-GBPJPY"), "GBPJPY")
        self.assertEqual(self.SB._ticker_from_report_id("AF-20260617-XAU"), "XAU")

    def test_row_without_report_id_is_skipped(self):
        self.assertIsNone(self.SB.map_row({"report_id": "", "instrument": "X"}))
        self.assertIsNone(self.SB.map_row({"instrument": "X"}))

    def test_read_sim_rows_builds_tuples_from_a_tiny_ledger(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            cols = ",".join(_LEDGER_HEADER)
            r1 = _ledger_line(report_id="AF-202606171200-BTC", instrument="BTC-USD",
                              confidence="70", hits="2", misses="1", hit_rate_pct="66.7",
                              asset_class="crypto", horizon="intraday")
            r2 = _ledger_line(report_id="AF-202606161200-BTC", instrument="BTC-USD",
                              confidence="", hits="", misses="", hit_rate_pct="",
                              asset_class="crypto", horizon="intraday")
            p = d / "sim_ledger.csv"
            p.write_text(cols + "\n" + r1 + "\n" + r2 + "\n", encoding="utf-8")
            rows = self.SB.read_sim_rows(p)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "AF-202606171200-BTC")
            self.assertEqual(rows[0][1], "BTC")
            self.assertEqual(rows[0][5], 70)
            self.assertIsNone(rows[1][5])   # blank confidence -> None
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_sim_rows_missing_file_is_empty(self):
        self.assertEqual(self.SB.read_sim_rows(Path("does/not/exist.csv")), [])

    def test_sync_exits_clean_when_no_rows(self):
        # sync() returns 0 (and never touches the DB) when there are no rows to push.
        with mock.patch.object(self.SB, "read_sim_rows", return_value=[]), \
             mock.patch.object(self.SB.engine_ops, "connect") as conn:
            self.assertEqual(self.SB.sync(), 0)
        conn.assert_not_called()


# Build a ledger header/row matching score_report.LEDGER_COLS so the mapping test uses the real schema.
import score_report as _SR   # noqa: E402
_LEDGER_HEADER = _SR.LEDGER_COLS


def _ledger_line(**vals):
    return ",".join(str(vals.get(c, "")) for c in _LEDGER_HEADER)


# --------------------------------------------------- clear_sandbox handler
class ClearSandboxHandler(unittest.TestCase):
    def test_clear_sandbox_is_allow_listed(self):
        self.assertIn("clear_sandbox", E.ALLOWED_COMMANDS)
        self.assertIn("clear_sandbox", E._COMMAND_HANDLERS)

    def test_clear_sandbox_empties_only_sim_trees(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            # sandbox trees (should be cleared) + a live tree (must survive untouched).
            (d / "ledger" / "sim").mkdir(parents=True)
            (d / "ledger" / "sim" / "outcome_ledger.csv").write_text("x", encoding="utf-8")
            (d / "reports" / "sim" / "2026-06-17" / "BTC").mkdir(parents=True)
            (d / "reports" / "sim" / "2026-06-17" / "BTC" / "free.pdf").write_text("x", encoding="utf-8")
            (d / "data" / "predictions" / "sim").mkdir(parents=True)
            (d / "data" / "predictions" / "sim" / "1_predictions.json").write_text("{}", encoding="utf-8")
            (d / "ledger" / "outcome_ledger.csv").write_text("LIVE", encoding="utf-8")  # live ledger
            (d / "reports" / "2026-06-17" / "ES").mkdir(parents=True)                   # live report
            with mock.patch.object(E, "ROOT", d), mock.patch.object(E, "_FileLock", _NoLockBT):
                ok, _result, _l, _r = E._cmd_clear_sandbox(None, {})
            self.assertTrue(ok)
            # sim trees emptied...
            self.assertEqual(list((d / "ledger" / "sim").iterdir()), [])
            self.assertEqual(list((d / "reports" / "sim").iterdir()), [])
            self.assertEqual(list((d / "data" / "predictions" / "sim").iterdir()), [])
            # ...live trees untouched.
            self.assertEqual((d / "ledger" / "outcome_ledger.csv").read_text(encoding="utf-8"), "LIVE")
            self.assertTrue((d / "reports" / "2026-06-17" / "ES").is_dir())
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clear_sandbox_no_sim_dirs_is_ok(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(E, "ROOT", d), mock.patch.object(E, "_FileLock", _NoLockBT):
                ok, result, _l, _r = E._cmd_clear_sandbox(None, {})
            self.assertTrue(ok)
            self.assertIn("none present", result)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
