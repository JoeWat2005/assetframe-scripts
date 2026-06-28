"""Tests for control_server.py — the auth gate, the command allow-list + async job dispatch, and the
status snapshot. No real socket / Neon / Cloudflare: engine_ops.connect + run_command are faked.
Run:  python -m pytest tests/test_control_server.py
"""
import contextlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import control_server as CS


class FakeCur:
    def __init__(self, one=None, all=None):
        self._one, self._all = one, all

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class FakeConn:
    def __init__(self, state=None, runs=None):
        self.state, self.runs = state, runs or []

    def execute(self, sql, params=None):
        if "engine_state" in sql:
            return FakeCur(one=self.state)
        if "engine_runs" in sql:
            return FakeCur(all=self.runs)
        return FakeCur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cfg(**kw):
    c = CS.ControlConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@contextlib.contextmanager
def _fake_connect():
    yield FakeConn()


class TestAllowList(unittest.TestCase):
    def test_restart_commands_excluded(self):
        # restart_poller/pull_latest need the POLLER process — never run from the control server
        self.assertNotIn("restart_poller", CS.ALLOWED)
        self.assertNotIn("pull_latest", CS.ALLOWED)

    def test_safe_commands_present(self):
        for c in ("service_check", "run_scoring", "set_config", "compute_due", "run_backtest"):
            self.assertIn(c, CS.ALLOWED)


class TestSubmit(unittest.TestCase):
    def test_unknown_rejected(self):
        self.assertEqual(CS.submit_command("nope", {}, spawn=False)[0], 400)

    def test_restart_rejected_here(self):
        code, body = CS.submit_command("restart_poller", {}, spawn=False)
        self.assertEqual(code, 400)
        self.assertIn("poller path", body["error"])

    def test_args_must_be_object(self):
        self.assertEqual(CS.submit_command("service_check", [], spawn=False)[0], 400)

    def test_valid_creates_running_job(self):
        code, body = CS.submit_command("service_check", {}, spawn=False)
        self.assertEqual(code, 202)
        sc, j = CS.job_status(body["job_id"])
        self.assertEqual((sc, j["status"]), (200, "running"))

    def test_job_status_missing(self):
        self.assertEqual(CS.job_status("job-nope")[0], 404)


class TestRunJob(unittest.TestCase):
    def setUp(self):
        self._orig = CS.engine_ops.connect
        CS.engine_ops.connect = _fake_connect

    def tearDown(self):
        CS.engine_ops.connect = self._orig

    def test_records_outcome(self):
        jid = CS.submit_command("service_check", {}, spawn=False)[1]["job_id"]
        CS._run_job(jid, "service_check", {},
                    runner=lambda conn, row: {"status": "done", "result": "all reachable"})
        _sc, j = CS.job_status(jid)
        self.assertEqual(j["status"], "done")
        self.assertEqual(j["result"], "all reachable")
        self.assertIsNotNone(j["finished_at"])

    def test_job_error_is_failed(self):
        jid = CS.submit_command("service_check", {}, spawn=False)[1]["job_id"]

        def boom(conn, row):
            raise RuntimeError("kaboom")

        CS._run_job(jid, "service_check", {}, runner=boom)
        _sc, j = CS.job_status(jid)
        self.assertEqual(j["status"], "failed")
        self.assertIn("kaboom", j["result"])

    def test_log_is_threaded_through(self):
        # the command layer's `log` (e.g. tail_logs output) must reach the job, not be dropped
        jid = CS.submit_command("tail_logs", {}, spawn=False)[1]["job_id"]
        CS._run_job(jid, "tail_logs", {},
                    runner=lambda conn, row: {"status": "done", "result": "captured", "log": "line1\nline2"})
        _sc, j = CS.job_status(jid)
        self.assertEqual(j["log"], "line1\nline2")


