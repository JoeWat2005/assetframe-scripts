"""INTEGRATION tests for scripts/scheduler/service/ — the poller/scheduled_run loop wired to the
REAL coordination layer (db + commands), not isolated functions.

Where test_scheduler_service_unit.py monkeypatches every engine_ops.* call (claim/run_command/
heartbeat/reap), THIS file does the opposite: it drives poller.tick / poller.run_once / poller.loop /
scheduled_run.main with a STATEFUL FakeConn that actually models the four Neon tables
(engine_state, generation_requests, engine_commands, engine_runs), and lets the REAL functions run
against it:

    engine_ops.heartbeat / is_paused          (db.EngineDB)
    engine_ops.claim_next_request             (db.EngineDB._claim_next, two-phase cancel-drain)
    engine_ops.claim_next_command             (same, engine_commands variant)
    engine_ops.run_command  -> _COMMAND_HANDLERS -> _finish_command   (commands.py allow-list)
    engine_ops.reap_stale_runs / reap_stale_commands                   (db.EngineDB)

so the cross-module DATA CONTRACT (what claim writes via RETURNING * vs what _drain/run_command read,
the restart self-exit wiring, the age-based reap + current_run_id clear, the cancel-before-run drain)
is exercised end to end.

ONLY true external boundaries are faked: the psycopg connect() (replaced by FakeConn), the Upstash
network helpers (heartbeat_upstash/clear_wake/wake_pending/upstash_enabled + the daemon), the
subprocess-spawning run_and_record (the generation child), and the config-file-writing
_sync_assets_from_neon. Everything in-process is real. No Neon, no subprocess, no network, no disk.

Import style mirrors the existing tests: sys.path.insert(0, tests dir) + flat imports (the conftest's
`import scripts` shim already put scripts/coordination + scripts/scheduler/service on sys.path).
"""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller
import scheduled_run
import engine_ops as E


