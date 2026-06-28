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
import runner
import commands


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
        src = (Path(HERE).parent / "scripts" / "pipeline" / "scaffold_payload.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("ASSETFRAME_SANDBOX") == "1"', src)
        self.assertIn("data/predictions/sim/", src)
        self.assertIn("reports/sim/", src)


# --------------------------------------------------- run_daily wiring (static checks)
class RunDailyWiring(unittest.TestCase):
    """run_daily.py must (a) parse --sandbox, (b) arm ASSETFRAME_SANDBOX + repoint PRED_DIR
    first, and (c) skip the memory refresh under sandbox. Verified against the source so the
    test needs no network/subprocess."""
    src = (Path(HERE).parent / "scripts" / "scheduler" / "run_daily.py").read_text(encoding="utf-8")

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
        with mock.patch.object(commands, "run_backtest_batch", return_value="backtest-X") as rbb:
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
        with mock.patch.object(commands, "run_backtest_batch", return_value="backtest-Y") as rbb:
            ok, msg, _l, _r = E._cmd_run_backtest(
                None, {"assets": ["btc"], "as_of": "2026-06-17 12:00", "days": 5})
        self.assertTrue(ok)
        self.assertEqual(rbb.call_args.kwargs.get("days"), 5)
        self.assertIn("5 days", msg)

    def test_run_backtest_clamps_days_to_max(self):
        with mock.patch.object(commands, "run_backtest_batch", return_value="backtest-Z") as rbb:
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

        with mock.patch.object(runner, "_FileLock", _NoLock), \
             mock.patch.object(runner, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(runner, "_publish_chain") as pub, \
             mock.patch.object(runner, "_read_run_manifest", return_value=(None, None)):
            run_id = E.run_and_record(_RecConn(), trigger="backtest",
                                      scope={"assets": ["btc"], "as_of": "2026-06-17 12:00"},
                                      sandbox=True)
        # --sandbox forwarded to run_daily; publish chain NEVER called.
        self.assertIn("--sandbox", captured["args"])
        pub.assert_not_called()
        self.assertTrue(run_id)

    def test_non_sandbox_still_publishes(self):
        with mock.patch.object(runner, "_FileLock", _NoLock), \
             mock.patch.object(runner, "_exec_run_daily",
                               return_value=("done", {"generated": 1}, None, "log")), \
             mock.patch.object(runner, "_publish_chain", return_value=(True, None, "plog")) as pub, \
             mock.patch.object(runner, "_read_run_manifest", return_value=(None, None)):
            captured_args = []
            orig = runner.scope_to_run_args
            with mock.patch.object(runner, "scope_to_run_args",
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

        with mock.patch.object(runner, "_FileLock", _NoLockBT), \
             mock.patch.object(runner, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(runner, "_run_sync_backtest", return_value=(True, "synced 4")):
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

        with mock.patch.object(runner, "_FileLock", _NoLockBT), \
             mock.patch.object(runner, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(runner, "_run_sync_backtest", return_value=(True, "synced 1")) as sync:
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=1)
        self.assertEqual(len(calls), 1)            # exactly one day
        sync.assert_called_once()                  # sync still runs once for a single day

    def test_batch_syncs_once_after_all_days(self):
        with mock.patch.object(runner, "_FileLock", _NoLockBT), \
             mock.patch.object(runner, "_exec_run_daily",
                               return_value=("done", {"score": {"scored": 2}}, None, "log")), \
             mock.patch.object(runner, "_run_sync_backtest", return_value=(True, "synced 6")) as sync:
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=3)
        sync.assert_called_once()                  # ONE sync for the whole 3-day batch

    def test_batch_clamps_days_to_max(self):
        n_days = []

        def _fake_exec(conn, args, request_id):
            n_days.append(1)
            return "done", {}, None, "log"

        with mock.patch.object(runner, "_FileLock", _NoLockBT), \
             mock.patch.object(runner, "_exec_run_daily", side_effect=_fake_exec), \
             mock.patch.object(runner, "_run_sync_backtest", return_value=(True, "ok")):
            E.run_backtest_batch(_RecConnBT(), ["btc"], "2026-06-17 12:00", days=999)
        self.assertEqual(len(n_days), E.MAX_BACKTEST_DAYS)

    def test_batch_records_one_backtest_run_row(self):
        c = _RecConnBT()
        with mock.patch.object(runner, "_FileLock", _NoLockBT), \
             mock.patch.object(runner, "_exec_run_daily",
                               return_value=("done", {"score": {"scored": 1}}, None, "log")), \
             mock.patch.object(runner, "_run_sync_backtest", return_value=(True, "ok")):
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
            with mock.patch.object(commands, "ROOT", d), mock.patch.object(commands, "_FileLock", _NoLockBT):
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
            with mock.patch.object(commands, "ROOT", d), mock.patch.object(commands, "_FileLock", _NoLockBT):
                ok, result, _l, _r = E._cmd_clear_sandbox(None, {})
            self.assertTrue(ok)
            self.assertIn("none present", result)
        finally:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------- score_report per-prediction sidecar
class _Chdir:
    """Context manager: chdir into `d` for the block, restore cwd after (score_report's LEDGER and
    SCORED_DIR are relative paths, so we run it inside a throwaway temp tree)."""
    def __init__(self, d):
        self.d = str(d)
        self._old = None

    def __enter__(self):
        self._old = os.getcwd()
        os.chdir(self.d)
        return self

    def __exit__(self, *exc):
        os.chdir(self._old)
        return False


# A closed window (well in the past) with a tiny hourly CSV whose final bar settles ABOVE 100,
# inside [90,110], does NOT close below 95, and a "no_close_above_after_touch" whose touch (120) is
# never reached -> P1=Y, P2=Y, P3=N, P4=NT (never-triggered), manual P5 stays MANUAL. Lets the
# sidecar exercise every outcome shape (Y/N/NT/MANUAL).
_PRED_FIXTURE = {
    "report_id": "AF-202001011200-TEST", "instrument": "TEST/USD", "symbol": "TEST=X",
    "view": "Constructive", "confidence": 64,
    "window_start_utc": "2020-01-01 12:00", "window_end_utc": "2020-01-01 14:00",
    "hourly_csv": "data/candles/TEST_hourly.csv",
    "predictions": [
        {"id": "P1", "type": "close_above", "level": 100.0, "expect": True},
        {"id": "P2", "type": "range_inside", "lo": 90.0, "hi": 110.0, "expect": True},
        {"id": "P3", "type": "close_below", "level": 95.0, "expect": True},
        {"id": "P4", "type": "no_close_above_after_touch", "touch": 120.0, "level": 121.0,
         "expect": True},
        {"id": "P5", "type": "manual", "note": "GDP <= -0.3% then a slide within 2h"},
    ],
}
_HOURLY_CSV = (
    "time,open,high,low,close\n"
    "2020-01-01 12:00,100.0,105.0,99.0,102.0\n"
    "2020-01-01 13:00,102.0,108.0,98.0,104.0\n"
)


def _seed_score_tree(d):
    """Write the predictions file + hourly CSV into temp tree `d`; return the predictions path."""
    (d / "data" / "candles").mkdir(parents=True, exist_ok=True)
    (d / "data" / "candles" / "TEST_hourly.csv").write_text(_HOURLY_CSV, encoding="utf-8")
    pred_path = d / "predictions.json"
    import json as _json
    pred_path.write_text(_json.dumps(_PRED_FIXTURE), encoding="utf-8")
    return pred_path


def _run_score(d, pred_path, extra_argv=None):
    """Reimport score_report under the current env, run main() inside temp tree `d`."""
    import json as _json
    S = _reload_score_report()
    argv = ["score_report.py", str(pred_path)] + (extra_argv or [])
    with _Chdir(d), mock.patch.object(sys, "argv", argv):
        try:
            S.main()
        except SystemExit:
            pass
    scored = d / "data" / "predictions" / "sim" / "scored" / "AF-202001011200-TEST.json"
    return (_json.loads(scored.read_text(encoding="utf-8")) if scored.exists() else None), S


class ScoreReportSidecar(unittest.TestCase):
    def setUp(self):
        import tempfile
        import shutil
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.pred = _seed_score_tree(self._tmp)

    def test_sidecar_written_under_sandbox(self):
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "1"}, clear=False):
            entries, _S = _run_score(self._tmp, self.pred)
        self.assertIsNotNone(entries, "sidecar JSON should be written under sandbox")
        self.assertEqual(len(entries), 5)
        # ordered + 0-based sort
        self.assertEqual([e["pred_id"] for e in entries], ["P1", "P2", "P3", "P4", "P5"])
        self.assertEqual([e["sort"] for e in entries], [0, 1, 2, 3, 4])
        by_id = {e["pred_id"]: e for e in entries}
        # outcomes from the graded results
        self.assertEqual(by_id["P1"]["outcome"], "Y")
        self.assertEqual(by_id["P2"]["outcome"], "Y")
        self.assertEqual(by_id["P3"]["outcome"], "N")
        self.assertEqual(by_id["P4"]["outcome"], "NT")
        # manual shape: flag True, outcome MANUAL, ptext comes from the note
        self.assertTrue(by_id["P5"]["manual"])
        self.assertEqual(by_id["P5"]["outcome"], "MANUAL")
        self.assertIn("GDP", by_id["P5"]["ptext"])
        self.assertEqual(by_id["P5"]["ptype"], "manual")
        # auto-graded ones are not flagged manual
        self.assertFalse(by_id["P1"]["manual"])

    def test_sidecar_not_written_in_production(self):
        env = {k: v for k, v in os.environ.items() if k != "ASSETFRAME_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True):
            entries, _S = _run_score(self._tmp, self.pred)
        # production: the (sandbox) scored sidecar dir must not exist at all.
        self.assertIsNone(entries)
        self.assertFalse((self._tmp / "data" / "predictions" / "sim" / "scored").exists())

    def test_sidecar_not_written_on_dry_run(self):
        # --dry-run writes no ledger row, so it must write no sidecar even under sandbox.
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "1"}, clear=False):
            entries, _S = _run_score(self._tmp, self.pred, ["--dry-run"])
        self.assertIsNone(entries)

    def test_resolved_manual_outcome_flows_through(self):
        # an admin-resolved manual (--manual P5=Y) should land as outcome Y, still flagged manual.
        with mock.patch.dict(os.environ, {"ASSETFRAME_SANDBOX": "1"}, clear=False):
            entries, _S = _run_score(self._tmp, self.pred, ["--manual", "P5=Y"])
        p5 = {e["pred_id"]: e for e in entries}["P5"]
        self.assertEqual(p5["outcome"], "Y")
        self.assertTrue(p5["manual"])

    @classmethod
    def tearDownClass(cls):
        env = {k: v for k, v in os.environ.items() if k != "ASSETFRAME_SANDBOX"}
        with mock.patch.dict(os.environ, env, clear=True):
            _reload_score_report()


