"""Tests for the OCI engine runner's PURE logic — no live DB, no OCI, no subprocess.

We exercise:
  * scope_to_run_args  : {all_due}->--mode production; {assets:[a,b]}->--asset a --asset b
  * claim_next_request : the cancel-drain + claim-oldest SQL/state transitions
  * is_paused / heartbeat / set_current_run / is_cancel_requested SQL
  * the pause contract  : scheduled_run respects automation_paused; the manual poller
                          path does NOT (run_and_record is invoked regardless)
  * summarize_manifest : run_manifest.json -> compact results jsonb
  * database_url        : missing DATABASE_URL -> ConfigError (clear, not a stack trace)

The DB is a FakeConn that records every SQL string + params and returns scripted rows,
so we assert on the SQL the code asks Postgres to run and on the resulting row state.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import engine_ops as E
import db
import commands
import poller
import scheduled_run


# --------------------------------------------------------------------- fakes
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records executed SQL; returns scripted results.

    `results` maps a lowercase substring of the SQL to either a list of row-dicts
    (used as fetchone/fetchall) or a callable(params)->rows. Unmatched statements
    return no rows. Supports the .transaction() context manager used by claim.
    """
    def __init__(self, results=None):
        self.results = results or {}
        self.executed = []          # list of (sql, params)
        self.tx_depth = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = None
        for key, val in self.results.items():
            if key in sql.lower():
                rows = val(params) if callable(val) else val
                break
        return FakeCursor(rows)

    def transaction(self):
        conn = self

        class _Tx:
            def __enter__(self_):
                conn.tx_depth += 1
                return self_

            def __exit__(self_, *exc):
                conn.tx_depth -= 1
                return False
        return _Tx()

    # context-manager so `with engine_ops.connect() as conn:` works in patched tests.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sql_log(self):
        return " || ".join(s.lower() for s, _ in self.executed)


# --------------------------------------------------------- scope -> run args
class ScopeMapping(unittest.TestCase):
    def test_all_due_maps_to_production(self):
        self.assertEqual(E.scope_to_run_args({"all_due": True}),
                         ["--mode", "production"])

    def test_assets_map_to_repeated_asset_flags(self):
        self.assertEqual(
            E.scope_to_run_args({"assets": ["aapl", "btc"]}),
            ["--mode", "production", "--asset", "aapl", "--asset", "btc"])

    def test_asset_ids_lowercased_and_trimmed(self):
        self.assertEqual(
            E.scope_to_run_args({"assets": [" AAPL ", "Btc"]}),
            ["--mode", "production", "--asset", "aapl", "--asset", "btc"])

    def test_scope_accepts_json_string(self):
        self.assertEqual(
            E.scope_to_run_args('{"assets": ["es"]}'),
            ["--mode", "production", "--asset", "es"])

    def test_empty_or_unknown_scope_defaults_to_production(self):
        self.assertEqual(E.scope_to_run_args({}), ["--mode", "production"])
        self.assertEqual(E.scope_to_run_args(None), ["--mode", "production"])
        self.assertEqual(E.scope_to_run_args("not json"), ["--mode", "production"])

    def test_none_entries_in_assets_skipped(self):
        self.assertEqual(
            E.scope_to_run_args({"assets": ["aapl", None]}),
            ["--mode", "production", "--asset", "aapl"])

    def test_as_of_appended_when_valid(self):
        self.assertEqual(
            E.scope_to_run_args({"assets": ["btc"], "as_of": "2026-06-17 12:00"}),
            ["--mode", "production", "--asset", "btc", "--as-of", "2026-06-17 12:00"])
        self.assertEqual(
            E.scope_to_run_args({"all_due": True, "as_of": "2026-06-17 12:00"}),
            ["--mode", "production", "--as-of", "2026-06-17 12:00"])

    def test_as_of_ignored_when_malformed_or_empty(self):
        self.assertEqual(
            E.scope_to_run_args({"assets": ["btc"], "as_of": "garbage"}),
            ["--mode", "production", "--asset", "btc"])
        self.assertEqual(E.scope_to_run_args({"all_due": True, "as_of": ""}), ["--mode", "production"])


