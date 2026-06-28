"""OFFLINE unit tests for scripts/scheduler/service/ — poller, scheduled_run, _service.

These exercise the orchestration logic the existing test_engine_ops.py does NOT cover:

  * _service.service_log    : the timestamped, flushed stdout logger format.
  * poller.run_once         : the single --once pass — Upstash heartbeat, Neon open,
                              clear_wake, tick; ConfigError re-raises, other errors swallow.
  * poller._drain           : claim-until-empty over generation_requests (manual trigger).
  * poller._drain_commands  : claim-until-empty over engine_commands, restart gating.
  * poller.loop             : one full orchestrated pass (startup reap + sync + reap_runs + tick)
                              and the ConfigError -> return 1 fail-loud branch.
  * poller.main             : --once / loop dispatch + exit codes + arg parsing.
  * poller._handle_sigterm  : sets the module _STOP flag.
  * scheduled_run._record_skip / main : the paused-skip note + the error exit codes.

Everything is faked: NO Neon, Upstash, subprocess or network. engine_ops.* is monkeypatched
on the shared module object (poller.engine_ops is scheduled_run.engine_ops is engine_ops).
"""
import io
import contextlib
import re
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poller
import scheduled_run
import _service
import engine_ops as E


# --------------------------------------------------------------------- fakes
class FakeConn:
    """Minimal context-manager connection that records executed SQL + params."""
    def __init__(self):
        self.executed = []          # list of (sql, params)
        self.raise_on_execute = None

    def execute(self, sql, params=None):
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        self.executed.append((sql, params))
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sql_log(self):
        return " || ".join(s.lower() for s, _ in self.executed)


class _StopGuard:
    """Save/restore poller._STOP around a test that touches the module global."""
    def __enter__(self):
        self._saved = poller._STOP
        poller._STOP = False
        return self

    def __exit__(self, *exc):
        poller._STOP = self._saved
        return False