# --------------------------------------------------- sync_backtest per-prediction sync
class SyncBacktestPredictions(unittest.TestCase):
    def setUp(self):
        import sync_backtest
        self.SB = sync_backtest

    def test_map_pred_shape(self):
        t = self.SB.map_pred("AF-202001011200-TEST",
                              {"pred_id": "P1", "ptype": "close_above", "ptext": "settles above 100",
                               "manual": False, "sort": 0, "outcome": "Y"})
        # PRED_COLS order: report_id, pred_id, ptype, ptext, manual, outcome, sort
        self.assertEqual(t[0], "AF-202001011200-TEST")
        self.assertEqual(t[1], "P1")
        self.assertEqual(t[2], "close_above")
        self.assertEqual(t[3], "settles above 100")
        self.assertIs(t[4], False)
        self.assertEqual(t[5], "Y")
        self.assertEqual(t[6], 0)

    def test_map_pred_manual_and_null_outcome(self):
        t = self.SB.map_pred("R1", {"pred_id": "P5", "ptype": "manual", "ptext": "note here",
                                    "manual": True, "sort": 4, "outcome": None})
        self.assertIs(t[4], True)
        self.assertIsNone(t[5])     # unresolved -> null outcome preserved as None

    def test_map_pred_without_pred_id_skipped(self):
        self.assertIsNone(self.SB.map_pred("R1", {"ptype": "manual"}))
        self.assertIsNone(self.SB.map_pred("R1", {"pred_id": "", "ptype": "x"}))

    def test_read_pred_rows_from_scored_dir(self):
        import tempfile
        import shutil
        import json as _json
        d = Path(tempfile.mkdtemp())
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "AF-202001011200-TEST.json").write_text(_json.dumps([
                {"pred_id": "P1", "ptype": "close_above", "ptext": "above 100",
                 "manual": False, "sort": 0, "outcome": "Y"},
                {"pred_id": "P5", "ptype": "manual", "ptext": "note", "manual": True,
                 "sort": 4, "outcome": "MANUAL"},
            ]), encoding="utf-8")
            rows = self.SB.read_pred_rows(d)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][0], "AF-202001011200-TEST")  # report_id from filename stem
            self.assertEqual(rows[0][1], "P1")
            self.assertEqual(rows[1][1], "P5")
            self.assertIs(rows[1][4], True)
            self.assertEqual(rows[1][5], "MANUAL")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_pred_rows_missing_dir_is_empty(self):
        self.assertEqual(self.SB.read_pred_rows(Path("does/not/exist")), [])

    def test_read_pred_rows_skips_malformed_file(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            (d / "bad.json").write_text("{ not valid json", encoding="utf-8")
            (d / "notalist.json").write_text('{"pred_id":"P1"}', encoding="utf-8")
            self.assertEqual(self.SB.read_pred_rows(d), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_upsert_sql_preserves_manual_outcome_via_coalesce(self):
        # the COALESCE on the existing outcome is the guard that keeps an admin-entered manual grade.
        sql = self.SB._PRED_UPSERT_SQL.lower()
        self.assertIn("on conflict (report_id, pred_id) do update set", sql)
        self.assertIn("outcome = coalesce(backtest_predictions.outcome, excluded.outcome)", sql)
        # the shape fields ARE refreshed from the sidecar.
        for col in ("ptype = excluded.ptype", "ptext = excluded.ptext",
                    "manual = excluded.manual", "sort = excluded.sort"):
            self.assertIn(col, sql)

    def test_sync_predictions_empty_is_clean_no_op(self):
        # no scored/ dir -> 0 and the DB is never connected.
        with mock.patch.object(self.SB, "read_pred_rows", return_value=[]), \
             mock.patch.object(self.SB.engine_ops, "connect") as conn:
            self.assertEqual(self.SB.sync_predictions(), 0)
        conn.assert_not_called()

    def test_sync_predictions_upserts_each_row(self):
        rows = [("R1", "P1", "close_above", "x", False, "Y", 0),
                ("R1", "P2", "manual", "n", True, None, 1)]

        class _Conn:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))

        c = _Conn()
        with mock.patch.object(self.SB, "read_pred_rows", return_value=rows), \
             mock.patch.object(self.SB.engine_ops, "connect", return_value=c):
            n = self.SB.sync_predictions()
        self.assertEqual(n, 2)
        self.assertEqual(len(c.calls), 2)
        self.assertTrue(all("backtest_predictions" in s.lower() for s, _ in c.calls))


if __name__ == "__main__":
    unittest.main()