# ------------------------------------------------------- engine_state SQL
class StateSql(unittest.TestCase):
    def test_heartbeat_updates_singleton(self):
        c = FakeConn()
        E.heartbeat(c)
        sql = c.executed[0][0].lower()
        self.assertIn("update engine_state", sql)
        self.assertIn("last_heartbeat_at = now()", sql)
        self.assertIn("where id = 1", sql)

    def test_is_paused_true_false(self):
        self.assertTrue(E.is_paused(FakeConn(
            {"select automation_paused": [{"automation_paused": True}]})))
        self.assertFalse(E.is_paused(FakeConn(
            {"select automation_paused": [{"automation_paused": False}]})))
        # no row at all -> treated as not paused.
        self.assertFalse(E.is_paused(FakeConn({"select automation_paused": []})))

    def test_set_current_run_sets_and_clears(self):
        c = FakeConn()
        E.set_current_run(c, "req-123")
        self.assertEqual(c.executed[-1][1], ("req-123",))
        E.set_current_run(c, None)
        self.assertEqual(c.executed[-1][1], (None,))
        self.assertIn("current_run_id", c.executed[-1][0].lower())

    def test_is_cancel_requested(self):
        yes = FakeConn({"select cancel_requested": [{"cancel_requested": True}]})
        no = FakeConn({"select cancel_requested": [{"cancel_requested": False}]})
        self.assertTrue(E.is_cancel_requested(yes, "r1"))
        self.assertFalse(E.is_cancel_requested(no, "r1"))
        # no request_id -> never hits the DB.
        empty = FakeConn()
        self.assertFalse(E.is_cancel_requested(empty, None))
        self.assertEqual(empty.executed, [])


# --------------------------------------------------- claim_next_request
class ClaimRequest(unittest.TestCase):
    def test_claim_drains_cancelled_then_claims_oldest(self):
        claimed_row = {"id": "r2", "scope": {"all_due": True}, "status": "running"}
        c = FakeConn({
            "set status = 'cancelled'": [{"id": "r1"}],     # a queued+cancelled row drained
            "set status = 'running'": [claimed_row],         # then the oldest claimed
        })
        row = E.claim_next_request(c)
        self.assertEqual(row, claimed_row)
        log = c.sql_log()
        # cancel-drain runs BEFORE the claim.
        self.assertLess(log.index("set status = 'cancelled'"),
                        log.index("set status = 'running'"))
        # claim uses the no-double-claim primitives + oldest-first ordering.
        self.assertIn("for update skip locked", log)
        self.assertIn("order by created_at limit 1", log)
        # the cancel-drain marks finished_at and filters on cancel_requested = true.
        self.assertIn("cancel_requested = true", log)
        self.assertIn("finished_at = now()", log)
        # both steps ran inside an explicit transaction.
        self.assertEqual(c.tx_depth, 0)   # balanced enter/exit

    def test_claim_returns_none_when_queue_empty(self):
        c = FakeConn({"set status = 'cancelled'": [], "set status = 'running'": []})
        self.assertIsNone(E.claim_next_request(c))