# ============================================================ _service.service_log
class ServiceLog(unittest.TestCase):
    def test_returns_callable(self):
        self.assertTrue(callable(_service.service_log("poller")))

    def test_format_has_prefix_utc_timestamp_and_message(self):
        log = _service.service_log("poller")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            log("hello world")
        out = buf.getvalue()
        # exactly: [poller YYYY-MM-DD HH:MM:SSZ] hello world\n
        self.assertRegex(
            out, r"^\[poller \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z\] hello world\n$")

    def test_prefix_is_honoured(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _service.service_log("scheduled")("x")
        self.assertTrue(buf.getvalue().startswith("[scheduled "))

    def test_distinct_prefixes_are_independent(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _service.service_log("a")("m1")
            _service.service_log("b")("m2")
        lines = buf.getvalue().strip().splitlines()
        self.assertTrue(lines[0].startswith("[a "))
        self.assertTrue(lines[1].startswith("[b "))

    def test_non_string_message_is_stringified(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _service.service_log("p")(12345)
        self.assertIn("12345", buf.getvalue())

    def test_uses_flush(self):
        # The logger must flush so journald sees lines immediately. Patch builtins.print
        # and assert flush=True was passed through.
        log = _service.service_log("p")
        with mock.patch("builtins.print") as p:
            log("msg")
        p.assert_called_once()
        self.assertTrue(p.call_args.kwargs.get("flush"))


# ============================================================ poller.run_once
class PollerRunOnce(unittest.TestCase):
    def test_happy_path_heartbeats_clears_wake_and_returns_tick(self):
        conn = FakeConn()
        with mock.patch.object(poller.engine_ops, "heartbeat_upstash") as hb, \
             mock.patch.object(poller.engine_ops, "connect", return_value=conn), \
             mock.patch.object(poller.engine_ops, "clear_wake") as cw, \
             mock.patch.object(poller, "tick", return_value="run-77") as tk:
            rv = poller.run_once()
        self.assertEqual(rv, "run-77")
        hb.assert_called_once()          # Upstash heartbeat happens FIRST (no Neon)
        cw.assert_called_once()          # wake flag cleared once on Neon
        tk.assert_called_once_with(conn)

    def test_configerror_is_reraised(self):
        with mock.patch.object(poller.engine_ops, "heartbeat_upstash"), \
             mock.patch.object(poller.engine_ops, "connect",
                               side_effect=E.ConfigError("no DATABASE_URL")):
            with self.assertRaises(E.ConfigError):
                poller.run_once()

    def test_transient_error_is_swallowed_returns_none(self):
        # A non-config error in the tick must NOT propagate (so --once is safe in any state).
        with mock.patch.object(poller.engine_ops, "heartbeat_upstash"), \
             mock.patch.object(poller.engine_ops, "connect",
                               side_effect=RuntimeError("neon blip")):
            self.assertIsNone(poller.run_once())

    def test_tick_error_is_swallowed(self):
        conn = FakeConn()
        with mock.patch.object(poller.engine_ops, "heartbeat_upstash"), \
             mock.patch.object(poller.engine_ops, "connect", return_value=conn), \
             mock.patch.object(poller.engine_ops, "clear_wake"), \
             mock.patch.object(poller, "tick", side_effect=ValueError("boom")):
            self.assertIsNone(poller.run_once())


# ============================================================ poller._drain
class PollerDrain(unittest.TestCase):
    def test_returns_none_when_queue_empty(self):
        conn = FakeConn()
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_request", return_value=None), \
             mock.patch.object(poller.engine_ops, "run_and_record") as rar:
            self.assertIsNone(poller._drain(conn))
        rar.assert_not_called()

    def test_drains_until_empty_and_returns_last_run_id(self):
        conn = FakeConn()
        rows = [{"id": "r1", "scope": {"assets": ["btc"]}},
                {"id": "r2", "scope": {"all_due": True}}]
        claims = list(rows)
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_request",
                               side_effect=lambda c: claims.pop(0) if claims else None), \
             mock.patch.object(poller.engine_ops, "run_and_record",
                               side_effect=lambda c, **kw: "req-" + kw["request_id"]) as rar:
            last = poller._drain(conn)
        self.assertEqual(last, "req-r2")        # last run id returned
        self.assertEqual(rar.call_count, 2)
        # each call is a manual run carrying the row's id + scope.
        first_kwargs = rar.call_args_list[0].kwargs
        self.assertEqual(first_kwargs["trigger"], "manual")
        self.assertEqual(first_kwargs["request_id"], "r1")
        self.assertEqual(first_kwargs["scope"], {"assets": ["btc"]})

    def test_missing_scope_defaults_to_empty_dict(self):
        conn = FakeConn()
        claims = [{"id": "rX"}]          # no 'scope' key
        seen = {}
        def _rar(c, **kw):
            seen.update(kw)
            return "req-rX"
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_request",
                               side_effect=lambda c: claims.pop(0) if claims else None), \
             mock.patch.object(poller.engine_ops, "run_and_record", side_effect=_rar):
            poller._drain(conn)
        self.assertEqual(seen["scope"], {})

    def test_respects_stop_flag(self):
        # If _STOP is already set, _drain never claims (graceful shutdown mid-tick).
        conn = FakeConn()
        with _StopGuard():
            poller._STOP = True
            with mock.patch.object(poller.engine_ops, "claim_next_request") as cl, \
                 mock.patch.object(poller.engine_ops, "run_and_record") as rar:
                self.assertIsNone(poller._drain(conn))
            cl.assert_not_called()
            rar.assert_not_called()


# ============================================================ poller._drain_commands
class PollerDrainCommands(unittest.TestCase):
    def test_returns_false_when_no_commands(self):
        conn = FakeConn()
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_command", return_value=None), \
             mock.patch.object(poller.engine_ops, "run_command") as rc:
            self.assertFalse(poller._drain_commands(conn))
        rc.assert_not_called()

    def test_drains_non_restart_commands_and_returns_false(self):
        conn = FakeConn()
        cmds = [{"id": "c1", "command": "sync_assets", "args": {}},
                {"id": "c2", "command": "run_scoring", "args": {}}]
        claims = list(cmds)
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_command",
                               side_effect=lambda c: claims.pop(0) if claims else None), \
             mock.patch.object(poller.engine_ops, "run_command",
                               return_value={"status": "done", "result": "ok", "restart": False}) as rc:
            self.assertFalse(poller._drain_commands(conn))
        self.assertEqual(rc.call_count, 2)

    def test_restart_command_returns_true_and_stops_draining(self):
        conn = FakeConn()
        claims = [{"id": "cR", "command": "restart_poller", "args": {}},
                  {"id": "cAfter", "command": "sync_assets", "args": {}}]
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "claim_next_command",
                               side_effect=lambda c: claims.pop(0) if claims else None), \
             mock.patch.object(poller.engine_ops, "run_command",
                               return_value={"status": "done", "result": "restarting",
                                             "restart": True}) as rc:
            self.assertTrue(poller._drain_commands(conn))
        # it returned on the FIRST (restart) command; the second was never claimed/run.
        rc.assert_called_once()


