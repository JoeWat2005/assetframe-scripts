"""INTEGRATION tests for scripts/coordination/* — the REAL modules WIRED TOGETHER.

Phase 1 (tests/test_coordination_unit.py) covered each module in isolation. This file instead drives
the CROSS-MODULE FLOWS + data contracts of the command/run-state lifecycle, calling the REAL functions
across db / runner / manifest / locking / commands / control_server / config_loader and faking ONLY
true external boundaries (the Neon psycopg conn -> FakeConn, and subprocess spawning of run_daily /
export / publish / sync / sync_backtest). Everything in-process stays real:

  * claim -> run -> record   : db.claim_next_request -> runner.run_and_record -> db.RunRecorder.start/
                               finish -> runner._exec_run_daily -> manifest._read_run_manifest +
                               summarize_manifest -> runner._publish_chain -> runner._finish_request.
                               Asserts the scope->args->Popen handoff and the generate/score-only/no-op
                               publish branches + the engine_runs/generation_requests/engine_state SQL.
  * command lifecycle        : db.claim_next_command -> commands.run_command -> a REAL filesystem-only
                               handler (set_config -> config/engine.json in a tmp ROOT; reset_ledger /
                               clear_reports -> tmp dirs) -> commands._finish_command.
  * config round-trip        : set_config WRITES engine.json; config_loader.apply_runtime_env READS it
                               back into os.environ — the seam that previously dropped keys silently.
  * control_server           : submit_command allow-list + _run_job dispatch through the REAL
                               engine_ops.run_command façade (fake engine_ops.connect), AND the
                               threaded job path with an injected fake runner.
  * EngineDB poller tick     : heartbeat + reap_stale_runs + reap_stale_commands + both empty claims
                               composed on one FakeConn (the two-phase claim transactions nest cleanly).
  * backtest lifecycle       : commands.run_command(run_backtest) -> runner.run_backtest_batch ->
                               RunRecorder(trigger_literal) multi-day -> _wipe_sandbox_state +
                               _run_sync_backtest.

Run:  python -m pytest tests/test_coordination_integration.py -q
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg  # noqa: F401  (kept for parity with the sibling suites' import style)

import db
import runner
import manifest
import commands
import locking          # noqa: F401
import wake             # noqa: F401
import engine_ops
import control_server as CS
import config_loader


# ===================================================================== fakes
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records executed SQL; returns scripted rows keyed by a lowercase SQL substring (first match
    wins). Models only the Neon boundary — every coordination module under test is real."""

    def __init__(self, results=None, raise_on=None, raise_exc=None):
        self.results = results or {}
        self.executed = []
        self.tx_depth = 0
        self.closed = False
        self._raise_on = raise_on
        self._raise_exc = raise_exc or RuntimeError

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self._raise_on and self._raise_on in sql.lower():
            raise self._raise_exc("boom")
        rows = None
        for key, val in self.results.items():
            if key in sql.lower():
                rows = val(params) if callable(val) else val
                break
        return FakeCursor(rows)

    def transaction(self):
        outer = self

        class _Tx:
            def __enter__(self_):
                outer.tx_depth += 1
                return self_

            def __exit__(self_, *exc):
                outer.tx_depth -= 1
                return False
        return _Tx()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # ---- query helpers used by the assertions --------------------------------
    def find(self, needle):
        """All (sql, params) whose lowercased SQL contains `needle`."""
        return [(s, p) for s, p in self.executed if needle in s.lower()]

    def first(self, needle):
        hits = self.find(needle)
        return hits[0] if hits else None


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProc:
    """subprocess.Popen stand-in: poll() returns 0 immediately so _exec_run_daily finishes at once."""

    def __init__(self):
        self._done = False

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


# ===================================================================== helpers
def _make_root():
    return Path(tempfile.mkdtemp(prefix="af-coord-int-"))


def _write_manifest(root, payload, date="2026-06-28"):
    """Write a real runs/<date>/run_manifest.json with a FAR-FUTURE mtime so _read_run_manifest's
    `since` filter (captured at Popen time) always accepts it."""
    sub = root / "runs" / date
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "run_manifest.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    future = 2_000_000_000          # year 2033 — newer than any test's `start`
    os.utime(f, (future, future))
    return f


