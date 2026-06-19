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
            "set status = 'cancelled'": [],
            "set status = 'running'": lambda _p: (_claims.pop(0) if _claims else []),
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
             mock.patch.object(E, "_load_dotenv_into_environ", lambda: None):
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
             mock.patch.object(E, "_load_dotenv_into_environ", lambda: None):
            with self.assertRaises(E.ConfigError):
                E.connect()


if __name__ == "__main__":
    unittest.main()