# ============================================================ poller.loop
class PollerLoop(unittest.TestCase):
    def test_one_pass_runs_startup_reap_sync_reap_runs_and_tick(self):
        conn = FakeConn()

        def _stopping_tick(c):
            poller._STOP = True           # exit the loop after exactly one pass
            return None

        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "upstash_enabled", return_value=True), \
             mock.patch.object(poller.engine_ops, "start_heartbeat_daemon") as sd, \
             mock.patch.object(poller.engine_ops, "stop_heartbeat_daemon") as stop_d, \
             mock.patch.object(poller.engine_ops, "heartbeat_upstash") as hb, \
             mock.patch.object(poller.engine_ops, "wake_pending", return_value=False), \
             mock.patch.object(poller.engine_ops, "connect", return_value=conn), \
             mock.patch.object(poller.engine_ops, "clear_wake") as cw, \
             mock.patch.object(poller.engine_ops, "reap_stale_commands") as rsc, \
             mock.patch.object(poller.engine_ops, "_sync_assets_from_neon",
                               return_value=(True, "synced 5")) as sync, \
             mock.patch.object(poller.engine_ops, "reap_stale_runs") as rsr, \
             mock.patch.object(poller, "tick", side_effect=_stopping_tick) as tk:
            rc = poller.loop(0)
        self.assertEqual(rc, 0)
        sd.assert_called_once_with(interval=10)   # background heartbeat daemon started
        stop_d.assert_called_once()               # and stopped on clean exit
        hb.assert_called()                        # cheap Upstash heartbeat each tick
        cw.assert_called()                        # wake cleared when going to Neon
        rsc.assert_called_once()                  # FIRST pass: phantom-command reap
        sync.assert_called_once_with(conn)        # FIRST pass: rebuild assets from Neon
        rsr.assert_called()                       # EVERY Neon pass: age-based run reap
        tk.assert_called_once_with(conn)

    def test_configerror_returns_1_fail_loud(self):
        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "upstash_enabled", return_value=True), \
             mock.patch.object(poller.engine_ops, "start_heartbeat_daemon"), \
             mock.patch.object(poller.engine_ops, "stop_heartbeat_daemon"), \
             mock.patch.object(poller.engine_ops, "heartbeat_upstash"), \
             mock.patch.object(poller.engine_ops, "wake_pending", return_value=False), \
             mock.patch.object(poller.engine_ops, "connect",
                               side_effect=E.ConfigError("no DATABASE_URL")):
            rc = poller.loop(0)
        self.assertEqual(rc, 1)

    def test_startup_sync_failure_is_non_fatal(self):
        # A throwing _sync_assets_from_neon on the first pass must NOT kill the loop;
        # reaped flips True anyway and the tick still runs.
        conn = FakeConn()

        def _stopping_tick(c):
            poller._STOP = True
            return None

        with _StopGuard(), \
             mock.patch.object(poller.engine_ops, "upstash_enabled", return_value=True), \
             mock.patch.object(poller.engine_ops, "start_heartbeat_daemon"), \
             mock.patch.object(poller.engine_ops, "stop_heartbeat_daemon"), \
             mock.patch.object(poller.engine_ops, "heartbeat_upstash"), \
             mock.patch.object(poller.engine_ops, "wake_pending", return_value=False), \
             mock.patch.object(poller.engine_ops, "connect", return_value=conn), \
             mock.patch.object(poller.engine_ops, "clear_wake"), \
             mock.patch.object(poller.engine_ops, "reap_stale_commands"), \
             mock.patch.object(poller.engine_ops, "_sync_assets_from_neon",
                               side_effect=RuntimeError("neon read failed")), \
             mock.patch.object(poller.engine_ops, "reap_stale_runs") as rsr, \
             mock.patch.object(poller, "tick", side_effect=_stopping_tick) as tk:
            rc = poller.loop(0)
        self.assertEqual(rc, 0)
        rsr.assert_called()       # reached past the sync (reaped flipped True)
        tk.assert_called_once()