# ------------------------------------------------ RunRecorder lifecycle
class RunRecorderLifecycle(unittest.TestCase):
    """db.RunRecorder folds the engine_runs row-lifecycle envelope shared by
    runner.run_and_record + runner.run_backtest_batch: the 'running' INSERT (+ON CONFLICT
    reset) and current_run_id claim on start(); the terminal UPDATE + current_run_id clear
    on finish(). These lock in the EXACT SQL the admin console reads and reap_stale_runs
    depends on — the FakeConn matches by lowercase substring, exactly as the other DB-SQL
    tests above do."""

    def test_start_inserts_running_row_and_claims_current_run(self):
        c = FakeConn()
        rec = db.RunRecorder(c, "req-7", "manual", {"assets": ["btc"]})
        self.assertTrue(rec.start())
        log = c.sql_log()
        # the run row is INSERTed 'running' with the trigger BOUND as a parameter...
        self.assertIn("insert into engine_runs", log)
        self.assertIn("values (%s, %s, %s, 'running', now())", log)
        # ...and the ON CONFLICT (id) branch RESETS results/errors/log_excerpt/finished_at +
        # status back to 'running' (a re-run of the same id starts clean).
        self.assertIn("on conflict (id) do update set trigger = excluded.trigger", log)
        self.assertIn("results = null, errors = null, log_excerpt = null, finished_at = null", log)
        # INSERT params: (run_id, trigger, scope_json).
        ins_sql, ins_params = c.executed[0]
        self.assertIn("insert into engine_runs", ins_sql.lower())
        self.assertEqual(ins_params[0], "req-7")
        self.assertEqual(ins_params[1], "manual")
        self.assertEqual(json.loads(ins_params[2]), {"assets": ["btc"]})
        # current_run_id is claimed to the run id (the engine_state singleton).
        self.assertIn("update engine_state", log)
        self.assertIn("current_run_id", log)
        self.assertEqual(c.executed[-1][1], ("req-7",))

    def test_start_backtest_embeds_literal_trigger(self):
        # run_backtest_batch's form: trigger embedded as a SQL literal (not a bound param), so
        # 'backtest' must appear IN the INSERT text (a regression guard the batch test relies on).
        c = FakeConn()
        rec = db.RunRecorder(c, "backtest-X", "backtest",
                             {"assets": ["btc"], "days": 2}, trigger_literal=True)
        self.assertTrue(rec.start())
        log = c.sql_log()
        self.assertIn("values (%s, 'backtest', %s, 'running', now())", log)
        self.assertIn("on conflict (id) do update set trigger = 'backtest'", log)
        # only (run_id, scope_json) bind — the trigger is a literal, not a param.
        ins_params = c.executed[0][1]
        self.assertEqual(len(ins_params), 2)
        self.assertEqual(ins_params[0], "backtest-X")
        self.assertEqual(json.loads(ins_params[1]), {"assets": ["btc"], "days": 2})

    def test_finish_records_outcome_and_clears_current_run(self):
        c = FakeConn()
        rec = db.RunRecorder(c, "req-7", "manual", {})
        rec.finish("done", {"generated": 1}, None, "log tail")
        upd_sql, upd_params = c.executed[0]
        s = upd_sql.lower()
        self.assertIn("update engine_runs set status = %s, results = %s, errors = %s,", s)
        self.assertIn("log_excerpt = %s, finished_at = now() where id = %s", s)
        # params: (status, results_json, errors, log, run_id).
        self.assertEqual(upd_params[0], "done")
        self.assertEqual(json.loads(upd_params[1]), {"generated": 1})
        self.assertIsNone(upd_params[2])
        self.assertEqual(upd_params[3], "log tail")
        self.assertEqual(upd_params[4], "req-7")
        # current_run_id is cleared (set to None) in the SAME finish() call.
        self.assertEqual(c.executed[-1][1], (None,))
        self.assertIn("current_run_id", c.executed[-1][0].lower())

    def test_finish_failed_status_nulls_empty_results(self):
        c = FakeConn()
        rec = db.RunRecorder(c, "daily-2026-06-28", "schedule", {})
        rec.finish("failed", {}, "boom", "trace")
        upd_params = c.executed[0][1]
        self.assertEqual(upd_params[0], "failed")
        self.assertIsNone(upd_params[1])     # an empty results dict serialises to NULL, not '{}'
        self.assertEqual(upd_params[2], "boom")
        self.assertEqual(upd_params[4], "daily-2026-06-28")

    def test_start_returns_false_on_db_error(self):
        # If the very first INSERT can't run (e.g. the table is missing), start() must report
        # failure WITHOUT raising — the caller (run_and_record / run_backtest_batch) decides how
        # to bail. The exception text is stashed so run_and_record can surface it.
        rec = db.RunRecorder(_MissingTableConn(), "req-7", "manual", {})
        self.assertFalse(rec.start())
        self.assertTrue(rec.start_error)


