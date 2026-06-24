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

    def test_run_backtest_delegates_sandboxed(self):
        with mock.patch.object(E, "run_and_record", return_value="manual-X") as rar:
            ok, msg, _l, _r = E._cmd_run_backtest(
                None, {"assets": [" BTC ", "ES"], "as_of": "2026-06-17 12:00"})
        self.assertTrue(ok)
        rar.assert_called_once()
        _a, kwargs = rar.call_args
        self.assertEqual(kwargs.get("trigger"), "backtest")
        self.assertTrue(kwargs.get("sandbox"))
        # assets lowercased/trimmed; as_of carried in the scope dict (mirrors the manual path).
        self.assertEqual(kwargs.get("scope"), {"assets": ["btc", "es"], "as_of": "2026-06-17 12:00"})
        self.assertIn("manual-X", msg)


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


if __name__ == "__main__":
    unittest.main()