# ============================================================ poller.main
class PollerMain(unittest.TestCase):
    def test_once_dispatches_run_once_and_returns_0(self):
        with mock.patch.object(poller.signal, "signal"), \
             mock.patch.object(poller, "run_once", return_value="ignored") as ro:
            rc = poller.main(["--once"])
        self.assertEqual(rc, 0)
        ro.assert_called_once()

    def test_once_configerror_returns_1(self):
        with mock.patch.object(poller.signal, "signal"), \
             mock.patch.object(poller, "run_once", side_effect=E.ConfigError("x")):
            self.assertEqual(poller.main(["--once"]), 1)

    def test_default_dispatches_loop_with_default_interval(self):
        with mock.patch.object(poller.signal, "signal"), \
             mock.patch.object(poller, "loop", return_value=0) as lp:
            rc = poller.main([])
        self.assertEqual(rc, 0)
        lp.assert_called_once_with(30)

    def test_interval_flag_is_passed_to_loop(self):
        with mock.patch.object(poller.signal, "signal"), \
             mock.patch.object(poller, "loop", return_value=0) as lp:
            poller.main(["--interval", "45"])
        lp.assert_called_once_with(45)

    def test_loop_return_code_is_propagated(self):
        with mock.patch.object(poller.signal, "signal"), \
             mock.patch.object(poller, "loop", return_value=1) as lp:
            self.assertEqual(poller.main([]), 1)

    def test_registers_sigterm_handler(self):
        with mock.patch.object(poller.signal, "signal") as sig, \
             mock.patch.object(poller, "loop", return_value=0):
            poller.main([])
        registered = {call.args[0] for call in sig.call_args_list}
        self.assertIn(poller.signal.SIGTERM, registered)


# ============================================================ poller._handle_sigterm
class PollerSigterm(unittest.TestCase):
    def test_sets_stop_flag(self):
        with _StopGuard():
            self.assertFalse(poller._STOP)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                poller._handle_sigterm(15, None)
            self.assertTrue(poller._STOP)


# ============================================================ scheduled_run._record_skip
class ScheduledRecordSkip(unittest.TestCase):
    def test_writes_skip_note_with_dated_id_and_paused_reason(self):
        conn = FakeConn()
        scheduled_run._record_skip(conn)
        self.assertEqual(len(conn.executed), 1)
        sql, params = conn.executed[0]
        s = sql.lower()
        self.assertIn("insert into engine_runs", s)
        self.assertIn("on conflict (id) do update", s)
        # params: (run_id, scope_json, results_json, log_excerpt)
        self.assertTrue(params[0].startswith("daily-"))
        self.assertTrue(params[0].endswith("-skipped"))
        self.assertEqual(params[1], '{"all_due": true}')
        self.assertIn("automation_paused", params[2])
        self.assertIn("skipped", params[3].lower())

    def test_swallows_db_error(self):
        conn = FakeConn()
        conn.raise_on_execute = RuntimeError("table missing")
        # Best-effort: must not raise even if the INSERT fails.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            scheduled_run._record_skip(conn)   # no exception
        self.assertIn("could not record skip", buf.getvalue())


# ============================================================ scheduled_run.main (error paths)
class ScheduledMainErrors(unittest.TestCase):
    def test_configerror_returns_1(self):
        with mock.patch.object(scheduled_run.engine_ops, "connect",
                               side_effect=E.ConfigError("no DATABASE_URL")):
            self.assertEqual(scheduled_run.main(), 1)

    def test_unexpected_error_before_recording_returns_1(self):
        # connect()/heartbeat() throwing a non-config error -> guarded -> exit 1.
        conn = FakeConn()
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=conn), \
             mock.patch.object(scheduled_run.engine_ops, "heartbeat",
                               side_effect=RuntimeError("neon down")):
            self.assertEqual(scheduled_run.main(), 1)

    def test_heartbeat_runs_before_pause_check(self):
        # Happy active path: heartbeat is stamped, then the pause flag is read, then the run
        # is recorded with trigger='schedule' / scope all_due.
        conn = FakeConn()
        with mock.patch.object(scheduled_run.engine_ops, "connect", return_value=conn), \
             mock.patch.object(scheduled_run.engine_ops, "heartbeat") as hb, \
             mock.patch.object(scheduled_run.engine_ops, "is_paused", return_value=False), \
             mock.patch.object(scheduled_run.engine_ops, "run_and_record",
                               return_value="daily-2026-06-28") as rar:
            rc = scheduled_run.main()
        self.assertEqual(rc, 0)
        hb.assert_called_once_with(conn)
        rar.assert_called_once()
        self.assertEqual(rar.call_args.kwargs.get("trigger"), "schedule")
        self.assertEqual(rar.call_args.kwargs.get("scope"), {"all_due": True})


if __name__ == "__main__":
    unittest.main()