# ----------------------------------------------- the pause contract
class PauseContract(unittest.TestCase):
    """Manual requests ignore automation_paused; scheduled runs respect it."""

    def test_scheduled_run_skips_when_paused(self):
        c = FakeConn({"select automation_paused": [{"automation_paused": True}]})
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=c), \
             mock.patch.object(scheduled_run.engine_ops, "run_and_record") as rar:
            rc = scheduled_run.main()
        self.assertEqual(rc, 0)
        rar.assert_not_called()                      # NO run when paused
        # it recorded a skip note (an engine_runs insert mentioning the skip).
        self.assertIn("insert into engine_runs", c.sql_log())
        self.assertIn("automation_paused", c.sql_log())

    def test_scheduled_run_runs_when_active(self):
        c = FakeConn({"select automation_paused": [{"automation_paused": False}]})
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=c), \
             mock.patch.object(scheduled_run.engine_ops, "run_and_record",
                               return_value="daily-2026-06-18") as rar:
            rc = scheduled_run.main()
        self.assertEqual(rc, 0)
        rar.assert_called_once()
        # trigger='schedule', scope={all_due:true}
        _args, kwargs = rar.call_args
        self.assertEqual(kwargs.get("trigger"), "schedule")
        self.assertEqual(kwargs.get("scope"), {"all_due": True})

    def test_poller_tick_runs_manual_even_when_paused(self):
        # engine_state says PAUSED, but a request is queued -> the poller still runs it.
        claimed = {"id": "rX", "scope": {"assets": ["btc"]}, "status": "running"}
        # poller.tick drains the queue (claim-until-empty), so the FakeConn must model the
        # queue emptying: the row is claimable ONCE, then gone. A static row makes the real
        # claim succeed every iteration and _drain would spin forever (this was the CI hang).
        _claims = [[claimed]]
        c = FakeConn({
            "select automation_paused": [{"automation_paused": True}],
            # tick() now drains the engine_commands queue (empty here) BEFORE the
            # generation_requests queue — keys are table-qualified so the fake tells the two
            # claim queries apart (in prod they hit different tables; the substring fake can't
            # distinguish a bare "set status = 'running'").
            "engine_commands set status = 'cancelled'": [],
            "engine_commands set status = 'running'": [],
            "generation_requests set status = 'cancelled'": [],
            "generation_requests set status = 'running'": lambda _p: (_claims.pop(0) if _claims else []),
        })
        with mock.patch.object(poller.engine_ops, "run_and_record",
                               return_value="req-rX") as rar:
            run_id = poller.tick(c)
        self.assertEqual(run_id, "req-rX")
        rar.assert_called_once()
        _a, kwargs = rar.call_args
        self.assertEqual(kwargs.get("trigger"), "manual")
        self.assertEqual(kwargs.get("request_id"), "rX")
        self.assertEqual(kwargs.get("scope"), {"assets": ["btc"]})
        # crucially, the poller never consulted automation_paused.
        self.assertNotIn("select automation_paused", c.sql_log())

    def test_poller_tick_noop_when_no_request(self):
        c = FakeConn({"set status = 'cancelled'": [], "set status = 'running'": []})
        with mock.patch.object(poller.engine_ops, "run_and_record") as rar:
            self.assertIsNone(poller.tick(c))
        rar.assert_not_called()
        self.assertIn("update engine_state", c.sql_log())   # still heartbeats


# ------------------------------------------------ manifest summarisation
class ManifestSummary(unittest.TestCase):
    def test_summarize_picks_headline_and_per_asset(self):
        manifest = {
            "run_id": "daily-2026-06-18", "mode": "production", "run_date": "2026-06-18",
            "assets_selected": 2, "assets_due": 2, "generated": 1,
            "needs_brief": ["XAU"], "brief_rejected": [], "brief_stand_aside": [],
            "token_cost": {"est_cost_usd": 0.12},
            "score": {"scored": [1, 2], "skipped": [1], "errors": []},
            "jobs": [
                {"asset_id": "btc", "ticker": "BTC", "status": "generated",
                 "report_id": "AF-1", "errors": []},
                {"asset_id": "xau", "ticker": "XAU", "status": "needs_brief",
                 "report_id": None, "errors": [{"brief": "no key"}]},
            ],
        }
        s = E.summarize_manifest(manifest)
        self.assertEqual(s["run_id"], "daily-2026-06-18")
        self.assertEqual(s["generated"], 1)
        self.assertEqual(s["score"], {"scored": 2, "skipped": 1, "errors": 0})
        self.assertEqual(len(s["assets"]), 2)
        self.assertEqual(s["assets"][0], {"asset_id": "btc", "ticker": "BTC",
                                          "status": "generated", "report_id": "AF-1"})
        self.assertEqual(s["token_cost"], {"est_cost_usd": 0.12})
        # job errors bubble up.
        self.assertTrue(any(e["ticker"] == "XAU" for e in s["job_errors"]))

    def test_summarize_handles_non_dict(self):
        self.assertEqual(E.summarize_manifest(None), {})
        self.assertEqual(E.summarize_manifest("oops"), {})


# ------------------------------------------------ run id derivation
class RunId(unittest.TestCase):
    def test_request_run_id(self):
        self.assertEqual(E._new_run_id("manual", "abc"), "req-abc")

    def test_schedule_run_id_is_dated(self):
        rid = E._new_run_id("schedule", None)
        self.assertTrue(rid.startswith("daily-"))