class TestAuthorize(unittest.TestCase):
    def test_insecure_allows(self):
        self.assertTrue(CS.authorize({}, _cfg(insecure=True), None, require_bearer=False)[0])

    def test_missing_jwt_blocked(self):
        ok, reason = CS.authorize({}, _cfg(insecure=False), None, require_bearer=False)
        self.assertFalse(ok)
        self.assertIn("Access", reason)

    def test_invalid_jwt_blocked(self):
        class V:
            def verify(self, t):
                raise ValueError("bad sig")

        ok, _ = CS.authorize({"Cf-Access-Jwt-Assertion": "x"}, _cfg(insecure=False), V(),
                             require_bearer=False)
        self.assertFalse(ok)

    def test_valid_jwt_ok(self):
        class V:
            def verify(self, t):
                return {"aud": ["x"]}

        ok, _ = CS.authorize({"Cf-Access-Jwt-Assertion": "x"}, _cfg(insecure=False), V(),
                             require_bearer=False)
        self.assertTrue(ok)

    def test_bearer_enforced_on_post(self):
        cfg = _cfg(insecure=True, bearer="s3cret")   # insecure skips JWT; bearer still enforced
        self.assertFalse(CS.authorize({}, cfg, None, require_bearer=True)[0])
        self.assertFalse(CS.authorize({"Authorization": "Bearer wrong"}, cfg, None, require_bearer=True)[0])
        self.assertTrue(CS.authorize({"Authorization": "Bearer s3cret"}, cfg, None, require_bearer=True)[0])

    def test_bearer_not_required_on_get(self):
        cfg = _cfg(insecure=True, bearer="s3cret")
        self.assertTrue(CS.authorize({}, cfg, None, require_bearer=False)[0])


class TestSnapshot(unittest.TestCase):
    def test_online_when_fresh_heartbeat(self):
        st = {"last_heartbeat_at": datetime.now(timezone.utc), "automation_paused": False,
              "current_run_id": "run-1"}
        runs = [{"id": "run-1", "trigger": "manual", "status": "running",
                 "started_at": datetime.now(timezone.utc), "finished_at": None, "errors": None}]
        snap = CS.snapshot(FakeConn(state=st, runs=runs))
        self.assertTrue(snap["online"])
        self.assertEqual(snap["current_run_id"], "run-1")
        self.assertEqual(len(snap["runs"]), 1)
        self.assertIsInstance(snap["runs"][0]["started_at"], str)   # datetimes serialised for JSON

    def test_offline_when_stale(self):
        st = {"last_heartbeat_at": datetime.now(timezone.utc) - timedelta(minutes=10),
              "automation_paused": True, "current_run_id": None}
        snap = CS.snapshot(FakeConn(state=st, runs=[]))
        self.assertFalse(snap["online"])
        self.assertTrue(snap["paused"])


class TestBearerFailClosed(unittest.TestCase):
    """Review fix: in secure mode the bearer is the write factor — an UNSET token must fail closed
    (not silently drop POST /control to the same gate as a read)."""

    class _V:
        def verify(self, t):
            return {}                    # JWT valid — isolate the bearer check

    def test_secure_post_blocked_when_token_unconfigured(self):
        cfg = _cfg(insecure=False, bearer="")
        ok, reason = CS.authorize({"Cf-Access-Jwt-Assertion": "x"}, cfg, self._V(), require_bearer=True)
        self.assertFalse(ok)
        self.assertIn("ASSETFRAME_CONTROL_TOKEN", reason)

    def test_secure_post_ok_with_token_and_bearer(self):
        cfg = _cfg(insecure=False, bearer="s3cret")
        ok, _ = CS.authorize({"Cf-Access-Jwt-Assertion": "x", "Authorization": "Bearer s3cret"},
                             cfg, self._V(), require_bearer=True)
        self.assertTrue(ok)

    def test_secure_read_only_needs_no_token(self):
        cfg = _cfg(insecure=False, bearer="")          # reads are gated by the JWT alone
        ok, _ = CS.authorize({"Cf-Access-Jwt-Assertion": "x"}, cfg, self._V(), require_bearer=False)
        self.assertTrue(ok)


class TestCachedSnapshot(unittest.TestCase):
    def test_caches_within_ttl(self):
        calls = [0]
        orig = CS.read_snapshot

        def counting(runs=8):
            calls[0] += 1
            return {"n": calls[0]}

        CS.read_snapshot = counting
        CS._SNAP["data"], CS._SNAP["at"] = None, -1e9
        try:
            a, b = CS.cached_snapshot(), CS.cached_snapshot()
            self.assertEqual(a, b)
            self.assertEqual(calls[0], 1)              # 2nd call served from cache (one Neon hit)
        finally:
            CS.read_snapshot = orig
            CS._SNAP["data"], CS._SNAP["at"] = None, -1e9


if __name__ == "__main__":
    unittest.main(verbosity=2)