def _utcnow():
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------- stateful fake DB
class FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows) if rows is not None else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _NullTx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """A small in-memory Postgres stand-in that the REAL db/commands SQL drives.

    Tables are lists of row-dicts (engine_state is the id=1 singleton). execute() pattern-matches the
    handful of statements the coordination layer emits and mutates the rows accordingly, returning a
    FakeCursor so RETURNING id / RETURNING * keep working. `missing` simulates an un-migrated table
    (raises UndefinedTable, like a fresh box whose engine_commands migration hasn't run)."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.state = {"id": 1, "automation_paused": False, "last_heartbeat_at": None,
                      "current_run_id": None, "updated_at": None}
        self.generation_requests = []
        self.engine_commands = []
        self.engine_runs = []
        self.executed = []

    # -- context manager (for `with engine_ops.connect() as conn:`) -------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return _NullTx()

    # -- seed helpers -----------------------------------------------------------------
    def add_request(self, rid, *, created, scope=None, cancel=False, status="queued"):
        self.generation_requests.append(
            {"id": rid, "created_at": created, "scope": (scope if scope is not None else {}),
             "cancel_requested": cancel, "status": status, "started_at": None,
             "finished_at": None, "run_id": None, "error": None})

    def add_command(self, cid, command, *, created, args=None, cancel=False, status="queued"):
        self.engine_commands.append(
            {"id": cid, "command": command, "args": (args or {}), "created_at": created,
             "cancel_requested": cancel, "status": status, "started_at": None,
             "finished_at": None, "result": None, "log_excerpt": None})

    def add_run(self, rid, *, status, started_at, trigger="manual"):
        self.engine_runs.append(
            {"id": rid, "status": status, "started_at": started_at, "trigger": trigger,
             "scope": {}, "results": None, "errors": None, "log_excerpt": None,
             "finished_at": None})

    def get_request(self, rid):
        return next((r for r in self.generation_requests if r["id"] == rid), None)

    def get_command(self, cid):
        return next((c for c in self.engine_commands if c["id"] == cid), None)

    def get_run(self, rid):
        return next((r for r in self.engine_runs if r["id"] == rid), None)

    # -- the dispatcher ---------------------------------------------------------------
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        norm = " ".join(sql.split()).lower()
        params = params or ()
        for tbl in self.missing:
            if tbl in norm:
                raise psycopg.errors.UndefinedTable(f'relation "{tbl}" does not exist')

        if "engine_state" in norm:
            return self._engine_state(norm, params)
        if "engine_runs" in norm:
            return self._engine_runs(norm, params)
        if "generation_requests" in norm:
            return self._queue(self.generation_requests, norm, params)
        if "engine_commands" in norm:
            return self._queue(self.engine_commands, norm, params)
        return FakeCursor([])

    def _engine_state(self, norm, params):
        if "last_heartbeat_at" in norm:                          # heartbeat()
            self.state["last_heartbeat_at"] = _utcnow()
            self.state["updated_at"] = _utcnow()
            return FakeCursor([])
        if "select automation_paused" in norm:                   # is_paused()
            return FakeCursor([{"automation_paused": self.state["automation_paused"]}])
        if "current_run_id = null where current_run_id is not null" in norm:   # reap phase 2
            running = {r["id"] for r in self.engine_runs if r["status"] == "running"}
            if self.state["current_run_id"] is not None and self.state["current_run_id"] not in running:
                self.state["current_run_id"] = None
            return FakeCursor([])
        if "current_run_id =" in norm:                           # set_current_run(run_id)
            self.state["current_run_id"] = params[0] if params else None
            return FakeCursor([])
        return FakeCursor([])

    def _engine_runs(self, norm, params):
        if "insert into engine_runs" in norm:                    # _record_skip upsert
            rid = params[0]
            row = self.get_run(rid)
            payload = {"id": rid, "trigger": "schedule", "scope": params[1], "status": "done",
                       "results": params[2], "log_excerpt": params[3], "started_at": _utcnow(),
                       "finished_at": _utcnow(), "errors": None}
            if row:
                row.update(status="done", finished_at=_utcnow())
            else:
                self.engine_runs.append(payload)
            return FakeCursor([])
        if "set status = 'failed'" in norm and "make_interval" in norm:   # reap_stale_runs phase 1
            max_age = int(params[0]) if params else 0
            cutoff = _utcnow() - timedelta(seconds=max_age)
            for r in self.engine_runs:
                if r["status"] == "running" and r["started_at"] is not None and r["started_at"] < cutoff:
                    r["status"] = "failed"
                    r["errors"] = r.get("errors") or "orphaned (process killed mid-run)"
                    r["finished_at"] = _utcnow()
            return FakeCursor([])
        if "set status = %s" in norm:                            # RunRecorder.finish (id = params[-1])
            row = self.get_run(params[-1])
            if row:
                row["status"] = params[0]
            return FakeCursor([])
        return FakeCursor([])

    def _queue(self, rows, norm, params):
        """Shared two-phase claim/drain + finish for generation_requests and engine_commands."""
        if "set status = 'cancelled'" in norm:                   # phase 1: drain queued+cancelled
            drained = []
            for r in rows:
                if r["status"] == "queued" and r["cancel_requested"]:
                    r["status"] = "cancelled"
                    r["finished_at"] = _utcnow()
                    drained.append({"id": r["id"]})
            return FakeCursor(drained)                            # RETURNING id (generation only)
        if "set status = 'running'" in norm:                     # phase 2: claim oldest runnable
            runnable = [r for r in rows if r["status"] == "queued" and not r["cancel_requested"]]
            if not runnable:
                return FakeCursor([])
            row = sorted(runnable, key=lambda r: r["created_at"])[0]
            row["status"] = "running"
            row["started_at"] = _utcnow()
            return FakeCursor([dict(row)])                        # RETURNING *
        if "set status = 'failed'" in norm:                      # reap_stale_commands
            for r in rows:
                if r["status"] == "running":
                    r["status"] = "failed"
                    r["result"] = r.get("result") or "interrupted (poller restarted)"
                    r["finished_at"] = _utcnow()
            return FakeCursor([])
        if "set status = %s" in norm:                            # _finish_command / _finish_request
            row = next((r for r in rows if r["id"] == params[-1]), None)
            if row:
                row["status"] = params[0]
                row["result"] = params[1] if "result =" in norm else row.get("result")
                row["finished_at"] = _utcnow()
            return FakeCursor([])
        return FakeCursor([])


# --------------------------------------------------------------------- helpers
class _StopGuard:
    """Save/restore poller._STOP around a test (tick may set the module global on restart)."""
    def __enter__(self):
        self._saved = poller._STOP
        poller._STOP = False
        return self

    def __exit__(self, *exc):
        poller._STOP = self._saved
        return False


class _RunRecorder:
    """A fake for engine_ops.run_and_record: records every call, returns a deterministic run id.
    (The real one spawns the run_daily subprocess — the only thing we MUST stub here.)"""
    def __init__(self):
        self.calls = []

    def __call__(self, conn, trigger, scope, request_id=None, sandbox=False):
        self.calls.append({"trigger": trigger, "scope": scope, "request_id": request_id})
        return f"run-{request_id or trigger}"


def _fake_wake(stack):
    """Enter all Upstash-network fakes on an ExitStack (the only network boundary)."""
    for name in ("heartbeat_upstash", "clear_wake", "start_heartbeat_daemon", "stop_heartbeat_daemon"):
        stack.enter_context(mock.patch.object(poller.engine_ops, name))
    stack.enter_context(mock.patch.object(poller.engine_ops, "wake_pending", return_value=False))
    stack.enter_context(mock.patch.object(poller.engine_ops, "upstash_enabled", return_value=True))


# ============================================================ poller.run_once (real claim + drain)
class RunOnceRequestDrain(unittest.TestCase):
    def test_real_two_phase_claim_drains_queue_skipping_cancelled(self):
        import contextlib
        conn = FakeConn()
        # Oldest first: A runnable, B cancelled-before-start, C runnable.
        conn.add_request("A", created=1, scope={"assets": ["btc"]})
        conn.add_request("B", created=2, scope={"assets": ["eth"]}, cancel=True)
        conn.add_request("C", created=3, scope={"all_due": True})
        rar = _RunRecorder()
        with _StopGuard(), contextlib.ExitStack() as stack:
            _fake_wake(stack)
            stack.enter_context(mock.patch.object(poller.engine_ops, "connect", return_value=conn))
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            rv = poller.run_once()

        # heartbeat (real db.heartbeat) stamped the singleton.
        self.assertIsNotNone(conn.state["last_heartbeat_at"])
        # B was drained to 'cancelled' WITHOUT running; A and C were claimed (now 'running').
        self.assertEqual(conn.get_request("B")["status"], "cancelled")
        self.assertEqual(conn.get_request("A")["status"], "running")
        self.assertEqual(conn.get_request("C")["status"], "running")
        # run_and_record was invoked for exactly A then C, in created order, manual trigger.
        self.assertEqual([c["request_id"] for c in rar.calls], ["A", "C"])
        self.assertTrue(all(c["trigger"] == "manual" for c in rar.calls))
        self.assertEqual(rar.calls[0]["scope"], {"assets": ["btc"]})    # scope survived RETURNING *
        self.assertEqual(rar.calls[1]["scope"], {"all_due": True})
        self.assertEqual(rv, "run-C")                                   # tick returns the LAST run id

    def test_empty_queue_runs_nothing_but_still_heartbeats(self):
        import contextlib
        conn = FakeConn()
        rar = _RunRecorder()
        with _StopGuard(), contextlib.ExitStack() as stack:
            _fake_wake(stack)
            stack.enter_context(mock.patch.object(poller.engine_ops, "connect", return_value=conn))
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            rv = poller.run_once()
        self.assertIsNone(rv)
        self.assertEqual(rar.calls, [])
        self.assertIsNotNone(conn.state["last_heartbeat_at"])


# ============================================================ poller.tick (real command channel)
class TickCommandChannel(unittest.TestCase):
    def test_commands_drained_through_real_allowlist_then_requests(self):
        """Non-restart commands run via the REAL run_command -> _COMMAND_HANDLERS -> _finish_command,
        recording terminal status on engine_commands, THEN the generation queue drains."""
        import contextlib
        conn = FakeConn()
        # c1 hits the real set_config validator (rejects a non-int RUN_TIMEOUT, no file write);
        # c2 is an unknown verb the allow-list rejects. Both must end 'failed', neither restarts.
        conn.add_command("c1", "set_config", created=1,
                         args={"key": "ASSETFRAME_RUN_TIMEOUT", "value": "not-an-int"})
        conn.add_command("c2", "definitely_not_a_command", created=2)
        conn.add_request("R", created=1, scope={"assets": ["btc"]})
        rar = _RunRecorder()
        with _StopGuard(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            rv = poller.tick(conn)

        self.assertEqual(conn.get_command("c1")["status"], "failed")    # validator rejected the value
        self.assertIn("not valid", (conn.get_command("c1")["result"] or ""))
        self.assertEqual(conn.get_command("c2")["status"], "failed")    # unknown verb
        self.assertIn("unknown command", (conn.get_command("c2")["result"] or ""))
        # generation queue still drained in the SAME tick (no restart short-circuit).
        self.assertEqual([c["request_id"] for c in rar.calls], ["R"])
        self.assertEqual(rv, "run-R")

    def test_restart_command_self_exits_and_skips_generation_drain(self):
        """The restart contract: a restart_poller command -> run_command returns restart=True ->
        _drain_commands returns True -> tick sets _STOP and returns None WITHOUT draining requests
        (the queued run is left 'queued' for after the systemd relaunch)."""
        import contextlib
        conn = FakeConn()
        conn.add_command("c1", "definitely_not_a_command", created=1)   # an older non-restart cmd
        conn.add_command("c2", "restart_poller", created=2)             # then the restart
        conn.add_command("c3", "definitely_not_a_command", created=3)   # queued AFTER restart
        conn.add_request("R", created=1)
        rar = _RunRecorder()
        with _StopGuard(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            rv = poller.tick(conn)
            stop_flag = poller._STOP                                   # capture BEFORE the guard restores it

        self.assertIsNone(rv)                                           # tick bailed on restart
        self.assertTrue(stop_flag)                                     # self-exit flag set
        self.assertEqual(conn.get_command("c1")["status"], "failed")   # earlier cmd ran first
        self.assertEqual(conn.get_command("c2")["status"], "done")     # restart recorded done (pre-exit)
        self.assertEqual(conn.get_command("c3")["status"], "queued")   # NOT drained — picked up later
        self.assertEqual(conn.get_request("R")["status"], "queued")    # generation drain was skipped
        self.assertEqual(rar.calls, [])

    def test_engine_commands_not_migrated_is_graceful_requests_still_run(self):
        """A box whose engine_commands migration hasn't run: claim_next_command swallows the
        UndefinedTable to None (no crash), the command drain is a no-op, and the generation queue
        still drains in the same tick."""
        import contextlib
        conn = FakeConn(missing={"engine_commands"})
        conn.add_request("R", created=1, scope={"all_due": True})
        rar = _RunRecorder()
        with _StopGuard(), contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            rv = poller.tick(conn)
        self.assertEqual([c["request_id"] for c in rar.calls], ["R"])
        self.assertEqual(rv, "run-R")
        self.assertIsNotNone(conn.state["last_heartbeat_at"])


# ============================================================ poller.loop (startup reap + age reap + tick)
class LoopFullPass(unittest.TestCase):
    def test_one_pass_reaps_orphan_run_clears_banner_and_runs_tick(self):
        """A full orchestrated Neon pass: real reap_stale_commands (startup) + real reap_stale_runs
        (age-based) + real tick, with only the network/connect/config-sync/run_and_record boundaries
        faked. An orphaned 'running' run older than RUN_TIMEOUT+grace is failed and its stale
        current_run_id banner cleared; a freshly-started run is left alone."""
        import contextlib
        conn = FakeConn()
        conn.state["current_run_id"] = "orphan"                        # banner points at the dead run
        conn.add_run("orphan", status="running", started_at=_utcnow() - timedelta(days=30))
        conn.add_run("live", status="running", started_at=_utcnow())   # legitimately in-flight
        conn.add_request("R", created=1, scope={"assets": ["btc"]})
        rar = _RunRecorder()

        real_tick = poller.tick

        def _tick_then_stop(c):
            rv = real_tick(c)
            poller._STOP = True                                        # exit loop after one real pass
            return rv

        with _StopGuard(), contextlib.ExitStack() as stack:
            _fake_wake(stack)
            stack.enter_context(mock.patch.object(poller.engine_ops, "connect", return_value=conn))
            stack.enter_context(mock.patch.object(poller.engine_ops, "run_and_record", rar))
            # _sync_assets_from_neon writes the real config/assets.json — stub it (config boundary).
            sync = stack.enter_context(mock.patch.object(
                poller.engine_ops, "_sync_assets_from_neon", return_value=(True, "synced 0")))
            stack.enter_context(mock.patch.object(poller, "tick", side_effect=_tick_then_stop))
            rc = poller.loop(0)

        self.assertEqual(rc, 0)
        sync.assert_called_once_with(conn)                             # startup config sync ran once
        # reap_stale_runs (real): the 30-day orphan is failed; the fresh run is untouched.
        self.assertEqual(conn.get_run("orphan")["status"], "failed")
        self.assertEqual(conn.get_run("live")["status"], "running")
        # current_run_id banner cleared because it no longer points at a running run.
        self.assertIsNone(conn.state["current_run_id"])
        # tick (real) still heartbeated and drained the request.
        self.assertIsNotNone(conn.state["last_heartbeat_at"])
        self.assertEqual([c["request_id"] for c in rar.calls], ["R"])


# ============================================================ scheduled_run.main (real is_paused contract)
class ScheduledRunPauseContract(unittest.TestCase):
    def test_paused_records_skip_row_and_does_not_run(self):
        """The daily oneshot RESPECTS the pause flag: real heartbeat + real is_paused -> the
        scheduled_run._record_skip note is written to engine_runs and run_and_record is NOT called."""
        conn = FakeConn()
        conn.state["automation_paused"] = True
        rar = _RunRecorder()
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=conn), \
             mock.patch.object(scheduled_run.engine_ops, "run_and_record", rar):
            rc = scheduled_run.main()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(conn.state["last_heartbeat_at"])          # heartbeat ran first
        self.assertEqual(rar.calls, [])                                # paused -> no generation
        skip = [r for r in conn.engine_runs if str(r["id"]).endswith("-skipped")]
        self.assertEqual(len(skip), 1)
        self.assertEqual(skip[0]["status"], "done")
        self.assertEqual(skip[0]["scope"], '{"all_due": true}')        # scope literal contract
        self.assertIn("automation_paused", skip[0]["results"])

    def test_active_runs_schedule_all_due_and_writes_no_skip(self):
        """Not paused: real is_paused returns False -> run_and_record('schedule', {all_due})."""
        conn = FakeConn()
        conn.state["automation_paused"] = False
        rar = _RunRecorder()
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=conn), \
             mock.patch.object(scheduled_run.engine_ops, "run_and_record", rar):
            rc = scheduled_run.main()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(conn.state["last_heartbeat_at"])
        self.assertEqual(len(rar.calls), 1)
        self.assertEqual(rar.calls[0]["trigger"], "schedule")
        self.assertEqual(rar.calls[0]["scope"], {"all_due": True})
        self.assertEqual([r for r in conn.engine_runs if str(r["id"]).endswith("-skipped")], [])


if __name__ == "__main__":
    unittest.main()