# -------------------------------------------- request status mapping
class RequestStatus(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(E._request_status("done"), "done")
        self.assertEqual(E._request_status("failed"), "failed")
        self.assertEqual(E._request_status("cancelled"), "cancelled")
        self.assertEqual(E._request_status("weird"), "failed")

    def test_finish_request_noop_without_id(self):
        c = FakeConn()
        E._finish_request(c, None, "done", "run-1", None)
        self.assertEqual(c.executed, [])

    def test_finish_request_updates_row(self):
        c = FakeConn()
        E._finish_request(c, "r9", "done", "req-r9", None)
        sql, params = c.executed[-1]
        self.assertIn("update generation_requests", sql.lower())
        self.assertEqual(params, ("done", "req-r9", None, "r9"))


# ----------------------------------------------- DATABASE_URL resolution
class DatabaseUrl(unittest.TestCase):
    def test_missing_database_url_raises_clear_configerror(self):
        # No DATABASE_URL in env AND no .env on disk -> ConfigError with a clear message.
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(db, "_load_dotenv_into_environ", lambda: None):
            with self.assertRaises(E.ConfigError) as ctx:
                E.database_url()
        self.assertIn("DATABASE_URL", str(ctx.exception))
        # it's a clean message, not a traceback fragment.
        self.assertIn("not set", str(ctx.exception))

    def test_present_database_url_returned(self):
        with mock.patch.dict(os.environ, {"DATABASE_URL": "postgres://x"}, clear=True):
            self.assertEqual(E.database_url(), "postgres://x")

    def test_connect_surfaces_configerror_when_missing(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(db, "_load_dotenv_into_environ", lambda: None):
            with self.assertRaises(E.ConfigError):
                E.connect()


# ------------------------------------------------ engine_commands (box control)
class _MissingTableConn(FakeConn):
    """A connection whose every execute raises UndefinedTable — models the engine_commands
    table not existing yet (migration 1750000020000 not applied on this branch)."""
    def execute(self, sql, params=None):
        raise E.psycopg.errors.UndefinedTable("relation \"engine_commands\" does not exist")


class _NoLock:
    """Stand-in for engine_ops._FileLock that always acquires (so command tests don't touch a
    real .run.lock). Exposes .Locked so the handlers' `except _FileLock.Locked` stays valid."""
    class Locked(Exception):
        pass

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class CommandQueue(unittest.TestCase):
    """The engine_commands control channel: claim, dispatch, allow-list, restart gating."""

    def setUp(self):
        # tick()/_drain_commands read+set the module-global _STOP; isolate it per test.
        self._stop = poller._STOP
        poller._STOP = False

    def tearDown(self):
        poller._STOP = self._stop

    def test_claim_command_drains_cancelled_then_claims_oldest(self):
        claimed = {"id": "c2", "command": "restart_poller", "args": {}, "status": "running"}
        c = FakeConn({
            "engine_commands set status = 'cancelled'": [{"id": "c1"}],
            "engine_commands set status = 'running'": [claimed],
        })
        row = E.claim_next_command(c)
        self.assertEqual(row, claimed)
        log = c.sql_log()
        self.assertLess(log.index("set status = 'cancelled'"), log.index("set status = 'running'"))
        self.assertIn("for update skip locked", log)
        self.assertIn("order by created_at limit 1", log)
        self.assertEqual(c.tx_depth, 0)   # balanced transactions

    def test_claim_command_none_when_queue_empty(self):
        c = FakeConn({"engine_commands set status = 'cancelled'": [],
                      "engine_commands set status = 'running'": []})
        self.assertIsNone(E.claim_next_command(c))

    def test_claim_command_none_when_table_missing(self):
        # Migration not applied yet -> UndefinedTable -> quiet None (no log spam, no crash).
        self.assertIsNone(E.claim_next_command(_MissingTableConn()))

    def test_run_command_unknown_verb_is_rejected(self):
        c = FakeConn()
        res = E.run_command(c, {"id": "c9", "command": "rm -rf /", "args": {}})
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["restart"])
        sql, params = c.executed[-1]
        self.assertIn("update engine_commands set status", sql.lower())
        self.assertEqual(params[0], "failed")          # status recorded
        self.assertIn("unknown command", (params[1] or ""))

    def test_run_command_restart_poller_sets_restart(self):
        c = FakeConn()
        res = E.run_command(c, {"id": "c1", "command": "restart_poller", "args": {}})
        self.assertEqual(res["status"], "done")
        self.assertTrue(res["restart"])
        self.assertEqual(c.executed[-1][1][0], "done")  # outcome recorded BEFORE any exit

    def test_run_command_suppresses_restart_when_handler_fails(self):
        # A handler that wants restart but reports failure must NOT trigger a restart.
        c = FakeConn()
        with mock.patch.dict(E._COMMAND_HANDLERS,
                             {"boom": lambda conn, args: (False, "nope", None, True)}):
            res = E.run_command(c, {"id": "c3", "command": "boom", "args": {}})
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["restart"])

    def test_run_command_handler_exception_recorded_not_raised(self):
        def _raiser(conn, args):
            raise RuntimeError("kaboom")
        c = FakeConn()
        with mock.patch.dict(E._COMMAND_HANDLERS, {"boom": _raiser}):
            res = E.run_command(c, {"id": "c4", "command": "boom", "args": {}})
        self.assertEqual(res["status"], "failed")
        self.assertFalse(res["restart"])
        self.assertIn("command error", (c.executed[-1][1][1] or ""))

    def test_run_maintenance_invokes_publish_chain(self):
        c = FakeConn()
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands, "_publish_chain", return_value=(True, None, "log tail")) as pc:
            ok, result, log, restart = E._cmd_run_maintenance(c, {})
        pc.assert_called_once()
        self.assertTrue(ok)
        self.assertFalse(restart)
        self.assertEqual(log, "log tail")

    def test_run_maintenance_reports_publish_failure(self):
        c = FakeConn()
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands, "_publish_chain", return_value=(False, "publish exited 2", "log")):
            ok, result, _log, restart = E._cmd_run_maintenance(c, {})
        self.assertFalse(ok)
        self.assertIn("publish exited 2", result)

    def test_set_config_allowlist_replace_and_reject(self):
        import json as _json
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(commands, "ROOT", d):
                cfgp = d / "config" / "engine.json"
                cfgp.parent.mkdir(parents=True, exist_ok=True)
                # set_config now writes config/engine.json (the single runtime-settings file), NOT .env.
                # replaces an existing key, preserves others.
                cfgp.write_text(_json.dumps({"ASSETFRAME_AUTHOR_BRIEFS": "0", "OTHER": "keep"}), encoding="utf-8")
                ok, _r, _l, _rs = E._cmd_set_config(None, {"key": "ASSETFRAME_AUTHOR_BRIEFS", "value": "1"})
                self.assertTrue(ok)
                cfg = _json.loads(cfgp.read_text(encoding="utf-8"))
                self.assertEqual(cfg["ASSETFRAME_AUTHOR_BRIEFS"], "1")
                self.assertEqual(cfg["OTHER"], "keep")     # untouched keys preserved
                # disallowed key (e.g. a secret) is rejected and never written
                ok2, _r2, _l2, _rs2 = E._cmd_set_config(None, {"key": "DATABASE_URL", "value": "postgres://x"})
                self.assertFalse(ok2)
                self.assertNotIn("DATABASE_URL", _json.loads(cfgp.read_text(encoding="utf-8")))
                # newline-injection value is rejected
                ok3, _r3, _l3, _rs3 = E._cmd_set_config(None, {"key": "ASSETFRAME_AUTHOR_BRIEFS", "value": "a\nEVIL=1"})
                self.assertFalse(ok3)
                # per-key value validation: a non-integer / out-of-range ASSETFRAME_RUN_TIMEOUT is
                # rejected (it is int()-parsed at import; a bad value would crash-loop the poller).
                self.assertFalse(E._cmd_set_config(None, {"key": "ASSETFRAME_RUN_TIMEOUT", "value": "abc"})[0])
                self.assertFalse(E._cmd_set_config(None, {"key": "ASSETFRAME_RUN_TIMEOUT", "value": "999999999"})[0])
                self.assertFalse(E._cmd_set_config(None, {"key": "ASSETFRAME_RUN_TIMEOUT", "value": ""})[0])
                self.assertTrue(E._cmd_set_config(None, {"key": "ASSETFRAME_RUN_TIMEOUT", "value": "300"})[0])
                self.assertEqual(_json.loads(cfgp.read_text(encoding="utf-8"))["ASSETFRAME_RUN_TIMEOUT"], "300")
                # a key removed from the allow-list is no longer settable
                self.assertFalse(E._cmd_set_config(None, {"key": "ASSETFRAME_MAX_WORKERS", "value": "8"})[0])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_int_env_falls_back_on_garbage(self):
        # A garbage value must NOT raise (that would crash the module at import -> systemd loop).
        with mock.patch.dict(os.environ, {"AF_T": "abc"}, clear=False):
            self.assertEqual(E._int_env("AF_T", 5400), 5400)
        with mock.patch.dict(os.environ, {"AF_T": "120"}, clear=False):
            self.assertEqual(E._int_env("AF_T", 5400), 120)
        self.assertEqual(E._int_env("AF_DOES_NOT_EXIST_NOPE", 7), 7)

    def test_reap_stale_commands_marks_running_failed(self):
        c = FakeConn()
        E.reap_stale_commands(c)
        sql, _params = c.executed[-1]
        self.assertIn("update engine_commands set status = 'failed'", sql.lower())
        self.assertIn("where status = 'running'", sql.lower())

    def test_reap_stale_commands_swallows_missing_table(self):
        E.reap_stale_commands(_MissingTableConn())   # must not raise if not migrated yet

    def test_sync_assets_refuses_empty_universe(self):
        # An empty engine_assets table must NEVER overwrite config/assets.json with nothing.
        c = FakeConn({"from engine_assets": []})
        ok, result, _l, _r = E._cmd_sync_assets(c, {})
        self.assertFalse(ok)
        self.assertIn("empty", (result or "").lower())

    def test_sync_assets_none_when_table_missing(self):
        ok, result, _l, _r = E._cmd_sync_assets(_MissingTableConn(), {})
        self.assertFalse(ok)
        self.assertIn("not migrated", (result or "").lower())

    def test_reset_ledger_keeps_only_header(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            (d / "ledger").mkdir()
            (d / "ledger" / "outcome_ledger.csv").write_text("report_id,hits\nAF-1,2\nAF-2,3\n", encoding="utf-8")
            with mock.patch.object(commands, "ROOT", d):
                ok, _result, _l, _r = E._cmd_reset_ledger(None, {})
            self.assertTrue(ok)
            self.assertEqual((d / "ledger" / "outcome_ledger.csv").read_text(encoding="utf-8").strip(), "report_id,hits")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clear_reports_empties_working_dirs(self):
        import tempfile
        import shutil
        d = Path(tempfile.mkdtemp())
        try:
            (d / "reports" / "2026-06-20" / "BTC").mkdir(parents=True)
            (d / "reports" / "2026-06-20" / "BTC" / "free.pdf").write_text("x", encoding="utf-8")
            (d / "runs").mkdir()
            (d / "runs" / "m.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(commands, "ROOT", d), mock.patch.object(commands, "_FileLock", _NoLock):
                ok, _result, _l, _r = E._cmd_clear_reports(None, {})
            self.assertTrue(ok)
            self.assertEqual(list((d / "reports").iterdir()), [])
            self.assertEqual(list((d / "runs").iterdir()), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_poller_drain_commands_returns_restart(self):
        cmd = {"id": "cX", "command": "restart_poller", "args": {}}
        _claims = [[cmd]]   # claimable ONCE, then the queue is empty (no infinite loop)
        c = FakeConn({
            "engine_commands set status = 'cancelled'": [],
            "engine_commands set status = 'running'": lambda _p: (_claims.pop(0) if _claims else []),
        })
        self.assertTrue(poller._drain_commands(c))

    def test_poller_tick_restart_command_skips_generation(self):
        cmd = {"id": "cR", "command": "restart_poller", "args": {}}
        _claims = [[cmd]]
        c = FakeConn({
            "engine_commands set status = 'cancelled'": [],
            "engine_commands set status = 'running'": lambda _p: (_claims.pop(0) if _claims else []),
            "generation_requests set status = 'running'": [],
        })
        with mock.patch.object(poller.engine_ops, "run_and_record") as rar:
            res = poller.tick(c)
        self.assertIsNone(res)
        rar.assert_not_called()          # generation drain skipped this tick
        self.assertTrue(poller._STOP)    # poller asked to exit for systemd to relaunch


if __name__ == "__main__":
    unittest.main()