# ===================================================================== claim -> run -> record
class ClaimRunRecord(unittest.TestCase):
    """db.claim_next_request -> runner.run_and_record (real), faking ONLY the Neon conn + the
    run_daily/export/publish/sync subprocesses. Exercises RunRecorder + manifest + locking + the
    publish chain as one wired lifecycle."""

    def setUp(self):
        self.root = _make_root()
        self.lock = self.root / ".run.lock"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _claim_conn(self):
        # the two-phase claim: phase-1 drain returns none, phase-2 hands back the queued row.
        return FakeConn({
            "set status = 'cancelled'": [],
            "set status = 'running'": [{"id": "r-100", "scope": {"assets": ["btc"]},
                                        "status": "running"}],
            "select cancel_requested": [{"cancel_requested": False}],
        })

    def _run_with_manifest(self, conn, manifest_payload):
        """Drive claim -> run_and_record with a real manifest file + faked subprocesses. Returns
        (run_id, captured_popen_cmds, publish_steps)."""
        _write_manifest(self.root, manifest_payload)
        popen_cmds = []
        publish_steps = []

        def _fake_popen(cmd, **kw):
            popen_cmds.append(cmd)
            return FakeProc()

        def _fake_run(cmd, **kw):
            publish_steps.append(cmd)
            return FakeCompleted(0, "ok")

        # claim exactly like the poller does (id + scope contract), then run.
        row = db.claim_next_request(conn)
        self.assertIsNotNone(row, "claim should hand back the queued row")
        with mock.patch.object(runner, "LOCK_PATH", self.lock), \
             mock.patch.object(manifest, "ROOT", self.root), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=_fake_popen), \
             mock.patch.object(runner.subprocess, "run", side_effect=_fake_run):
            run_id = runner.run_and_record(conn, trigger="manual", scope=row.get("scope"),
                                           request_id=row.get("id"))
        return run_id, popen_cmds, publish_steps

    def _finish_results(self, conn):
        """The results jsonb written by RunRecorder.finish (the terminal engine_runs UPDATE)."""
        hit = conn.first("update engine_runs set status")
        self.assertIsNotNone(hit, "RunRecorder.finish must UPDATE engine_runs")
        _sql, params = hit
        self.assertEqual(params[0], "done", f"run should finish 'done' (errors={params[2]!r})")
        return json.loads(params[1]) if params[1] else {}

    def test_generate_day_publishes_and_records_full_lifecycle(self):
        conn = self._claim_conn()
        payload = {"run_id": "daily-x", "mode": "production", "run_date": "2026-06-28",
                   "generated": 1,
                   "jobs": [{"asset_id": "btc", "ticker": "BTC", "status": "generated",
                             "report_id": "AF-202606281200-BTC", "errors": None}]}
        run_id, popen_cmds, publish_steps = self._run_with_manifest(conn, payload)

        # run id derives from the request id (manifest._new_run_id contract).
        self.assertEqual(run_id, "req-r-100")
        # scope -> scope_to_run_args -> the run_daily Popen argv (cross-module handoff).
        self.assertEqual(len(popen_cmds), 1)
        argv = popen_cmds[0]
        self.assertIn("--asset", argv)
        self.assertIn("btc", argv)
        self.assertIn("scripts.scheduler.run.run_daily", argv)
        # generated -> the publish chain ran export -> publish -> sync IN ORDER.
        joined = [" ".join(str(x) for x in c) for c in publish_steps]
        self.assertEqual(len(joined), 3)
        self.assertIn("export_content", joined[0])
        self.assertIn("publish", joined[1])
        self.assertIn("sync-db", joined[2])
        # results jsonb carries the summarized manifest + publish==ok.
        results = self._finish_results(conn)
        self.assertEqual(results.get("generated"), 1)
        self.assertEqual(results.get("publish"), "ok")
        self.assertEqual(results.get("assets"), [{"asset_id": "btc", "ticker": "BTC",
                                                  "status": "generated",
                                                  "report_id": "AF-202606281200-BTC"}])
        # engine_runs INSERT (start) used the request-derived run id + 'running'.
        ins = conn.first("insert into engine_runs")
        self.assertIsNotNone(ins)
        self.assertEqual(ins[1][0], "req-r-100")
        # current_run_id is claimed (=run id) then cleared (=None) — load-bearing ordering.
        crun = [p for s, p in conn.executed if "set current_run_id" in s.lower()]
        self.assertEqual(crun[0][0], "req-r-100")
        self.assertIsNone(crun[-1][0])
        # the generation_requests row is finished 'done' (the parameterized finish UPDATE, distinct
        # from the claim's literal cancel-drain).
        freq = conn.first("update generation_requests set status = %s")
        self.assertEqual(freq[1][0], "done")
        self.assertEqual(freq[1][1], "req-r-100")   # run_id back-reference

    def test_score_only_day_still_publishes(self):
        # generated=0 but a window was scored -> the publish chain MUST still run (the fix that keeps
        # the track record from being stranded in the local CSV). score.scored is a LIST in the
        # manifest; summarize_manifest reduces it to a count that run_and_record reads as truthy.
        conn = self._claim_conn()
        payload = {"run_id": "daily-x", "generated": 0, "jobs": [],
                   "score": {"scored": ["btc"], "skipped": [], "errors": []}}
        run_id, popen_cmds, publish_steps = self._run_with_manifest(conn, payload)
        self.assertEqual(len(publish_steps), 3, "score-only day must export+publish+sync")
        results = self._finish_results(conn)
        self.assertEqual(results.get("publish"), "ok")
        self.assertEqual(results.get("score"), {"scored": 1, "skipped": 0, "errors": 0})

    def test_noop_day_skips_publish(self):
        # nothing generated AND nothing scored -> publish chain is skipped (sync-db's anti-wipe guard
        # would otherwise trip on empty content). Not a failure.
        conn = self._claim_conn()
        payload = {"run_id": "daily-x", "generated": 0, "jobs": []}
        run_id, popen_cmds, publish_steps = self._run_with_manifest(conn, payload)
        self.assertEqual(publish_steps, [], "a no-op day must NOT run the publish chain")
        results = self._finish_results(conn)
        self.assertEqual(results.get("publish"), "skipped (nothing generated or scored)")


# ===================================================================== command lifecycle
class CommandLifecycle(unittest.TestCase):
    """db.claim_next_command -> commands.run_command -> a REAL filesystem handler -> _finish_command,
    all on a tmp ROOT. The handlers + dispatch + allow-list are real; only the Neon conn is faked."""

    def setUp(self):
        self.root = _make_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _command_conn(self, command, args):
        return FakeConn({
            "engine_commands set status = 'cancelled'": [],
            "engine_commands set status = 'running'": [
                {"id": "cmd-1", "command": command, "args": args}],
        })

    def test_set_config_via_run_command_round_trips_into_environ(self):
        conn = self._command_conn("set_config",
                                  {"key": "ASSETFRAME_RETENTION_DAYS", "value": "30"})
        row = commands.claim_next_command(conn)          # real two-phase claim
        self.assertEqual(row["command"], "set_config")
        with mock.patch.object(commands, "ROOT", self.root):
            res = commands.run_command(conn, row)        # real dispatch -> real _cmd_set_config
        self.assertEqual(res["status"], "done")
        # the file the handler wrote:
        cfgp = self.root / "config" / "engine.json"
        self.assertTrue(cfgp.exists())
        self.assertEqual(json.loads(cfgp.read_text())["ASSETFRAME_RETENTION_DAYS"], "30")
        # the engine_commands outcome row (_finish_command) was recorded against the claimed id.
        fin = conn.first("update engine_commands set status = %s")
        self.assertEqual(fin[1][0], "done")
        self.assertEqual(fin[1][3], "cmd-1")
        # CROSS-MODULE ROUND TRIP: config_loader.apply_runtime_env must seed the written key into the
        # environment (the seam where an allow-list drift would silently drop a setting).
        with mock.patch.dict(os.environ, {}, clear=True):
            config_loader.apply_runtime_env(cfgp)
            self.assertEqual(os.environ.get("ASSETFRAME_RETENTION_DAYS"), "30")

    def test_reset_ledger_via_run_command(self):
        led = self.root / "ledger" / "outcome_ledger.csv"
        led.parent.mkdir(parents=True, exist_ok=True)
        led.write_text("report_id,asset,grade\nAF-1,BTC,hit\nAF-2,XAU,miss\n", encoding="utf-8")
        conn = self._command_conn("reset_ledger", {})
        row = commands.claim_next_command(conn)
        with mock.patch.object(commands, "ROOT", self.root):
            res = commands.run_command(conn, row)
        self.assertEqual(res["status"], "done")
        self.assertEqual(led.read_text(encoding="utf-8"), "report_id,asset,grade\n")
        self.assertIn("2 rows cleared", res["result"])

    def test_clear_reports_via_run_command_leaves_ledger_untouched(self):
        # populate the working dirs the handler clears + a ledger it must NOT touch.
        for sub in ("reports/2026-06-28", "data/payloads", "data/predictions", "content", "runs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
            (self.root / sub / "x.bin").write_text("stale", encoding="utf-8")
        (self.root / "ledger").mkdir(parents=True, exist_ok=True)
        (self.root / "ledger" / "outcome_ledger.csv").write_text("keep", encoding="utf-8")
        conn = self._command_conn("clear_reports", {})
        row = commands.claim_next_command(conn)
        with mock.patch.object(commands, "ROOT", self.root), \
             mock.patch.object(commands, "LOCK_PATH", self.root / ".run.lock"):
            res = commands.run_command(conn, row)        # real lock on a tmp path
        self.assertEqual(res["status"], "done")
        self.assertEqual(list((self.root / "reports").iterdir()), [])
        self.assertEqual(list((self.root / "data" / "payloads").iterdir()), [])
        self.assertTrue((self.root / "ledger" / "outcome_ledger.csv").exists())  # live ledger kept

    def test_unknown_command_is_recorded_failed_not_run(self):
        conn = self._command_conn("definitely_not_a_command", {})
        row = commands.claim_next_command(conn)
        res = commands.run_command(conn, row)
        self.assertEqual(res["status"], "failed")
        self.assertIn("unknown command", res["result"])
        fin = conn.first("update engine_commands set status = %s")
        self.assertEqual(fin[1][0], "failed")


# ===================================================================== config contract
class ConfigContract(unittest.TestCase):
    def test_settable_config_keys_match_config_loader_runtime_keys(self):
        # set_config's allow-list (commands._SETTABLE_CONFIG_KEYS) and the keys apply_runtime_env
        # actually seeds (config_loader.SETTABLE_RUNTIME_KEYS) MUST match — else set_config "succeeds"
        # but the value never reaches os.environ on the next restart (a silent no-op the codebase has
        # been bitten by before). This guards against future drift between the two modules.
        self.assertEqual(set(commands._SETTABLE_CONFIG_KEYS),
                         set(config_loader.SETTABLE_RUNTIME_KEYS))

    def test_every_validated_key_is_also_settable(self):
        # a value-validator with no matching settable key would be dead (never reachable).
        self.assertTrue(set(commands._CONFIG_VALUE_VALIDATORS).issubset(
            set(commands._SETTABLE_CONFIG_KEYS)))


# ===================================================================== control_server
class ControlServerDispatch(unittest.TestCase):
    """control_server.submit_command + _run_job wired to the REAL engine_ops.run_command façade
    (which dispatches into commands), faking ONLY engine_ops.connect."""

    def setUp(self):
        self._jobs = dict(CS._JOBS)
        self._seq = list(CS._JOB_SEQ)
        CS._JOBS.clear()
        CS._JOB_SEQ[0] = 0
        self.root = _make_root()

    def tearDown(self):
        CS._JOBS.clear()
        CS._JOBS.update(self._jobs)
        CS._JOB_SEQ[0] = self._seq[0]
        shutil.rmtree(self.root, ignore_errors=True)

    def test_submit_command_allowlist_contract(self):
        # restart-only commands are refused here (they need the poller process).
        code, body = CS.submit_command("pull_latest", {}, spawn=False)
        self.assertEqual(code, 400)
        self.assertIn("only via the poller", body["error"])
        # unknown verb refused.
        code, body = CS.submit_command("bogus", {}, spawn=False)
        self.assertEqual(code, 400)
        # non-dict args refused.
        code, body = CS.submit_command("service_check", [1, 2], spawn=False)
        self.assertEqual(code, 400)
        # an allowed, in-process verb is accepted (job created, not yet run because spawn=False).
        code, body = CS.submit_command("set_config", {"key": "x"}, spawn=False)
        self.assertEqual(code, 202)
        self.assertEqual(body["status"], "running")
        self.assertIn(body["job_id"], CS._JOBS)

    def test_run_job_dispatches_through_real_run_command(self):
        # control_server._run_job -> engine_ops.run_command (façade=commands.run_command) ->
        # _cmd_set_config, on a faked Neon conn. The absent engine_commands id makes the bookkeeping
        # UPDATE a no-op (no row inserted), exactly as documented.
        conn = FakeConn()
        code, body = CS.submit_command("set_config",
                                       {"key": "ASSETFRAME_RETENTION_DAYS", "value": "21"},
                                       spawn=False)
        jid = body["job_id"]
        with mock.patch.object(engine_ops, "connect", return_value=conn), \
             mock.patch.object(commands, "ROOT", self.root):
            CS._run_job(jid, "set_config",
                        {"key": "ASSETFRAME_RETENTION_DAYS", "value": "21"})
        st_code, job = CS.job_status(jid)
        self.assertEqual(st_code, 200)
        self.assertEqual(job["status"], "done")
        self.assertIn("set ASSETFRAME_RETENTION_DAYS", job["result"])
        self.assertEqual(
            json.loads((self.root / "config" / "engine.json").read_text())["ASSETFRAME_RETENTION_DAYS"],
            "21")
        # bookkeeping UPDATE ran with a NULL id (no-op against real Postgres).
        fin = conn.first("update engine_commands set status = %s")
        self.assertIsNotNone(fin)
        self.assertIsNone(fin[1][3])

    def test_run_job_invalid_config_value_records_failed_job(self):
        # the handler's validator rejects a non-integer timeout; the failure propagates to the job.
        conn = FakeConn()
        jid = CS._new_job("set_config", {})
        with mock.patch.object(engine_ops, "connect", return_value=conn), \
             mock.patch.object(commands, "ROOT", self.root):
            CS._run_job(jid, "set_config",
                        {"key": "ASSETFRAME_RUN_TIMEOUT", "value": "not-an-int"})
        _code, job = CS.job_status(jid)
        self.assertEqual(job["status"], "failed")
        self.assertIn("not valid", job["result"])
        self.assertFalse((self.root / "config" / "engine.json").exists())

    def test_submit_command_spawns_job_with_injected_runner(self):
        # the THREADED path: submit_command(spawn=True, runner=fake) actually starts a worker thread
        # that runs the injected runner and records its outcome on the in-memory job.
        ev = threading.Event()
        seen = {}

        def _fake_runner(conn, payload):
            seen["payload"] = payload
            ev.set()
            return {"status": "done", "result": "faked ok", "log": "L"}

        with mock.patch.object(engine_ops, "connect", return_value=FakeConn()):
            code, body = CS.submit_command("service_check", {"k": 1}, spawn=True,
                                           runner=_fake_runner)
            self.assertEqual(code, 202)
            jid = body["job_id"]
            self.assertTrue(ev.wait(3.0), "worker thread never ran the injected runner")
            # wait for the finally-block to stamp the outcome.
            deadline = time.time() + 3.0
            while time.time() < deadline and CS.job_status(jid)[1]["finished_at"] is None:
                time.sleep(0.01)
        _c, job = CS.job_status(jid)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["result"], "faked ok")
        self.assertEqual(job["log"], "L")
        self.assertEqual(seen["payload"], {"command": "service_check", "args": {"k": 1}})


# ===================================================================== EngineDB poller tick
class EngineDBTick(unittest.TestCase):
    """The EngineDB heartbeat + reap + claim methods composed as one poller pass on a single conn —
    the two-phase claim transactions must nest cleanly alongside the unguarded UPDATEs."""

    def test_heartbeat_reap_claim_compose_without_interference(self):
        conn = FakeConn()           # no queued rows scripted -> both claims return None
        db.heartbeat(conn)
        db.reap_stale_runs(conn)
        commands.reap_stale_commands(conn)
        self.assertIsNone(commands.claim_next_command(conn))
        self.assertIsNone(db.claim_next_request(conn))

        log = " || ".join(s.lower() for s, _ in conn.executed)
        self.assertIn("update engine_state set last_heartbeat_at", log)      # heartbeat
        self.assertIn("update engine_runs set status = 'failed'", log)        # reap orphaned runs
        self.assertIn("update engine_state set current_run_id = null", log)   # stale current_run clear
        self.assertIn("update engine_commands set status = 'failed'", log)    # reap stale commands
        # reap default age == RUN_TIMEOUT + 1h (db -> runner shared constant).
        reap = [p for s, p in conn.executed if "make_interval" in s.lower()]
        self.assertEqual(reap[0][0], db.RUN_TIMEOUT + 3600)
        # the transactions opened by the two claims all closed (balanced enter/exit).
        self.assertEqual(conn.tx_depth, 0)


# ===================================================================== backtest lifecycle
class BacktestLifecycle(unittest.TestCase):
    """commands.run_command(run_backtest) -> runner.run_backtest_batch -> RunRecorder(trigger_literal)
    over multiple days -> _wipe_sandbox_state + _run_sync_backtest. Subprocesses + Neon faked."""

    def setUp(self):
        self.root = _make_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_two_day_backtest_records_literal_trigger_and_summary(self):
        # leftover sim state that _wipe_sandbox_state must clear before the run.
        for sub in runner.SANDBOX_DIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)
            (self.root / sub / "old.json").write_text("{}", encoding="utf-8")
        _write_manifest(self.root, {"run_id": "bt", "generated": 1,
                                    "score": {"scored": ["btc"], "skipped": [], "errors": []},
                                    "jobs": []})
        conn = FakeConn()
        popen_cmds = []

        def _fake_popen(cmd, **kw):
            popen_cmds.append(cmd)
            return FakeProc()

        cmd_row = {"id": "cmd-bt", "command": "run_backtest",
                   "args": {"assets": ["btc"], "as_of": "2026-06-10 12:00", "days": 2}}
        with mock.patch.object(runner, "ROOT", self.root), \
             mock.patch.object(runner, "LOCK_PATH", self.root / ".run.lock"), \
             mock.patch.object(manifest, "ROOT", self.root), \
             mock.patch.object(runner.subprocess, "Popen", side_effect=_fake_popen), \
             mock.patch.object(runner.subprocess, "run", return_value=FakeCompleted(0, "synced")):
            res = commands.run_command(conn, cmd_row)

        self.assertEqual(res["status"], "done")
        # two days each spawned run_daily with --sandbox (no publish path).
        self.assertEqual(len(popen_cmds), 2)
        for argv in popen_cmds:
            self.assertIn("--sandbox", argv)
            self.assertIn("btc", argv)
        # the engine_runs row used the LITERAL 'backtest' trigger (RunRecorder trigger_literal=True):
        ins = conn.first("insert into engine_runs")
        self.assertIn("'backtest'", ins[0].lower())
        self.assertEqual(len(ins[1]), 2)        # literal form binds (run_id, scope) only — no trigger param
        # the summary results jsonb reduced from the per-day manifests:
        fin = conn.first("update engine_runs set status = %s")
        self.assertEqual(fin[1][0], "done")
        results = json.loads(fin[1][1])
        self.assertTrue(results["sandbox"])
        self.assertEqual(results["days"], 2)
        self.assertEqual(len(results["day_runs"]), 2)
        self.assertEqual(results["total_scored"], 2)   # 1 scored/day * 2 days (int contract)
        # sandbox dirs were wiped + the Neon backtest tables cleared.
        for sub in runner.SANDBOX_DIRS:
            self.assertEqual(list((self.root / sub).iterdir()), [], f"{sub} not wiped")
        deletes = " ".join(s.lower() for s, _ in conn.executed if s.lower().startswith("delete from"))
        self.assertIn("backtest_results", deletes)
        self.assertIn("backtest_predictions", deletes)
        # the command outcome row recorded done against the claimed id.
        cfin = conn.first("update engine_commands set status = %s")
        self.assertEqual(cfin[1][3], "cmd-bt")


if __name__ == "__main__":
    unittest.main(verbosity=2)
