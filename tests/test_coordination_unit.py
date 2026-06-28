"""Offline unit tests for scripts/coordination/* — the OCI engine coordination layer.

These target the GAPS left by the existing suite (test_engine_ops / test_sandbox / test_control_server /
test_audit_fixes / test_data_license), which cover scope_to_run_args, claim/heartbeat SQL, RunRecorder,
summarize_manifest, run_backtest_batch, the set_config allow-list and clear_r2's malformed-date guard.
Here we exercise the modules that were only covered transitively after the refactor:

  * locking      — _FileLock acquire/release/re-acquire + LOCK_PATH anchoring (contention guard is
                   POSIX-only; asserted under skipUnless(fcntl)).
  * wake         — Upstash creds/_upstash REST/heartbeat/wake-flag commands + the heartbeat daemon.
  * manifest     — _tail byte-truncation, _new_run_id manual branch, _read_run_manifest selection.
  * db           — _load_dotenv_into_environ, _empty_dir, reap default age, EngineDB own/borrow close,
                   OpsContext, claim UndefinedTable propagation.
  * runner       — _publish_chain step ordering + fatal/non-fatal/cancel branches, _run_sync_backtest,
                   _exec_run_daily (launch/success/cancel/timeout/non-zero), _terminate, _wipe_sandbox_state.
  * commands     — _cmd_clear_wake/_clear_sandbox/_tail_logs/_service_check/_compute_due/_run_scoring/
                   _pull_latest/_r2_client + run_command arg parsing + _finish_command + allow-list shape.
  * engine_ops   — the façade re-exports the same function OBJECTS as the split modules.
  * control_server — _new_job eviction bound + snapshot DB-error branches.

Everything is faked: NO real Neon / Upstash / R2 / boto3 / subprocess / sockets. Run:
  python -m pytest tests/test_coordination_unit.py -q
"""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import psycopg

import locking
import wake
import manifest
import db
import runner
import commands
import engine_ops
import control_server as CS

_HAS_FCNTL = importlib.util.find_spec("fcntl") is not None


# ====================================================================== fakes
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records executed SQL; returns scripted rows keyed by a lowercase SQL substring. Optionally
    raises a given exception for statements whose lowercased SQL contains `raise_on`."""

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

    def sql_log(self):
        return " || ".join(s.lower() for s, _ in self.executed)


class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProc:
    """Stand-in for subprocess.Popen, with a scripted poll() sequence."""

    def __init__(self, poll_returns, wait_return=0, wait_raises=False):
        self._poll = list(poll_returns)
        self._last = None
        self._wait_return = wait_return
        self._wait_raises = wait_raises
        self.terminated = False
        self.killed = False

    def poll(self):
        if self._poll:
            self._last = self._poll.pop(0)
        return self._last

    def wait(self, timeout=None):
        if self._wait_raises:
            raise RuntimeError("wait timed out")
        return self._wait_return

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


class _NoLock:
    """Always-acquires lock stand-in (so handler tests never touch a real .run.lock)."""
    class Locked(Exception):
        pass

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _LockHeld:
    """Lock stand-in whose __enter__ raises Locked (models a concurrent run holding the lock)."""
    class Locked(Exception):
        pass

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        raise _LockHeld.Locked("held")

    def __exit__(self, *exc):
        return False


# ====================================================================== locking
class FileLockBasics(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.p = self.d / ".run.lock"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_lock_path_anchored_under_root(self):
        from _paths import ROOT
        self.assertEqual(locking.LOCK_PATH.name, ".run.lock")
        self.assertEqual(locking.LOCK_PATH.parent, ROOT)

    def test_acquire_creates_file_and_releases(self):
        self.assertFalse(self.p.exists())
        with locking._FileLock(self.p, blocking=False) as lk:
            self.assertIsNotNone(lk._fh)
        self.assertTrue(self.p.exists())   # lock file persists after release

    def test_reacquire_after_release_succeeds(self):
        with locking._FileLock(self.p, blocking=False):
            pass
        with locking._FileLock(self.p, blocking=False):
            pass   # a clean re-acquire must not raise

    def test_constructor_stores_blocking_and_timeout(self):
        lk = locking._FileLock(self.p, blocking=True, timeout=7)
        self.assertTrue(lk.blocking)
        self.assertEqual(lk.timeout, 7)
        self.assertEqual(Path(lk.path), self.p)

    @unittest.skipUnless(_HAS_FCNTL, "in-process contention guarantee holds only for fcntl/flock (POSIX VM)")
    def test_second_acquire_while_held_raises_locked(self):
        with locking._FileLock(self.p, blocking=False):
            with self.assertRaises(locking._FileLock.Locked):
                with locking._FileLock(self.p, blocking=False):
                    pass


# ====================================================================== wake
class UpstashCreds(unittest.TestCase):
    def test_creds_none_when_unset(self):
        with mock.patch.object(wake, "_load_dotenv_into_environ", lambda: None), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(wake._upstash_creds(), (None, None))
            self.assertFalse(wake.upstash_enabled())

    def test_creds_read_primary_env_and_strip_trailing_slash(self):
        with mock.patch.object(wake, "_load_dotenv_into_environ", lambda: None), \
             mock.patch.dict(os.environ, {"UPSTASH_REDIS_REST_URL": "https://x.upstash.io/",
                                          "UPSTASH_REDIS_REST_TOKEN": "tok"}, clear=True):
            self.assertEqual(wake._upstash_creds(), ("https://x.upstash.io", "tok"))
            self.assertTrue(wake.upstash_enabled())

    def test_creds_fall_back_to_kv_rest_names(self):
        with mock.patch.object(wake, "_load_dotenv_into_environ", lambda: None), \
             mock.patch.dict(os.environ, {"KV_REST_API_URL": "https://kv/",
                                          "KV_REST_API_TOKEN": "kvtok"}, clear=True):
            self.assertEqual(wake._upstash_creds(), ("https://kv", "kvtok"))


class UpstashRest(unittest.TestCase):
    def test_upstash_returns_none_without_creds_and_skips_network(self):
        sent = []
        with mock.patch.object(wake, "_upstash_creds", return_value=(None, None)), \
             mock.patch.object(wake.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: sent.append(1)):
            self.assertIsNone(wake._upstash(["GET", "k"]))
        self.assertEqual(sent, [])   # no network call attempted

    def test_upstash_posts_command_and_parses_result(self):
        captured = {}

        class _Resp:
            def __enter__(self_):
                return self_

            def __exit__(self_, *exc):
                return False

            def read(self_):
                return json.dumps({"result": "OK"}).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["data"] = req.data
            captured["method"] = req.get_method()
            captured["auth"] = req.headers.get("Authorization")
            return _Resp()

        with mock.patch.object(wake, "_upstash_creds", return_value=("https://x", "tok")), \
             mock.patch.object(wake.urllib.request, "urlopen", _fake_urlopen):
            out = wake._upstash(["SET", "k", "v"])
        self.assertEqual(out, "OK")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(json.loads(captured["data"]), ["SET", "k", "v"])
        self.assertEqual(captured["auth"], "Bearer tok")

    def test_upstash_swallows_network_error(self):
        def _boom(*a, **k):
            raise OSError("connection refused")
        with mock.patch.object(wake, "_upstash_creds", return_value=("https://x", "tok")), \
             mock.patch.object(wake.urllib.request, "urlopen", _boom):
            self.assertIsNone(wake._upstash(["GET", "k"]))


class WakeCommands(unittest.TestCase):
    def test_heartbeat_writes_set_with_ttl(self):
        with mock.patch.object(wake, "_upstash", return_value="OK") as up:
            wake.heartbeat_upstash()
        cmd = up.call_args[0][0]
        self.assertEqual(cmd[0], "SET")
        self.assertEqual(cmd[1], wake.HEARTBEAT_KEY)
        self.assertEqual(cmd[3], "EX")
        self.assertEqual(cmd[4], str(wake.HEARTBEAT_TTL))
        # value is an ISO timestamp
        datetime.fromisoformat(cmd[2])

    def test_wake_pending_reflects_flag(self):
        with mock.patch.object(wake, "_upstash", return_value="1"):
            self.assertTrue(wake.wake_pending())
        with mock.patch.object(wake, "_upstash", return_value=None):
            self.assertFalse(wake.wake_pending())

    def test_clear_wake_issues_del(self):
        with mock.patch.object(wake, "_upstash", return_value=1) as up:
            wake.clear_wake()
        self.assertEqual(up.call_args[0][0], ["DEL", wake.WAKE_KEY])

    def test_signal_wake_sets_flag_with_expiry(self):
        with mock.patch.object(wake, "_upstash", return_value="OK") as up:
            wake.signal_wake()
        self.assertEqual(up.call_args[0][0], ["SET", wake.WAKE_KEY, "1", "EX", "3600"])


class HeartbeatDaemon(unittest.TestCase):
    def setUp(self):
        wake.stop_heartbeat_daemon()
        if wake._HB_THREAD is not None:
            wake._HB_THREAD.join(2.0)
        wake._HB_THREAD = None
        wake._HB_STOP = None

    def tearDown(self):
        wake.stop_heartbeat_daemon()
        if wake._HB_THREAD is not None:
            wake._HB_THREAD.join(2.0)
        wake._HB_THREAD = None
        wake._HB_STOP = None

    def test_daemon_calls_heartbeat_then_idempotent_then_stops(self):
        ev = threading.Event()
        calls = []

        def _fake_hb():
            calls.append(1)
            ev.set()

        with mock.patch.object(wake, "heartbeat_upstash", _fake_hb):
            wake.start_heartbeat_daemon(interval=0.01)
            t1 = wake._HB_THREAD
            self.assertTrue(ev.wait(2.0), "daemon never fired a heartbeat")
            # a second start while alive is a no-op (same thread, no second daemon).
            wake.start_heartbeat_daemon(interval=0.01)
            self.assertIs(wake._HB_THREAD, t1)
            wake.stop_heartbeat_daemon()
            t1.join(2.0)
            self.assertFalse(t1.is_alive())
        self.assertTrue(calls)


# ====================================================================== manifest
class Tail(unittest.TestCase):
    def test_empty_and_none(self):
        self.assertEqual(manifest._tail(""), "")
        self.assertEqual(manifest._tail(None), "")

    def test_short_text_unchanged(self):
        self.assertEqual(manifest._tail("abc", 100), "abc")

    def test_truncates_to_last_nbytes(self):
        text = "".join(str(i % 10) for i in range(100))
        out = manifest._tail(text, 10)
        self.assertEqual(out, text[-10:])
        self.assertEqual(len(out.encode("utf-8")), 10)

    def test_multibyte_boundary_does_not_raise(self):
        # cut through the middle of multibyte chars: must decode with replacement, never raise. The
        # last 9 of the kept 10 bytes are 3 whole € chars, so they survive; the stray leading byte
        # becomes a replacement char rather than blowing up.
        text = "€" * 50          # each € is 3 bytes in utf-8
        out = manifest._tail(text, 10)
        self.assertIsInstance(out, str)
        self.assertTrue(out.endswith("€€€"))


class NewRunId(unittest.TestCase):
    def test_request_id_wins(self):
        self.assertEqual(manifest._new_run_id("schedule", "abc"), "req-abc")

    def test_schedule_branch_dated(self):
        self.assertTrue(manifest._new_run_id("schedule", None).startswith("daily-"))

    def test_manual_branch_timestamped(self):
        rid = manifest._new_run_id("manual", None)
        self.assertTrue(rid.startswith("manual-"))
        self.assertTrue(rid.endswith("Z"))


class ScopeArgsEdges(unittest.TestCase):
    def test_as_of_longer_than_16_chars_trimmed(self):
        self.assertEqual(
            manifest.scope_to_run_args({"all_due": True, "as_of": "2026-06-17 12:00:59 trailing"}),
            ["--mode", "production", "--as-of", "2026-06-17 12:00"])

    def test_non_string_asset_ids_coerced(self):
        self.assertEqual(
            manifest.scope_to_run_args({"assets": [123, "BTC"]}),
            ["--mode", "production", "--asset", "123", "--asset", "btc"])


class ReadRunManifest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, datedir, payload, mtime):
        sub = self.d / "runs" / datedir
        sub.mkdir(parents=True, exist_ok=True)
        f = sub / "run_manifest.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(f, (mtime, mtime))
        return f

    def test_missing_runs_dir_returns_none(self):
        with mock.patch.object(manifest, "ROOT", self.d):
            self.assertEqual(manifest._read_run_manifest(), (None, None))

    def test_picks_newest_by_mtime(self):
        self._write("2026-06-17", {"run_id": "old"}, 1000)
        newf = self._write("2026-06-18", {"run_id": "new"}, 2000)
        with mock.patch.object(manifest, "ROOT", self.d):
            data, path = manifest._read_run_manifest()
        self.assertEqual(data["run_id"], "new")
        self.assertEqual(path, newf)

    def test_since_filters_a_stale_prior_manifest(self):
        self._write("2026-06-17", {"run_id": "old"}, 1000)
        with mock.patch.object(manifest, "ROOT", self.d):
            # newest manifest mtime (1000) is OLDER than `since` (5000) -> this run never wrote one.
            self.assertEqual(manifest._read_run_manifest(since=5000), (None, None))

    def test_malformed_json_returns_none_data_with_path(self):
        f = self.d / "runs" / "2026-06-19" / "run_manifest.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{not valid json", encoding="utf-8")
        os.utime(f, (3000, 3000))
        with mock.patch.object(manifest, "ROOT", self.d):
            data, path = manifest._read_run_manifest()
        self.assertIsNone(data)
        self.assertEqual(path, f)


# ====================================================================== db
class LoadDotenv(unittest.TestCase):
    def test_reads_env_without_overriding_existing(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / ".env").write_text("A=fromfile\n# comment\n\nB=2\nC = 3 \nNOEQLINE\n", encoding="utf-8")
            with mock.patch.object(db, "ROOT", d), \
                 mock.patch.dict(os.environ, {"A": "preexisting"}, clear=False):
                db._load_dotenv_into_environ()
                self.assertEqual(os.environ["A"], "preexisting")   # not overridden
                self.assertEqual(os.environ["B"], "2")
                self.assertEqual(os.environ["C"], "3")             # value trimmed
                self.assertNotIn("NOEQLINE", os.environ)           # no '=' -> skipped
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_missing_env_file_is_noop(self):
        d = Path(tempfile.mkdtemp())
        try:
            with mock.patch.object(db, "ROOT", d):
                db._load_dotenv_into_environ()   # no .env present -> must not raise
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class EmptyDir(unittest.TestCase):
    def test_returns_false_for_non_directory(self):
        d = Path(tempfile.mkdtemp())
        try:
            self.assertFalse(db._empty_dir(d / "does-not-exist"))
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_clears_files_and_subdirs_returns_true(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "f.txt").write_text("x", encoding="utf-8")
            sub = d / "child"
            sub.mkdir()
            (sub / "g.txt").write_text("y", encoding="utf-8")
            self.assertTrue(db._empty_dir(d))
            self.assertEqual(list(d.iterdir()), [])
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class Utcnow(unittest.TestCase):
    def test_is_timezone_aware_utc(self):
        self.assertEqual(db._utcnow().tzinfo, timezone.utc)


class ReapStaleRuns(unittest.TestCase):
    def test_default_age_is_run_timeout_plus_one_hour(self):
        c = FakeConn()
        db.EngineDB(c).reap_stale_runs()
        params = [p for s, p in c.executed if "make_interval" in s.lower()]
        self.assertTrue(params)
        self.assertEqual(params[0][0], db.RUN_TIMEOUT + 3600)

    def test_explicit_age_passed_through_as_int(self):
        c = FakeConn()
        db.EngineDB(c).reap_stale_runs(123)
        params = [p for s, p in c.executed if "make_interval" in s.lower()]
        self.assertEqual(params[0][0], 123)

    def test_swallows_db_error(self):
        c = FakeConn(raise_on="update engine_runs", raise_exc=RuntimeError)
        db.EngineDB(c).reap_stale_runs()   # must not raise; the second UPDATE still runs
        self.assertTrue(any("engine_state" in s.lower() for s, _ in c.executed))


class EngineDBLifecycle(unittest.TestCase):
    def test_own_mode_closes_conn_on_exit(self):
        fake = FakeConn()
        with mock.patch.object(db, "connect", return_value=fake):
            with db.EngineDB.connect() as edb:
                self.assertIs(edb.conn, fake)
            self.assertTrue(fake.closed)

    def test_borrow_mode_never_closes_conn(self):
        fake = FakeConn()
        with db.EngineDB(fake) as edb:
            self.assertIs(edb.conn, fake)
        self.assertFalse(fake.closed)

    def test_opscontext_holds_db(self):
        sentinel = object()
        self.assertIs(db.OpsContext(sentinel).db, sentinel)


class ClaimUndefinedTable(unittest.TestCase):
    def test_generation_requests_claim_propagates_undefined_table(self):
        c = FakeConn(raise_on="generation_requests", raise_exc=psycopg.errors.UndefinedTable)
        with self.assertRaises(psycopg.errors.UndefinedTable):
            db.claim_next_request(c)

    def test_command_claim_swallows_undefined_table_to_none(self):
        # the engine_commands claim wrapper lives in commands.py (not migrated yet -> quiet None).
        c = FakeConn(raise_on="engine_commands", raise_exc=psycopg.errors.UndefinedTable)
        self.assertIsNone(commands.claim_next_command(c))


# ====================================================================== runner: publish chain
class PublishChain(unittest.TestCase):
    @staticmethod
    def _step_of(cmd):
        s = " ".join(str(x) for x in cmd)
        if "export_content" in s:
            return "export"
        if "publish" in s:
            return "publish"
        if "sync-db" in s:
            return "sync"
        return "?"

    def test_all_steps_succeed_in_order(self):
        order = []

        def _run(cmd, **k):
            order.append(self._step_of(cmd))
            return FakeCompleted(0, "out")

        with mock.patch.object(runner.subprocess, "run", side_effect=_run), \
             mock.patch.object(runner, "is_cancel_requested", return_value=False):
            ok, err, log = runner._publish_chain(FakeConn(), None)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(order, ["export", "publish", "sync"])

    def test_export_failure_is_fatal_and_skips_sync(self):
        ran = []

        def _run(cmd, **k):
            step = self._step_of(cmd)
            ran.append(step)
            return FakeCompleted(2 if step == "export" else 0, "boom")

        with mock.patch.object(runner.subprocess, "run", side_effect=_run), \
             mock.patch.object(runner, "is_cancel_requested", return_value=False):
            ok, err, log = runner._publish_chain(FakeConn(), None)
        self.assertFalse(ok)
        self.assertIn("export exited 2", err)
        self.assertNotIn("sync", ran)   # fatal export stops the chain before sync

    def test_publish_nonzero_is_non_fatal_warns_but_syncs(self):
        ran = []

        def _run(cmd, **k):
            step = self._step_of(cmd)
            ran.append(step)
            return FakeCompleted(1 if step == "publish" else 0, "r2 down")

        with mock.patch.object(runner.subprocess, "run", side_effect=_run), \
             mock.patch.object(runner, "is_cancel_requested", return_value=False):
            ok, err, log = runner._publish_chain(FakeConn(), None)
        self.assertTrue(ok)                 # non-fatal: still publishes to Neon
        self.assertIn("Re-publish", err)
        self.assertIn("sync", ran)

    def test_publish_launch_exception_is_non_fatal(self):
        def _run(cmd, **k):
            if self._step_of(cmd) == "publish":
                raise OSError("no such file: python")
            return FakeCompleted(0, "ok")

        with mock.patch.object(runner.subprocess, "run", side_effect=_run), \
             mock.patch.object(runner, "is_cancel_requested", return_value=False):
            ok, err, log = runner._publish_chain(FakeConn(), None)
        self.assertTrue(ok)
        self.assertIn("publish failed to launch", err)

    def test_export_launch_exception_is_fatal(self):
        def _run(cmd, **k):
            if self._step_of(cmd) == "export":
                raise OSError("python missing")
            return FakeCompleted(0)

        with mock.patch.object(runner.subprocess, "run", side_effect=_run), \
             mock.patch.object(runner, "is_cancel_requested", return_value=False):
            ok, err, log = runner._publish_chain(FakeConn(), None)
        self.assertFalse(ok)
        self.assertIn("export failed to launch", err)

    def test_cancel_before_export_aborts_chain(self):
        with mock.patch.object(runner.subprocess, "run",
                               side_effect=AssertionError("must not run a step when cancelled")), \
             mock.patch.object(runner, "is_cancel_requested", return_value=True):
            ok, err, log = runner._publish_chain(FakeConn(), "req-1")
        self.assertFalse(ok)
        self.assertEqual(err, "cancelled before export")


class RunSyncBacktest(unittest.TestCase):
    def test_success_returns_true_and_log(self):
        with mock.patch.object(runner.subprocess, "run", return_value=FakeCompleted(0, "pushed")):
            ok, log = runner._run_sync_backtest()
        self.assertTrue(ok)
        self.assertIn("pushed", log)

    def test_nonzero_returns_false(self):
        with mock.patch.object(runner.subprocess, "run", return_value=FakeCompleted(3, "err")):
            ok, log = runner._run_sync_backtest()
        self.assertFalse(ok)

    def test_launch_failure_returns_false(self):
        with mock.patch.object(runner.subprocess, "run", side_effect=OSError("no python")):
            ok, log = runner._run_sync_backtest()
        self.assertFalse(ok)
        self.assertIn("failed to launch", log)


class Terminate(unittest.TestCase):
    def test_soft_terminates_then_waits(self):
        p = FakeProc([None])
        runner._terminate(p)
        self.assertTrue(p.terminated)
        self.assertFalse(p.killed)

    def test_hard_kills_immediately(self):
        p = FakeProc([None])
        runner._terminate(p, hard=True)
        self.assertTrue(p.killed)
        self.assertFalse(p.terminated)

    def test_soft_escalates_to_kill_when_wait_hangs(self):
        p = FakeProc([None], wait_raises=True)
        runner._terminate(p)
        self.assertTrue(p.terminated)
        self.assertTrue(p.killed)


class WipeSandboxState(unittest.TestCase):
    def test_empties_sandbox_dirs_and_clears_neon_tables(self):
        d = Path(tempfile.mkdtemp())
        try:
            for sub in runner.SANDBOX_DIRS:
                (d / sub).mkdir(parents=True, exist_ok=True)
                (d / sub / "leftover.txt").write_text("stale", encoding="utf-8")
            c = FakeConn()
            with mock.patch.object(runner, "ROOT", d):
                runner._wipe_sandbox_state(c)
            for sub in runner.SANDBOX_DIRS:
                self.assertEqual(list((d / sub).iterdir()), [], f"{sub} not emptied")
            deletes = [s for s, _ in c.executed if s.lower().startswith("delete from")]
            self.assertEqual(len(deletes), 2)
            joined = " ".join(deletes).lower()
            self.assertIn("backtest_predictions", joined)
            self.assertIn("backtest_results", joined)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class ExecRunDaily(unittest.TestCase):
    def test_launch_failure_returns_failed(self):
        with mock.patch.object(runner.subprocess, "Popen", side_effect=OSError("denied")):
            status, results, errors, log = runner._exec_run_daily(FakeConn(), ["--mode", "production"], None)
        self.assertEqual(status, "failed")
        self.assertEqual(results, {})
        self.assertIn("could not launch run_daily.py", errors)

    def test_success_parses_manifest(self):
        manifest_payload = {"run_id": "daily-x", "generated": 1, "jobs": []}
        with mock.patch.object(runner.subprocess, "Popen", return_value=FakeProc([0], wait_return=0)), \
             mock.patch.object(runner, "_read_run_manifest", return_value=(manifest_payload, "p")):
            status, results, errors, log = runner._exec_run_daily(FakeConn(), ["--mode", "production"], None)
        self.assertEqual(status, "done")
        self.assertIsNone(errors)
        self.assertEqual(results.get("generated"), 1)

    def test_cancel_terminates_and_reports_cancelled(self):
        proc = FakeProc([None, None, None], wait_return=0)
        with mock.patch.object(runner.subprocess, "Popen", return_value=proc), \
             mock.patch.object(runner, "is_cancel_requested", return_value=True), \
             mock.patch.object(runner, "_read_run_manifest", return_value=(None, None)):
            status, results, errors, log = runner._exec_run_daily(FakeConn(), ["--mode", "production"], "req-9")
        self.assertEqual(status, "cancelled")
        self.assertIn("cancelled by admin", errors)
        self.assertTrue(proc.terminated)

    def test_timeout_reports_failed(self):
        # patch the clock so the first loop iteration is already past RUN_TIMEOUT.
        times = iter([1000.0] + [1000.0 + runner.RUN_TIMEOUT + 5] * 6)
        with mock.patch.object(runner.subprocess, "Popen", return_value=FakeProc([None, None], wait_return=0)), \
             mock.patch.object(runner.time, "time", lambda: next(times)), \
             mock.patch.object(runner, "_read_run_manifest", return_value=(None, None)):
            status, results, errors, log = runner._exec_run_daily(FakeConn(), ["--mode", "production"], None)
        self.assertEqual(status, "failed")
        self.assertIn("timed out", errors)

    def test_nonzero_exit_reports_failed_with_code(self):
        with mock.patch.object(runner.subprocess, "Popen", return_value=FakeProc([7], wait_return=7)), \
             mock.patch.object(runner, "_read_run_manifest", return_value=(None, None)):
            status, results, errors, log = runner._exec_run_daily(FakeConn(), ["--mode", "production"], None)
        self.assertEqual(status, "failed")
        self.assertIn("exited 7", errors)


# ====================================================================== commands
class ClearWakeAndSandbox(unittest.TestCase):
    def test_clear_wake_calls_clear_wake(self):
        c = FakeConn()
        with mock.patch.object(commands, "clear_wake") as cw:
            ok, msg, log, restart = commands._cmd_clear_wake(c, {})
        cw.assert_called_once()
        self.assertTrue(ok)
        self.assertFalse(restart)

    def test_clear_sandbox_empties_only_sandbox_dirs(self):
        d = Path(tempfile.mkdtemp())
        try:
            for sub in commands.SANDBOX_DIRS:
                (d / sub).mkdir(parents=True, exist_ok=True)
                (d / sub / "x.json").write_text("{}", encoding="utf-8")
            # a non-sandbox dir must be left untouched.
            (d / "ledger").mkdir(parents=True, exist_ok=True)
            (d / "ledger" / "outcome_ledger.csv").write_text("keep", encoding="utf-8")
            with mock.patch.object(commands, "ROOT", d), mock.patch.object(commands, "_FileLock", _NoLock):
                ok, msg, log, restart = commands._cmd_clear_sandbox(None, {})
            self.assertTrue(ok)
            for sub in commands.SANDBOX_DIRS:
                self.assertEqual(list((d / sub).iterdir()), [])
            self.assertTrue((d / "ledger" / "outcome_ledger.csv").exists())   # live ledger untouched
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_clear_sandbox_reports_lock_held(self):
        with mock.patch.object(commands, "_FileLock", _LockHeld):
            ok, msg, log, restart = commands._cmd_clear_sandbox(None, {})
        self.assertFalse(ok)
        self.assertIn("another run is in progress", msg)


class TailLogs(unittest.TestCase):
    def _fallback_conn(self):
        return FakeConn({"from engine_runs": [
            {"id": "run-1", "status": "done", "started_at": "2026-06-28T00:00:00Z",
             "errors": None, "log_excerpt": "all good"}]})

    def test_falls_back_to_engine_runs_when_journalctl_empty(self):
        c = self._fallback_conn()
        with mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(1, "", "")):
            ok, msg, out, restart = commands._cmd_tail_logs(c, {"lines": 200})
        self.assertTrue(ok)
        self.assertIn("run run-1", out)
        self.assertIn("all good", out)

    def test_lines_argument_clamped(self):
        c = self._fallback_conn()
        with mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(1, "", "")):
            self.assertIn("(20 lines requested)", commands._cmd_tail_logs(c, {"lines": 5})[1])
            self.assertIn("(1000 lines requested)", commands._cmd_tail_logs(c, {"lines": 99999})[1])
            self.assertIn("(200 lines requested)", commands._cmd_tail_logs(c, {"lines": "oops"})[1])
            self.assertIn("(200 lines requested)", commands._cmd_tail_logs(c, {})[1])

    def test_unknown_unit_falls_back_to_known_default(self):
        c = self._fallback_conn()
        with mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(1, "", "")):
            ok, msg, out, restart = commands._cmd_tail_logs(c, {"unit": "evil.service", "lines": 50})
        self.assertIn("assetframe-poller", msg)
        self.assertNotIn("evil.service", msg)


class ServiceCheck(unittest.TestCase):
    class _FakeR2:
        def list_objects_v2(self, **kw):
            return {"Contents": []}

    def test_all_services_reachable(self):
        c = FakeConn({"select 1": [{"ok": 1}]})
        with mock.patch.object(commands, "_r2_client", return_value=(self._FakeR2(), "bkt")), \
             mock.patch.object(commands, "upstash_enabled", return_value=True), \
             mock.patch.object(commands, "_upstash", return_value="2026-06-28T00:00:00Z"):
            ok, msg, out, restart = commands._cmd_service_check(c, {})
        self.assertTrue(ok)
        self.assertIn("all reachable", msg)
        self.assertIn("Neon:", out)

    def test_neon_failure_reported(self):
        c = FakeConn(raise_on="select 1", raise_exc=RuntimeError)
        with mock.patch.object(commands, "_r2_client", return_value=(self._FakeR2(), "bkt")), \
             mock.patch.object(commands, "upstash_enabled", return_value=False):
            ok, msg, out, restart = commands._cmd_service_check(c, {})
        self.assertFalse(ok)
        self.assertIn("Neon:    FAIL", out)

    def test_upstash_not_configured_is_not_a_failure(self):
        c = FakeConn({"select 1": [{"ok": 1}]})
        with mock.patch.object(commands, "_r2_client", return_value=(self._FakeR2(), "bkt")), \
             mock.patch.object(commands, "upstash_enabled", return_value=False):
            ok, msg, out, restart = commands._cmd_service_check(c, {})
        self.assertTrue(ok)
        self.assertIn("Upstash: not configured", out)


class R2Client(unittest.TestCase):
    def test_raises_when_env_unset(self):
        with mock.patch.object(commands, "_load_dotenv_into_environ", lambda: None), \
             mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                commands._r2_client()
        self.assertIn("R2_", str(ctx.exception))


class ComputeDue(unittest.TestCase):
    def test_updates_engine_assets_from_plan(self):
        plan = [{"asset_id": "btc", "decision": "generate", "reason": "due"},
                {"asset_id": "xau", "decision": "hold", "reason": "not due"}]
        c = FakeConn()
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "ok")), \
             mock.patch.object(commands, "_read_run_manifest", return_value=({"plan": plan}, "p")):
            ok, msg, out, restart = commands._cmd_compute_due(c, {})
        self.assertTrue(ok)
        self.assertIn("1/2 due now", msg)
        self.assertEqual(sum(1 for s, _ in c.executed if "update engine_assets" in s.lower()), 2)

    def test_no_plan_is_reported(self):
        c = FakeConn()
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "ok")), \
             mock.patch.object(commands, "_read_run_manifest", return_value=({}, "p")):
            ok, msg, out, restart = commands._cmd_compute_due(c, {})
        self.assertFalse(ok)
        self.assertIn("no plan", msg.lower())

    def test_dry_run_nonzero_reported(self):
        c = FakeConn()
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(4, "", "bad")):
            ok, msg, out, restart = commands._cmd_compute_due(c, {})
        self.assertFalse(ok)
        self.assertIn("dry_run exited 4", msg)

    def test_missing_due_columns_reported(self):
        plan = [{"asset_id": "btc", "decision": "generate", "reason": "due"}]
        c = FakeConn(raise_on="update engine_assets", raise_exc=psycopg.errors.UndefinedColumn)
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "ok")), \
             mock.patch.object(commands, "_read_run_manifest", return_value=({"plan": plan}, "p")):
            ok, msg, out, restart = commands._cmd_compute_due(c, {})
        self.assertFalse(ok)
        self.assertIn("migrate:up", msg)

    def test_lock_held_skips(self):
        with mock.patch.object(commands, "_FileLock", _LockHeld):
            ok, msg, out, restart = commands._cmd_compute_due(FakeConn(), {})
        self.assertFalse(ok)
        self.assertIn("compute-due skipped", msg)


class RunScoring(unittest.TestCase):
    def test_success_runs_publish_chain(self):
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "scored")), \
             mock.patch.object(commands, "_publish_chain", return_value=(True, None, "plog")) as pc:
            ok, msg, out, restart = commands._cmd_run_scoring(FakeConn(), {})
        pc.assert_called_once()
        self.assertTrue(ok)
        self.assertIn("synced", msg)

    def test_scoring_nonzero_short_circuits_before_publish(self):
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(2, "boom")), \
             mock.patch.object(commands, "_publish_chain",
                               side_effect=AssertionError("publish must not run after a failed score")):
            ok, msg, out, restart = commands._cmd_run_scoring(FakeConn(), {})
        self.assertFalse(ok)
        self.assertIn("scoring run exited 2", msg)

    def test_publish_failure_still_reports_local_score(self):
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "scored")), \
             mock.patch.object(commands, "_publish_chain", return_value=(False, "sync exited 1", "plog")):
            ok, msg, out, restart = commands._cmd_run_scoring(FakeConn(), {})
        self.assertTrue(ok)               # the local ledger WAS scored
        self.assertIn("sync failed", msg.lower())

    def test_lock_held_reported(self):
        with mock.patch.object(commands, "_FileLock", _LockHeld):
            ok, msg, out, restart = commands._cmd_run_scoring(FakeConn(), {})
        self.assertFalse(ok)
        self.assertIn("retry run_scoring", msg)


class PullLatest(unittest.TestCase):
    def test_all_steps_succeed_then_restart(self):
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", return_value=FakeCompleted(0, "ok")):
            ok, msg, log, restart = commands._cmd_pull_latest(FakeConn(), {})
        self.assertTrue(ok)
        self.assertTrue(restart)
        self.assertIn("pulled latest", msg)

    def test_failed_step_aborts_without_restart(self):
        def _run(cmd, **k):
            if cmd[:2] == ["git", "pull"]:
                return FakeCompleted(1, "diverged")
            return FakeCompleted(0, "ok")
        with mock.patch.object(commands, "_FileLock", _NoLock), \
             mock.patch.object(commands.subprocess, "run", side_effect=_run):
            ok, msg, log, restart = commands._cmd_pull_latest(FakeConn(), {})
        self.assertFalse(ok)
        self.assertFalse(restart)
        self.assertIn("git exited 1", msg)

    def test_lock_held_reported(self):
        with mock.patch.object(commands, "_FileLock", _LockHeld):
            ok, msg, log, restart = commands._cmd_pull_latest(FakeConn(), {})
        self.assertFalse(ok)
        self.assertIn("retry pull_latest", msg)


class RunCommandDispatch(unittest.TestCase):
    def test_json_string_args_parsed_to_dict(self):
        seen = {}

        def _probe(conn, args):
            seen.update(args)
            return True, "ok", None, False

        c = FakeConn()
        with mock.patch.dict(commands._COMMAND_HANDLERS, {"probe": _probe}):
            commands.run_command(c, {"id": "c1", "command": "probe", "args": '{"x": 1}'})
        self.assertEqual(seen, {"x": 1})

    def test_non_dict_args_coerced_to_empty(self):
        seen = []

        def _probe(conn, args):
            seen.append(args)
            return True, "ok", None, False

        c = FakeConn()
        with mock.patch.dict(commands._COMMAND_HANDLERS, {"probe": _probe}):
            commands.run_command(c, {"id": "c1", "command": "probe", "args": "[1, 2]"})
        self.assertEqual(seen, [{}])

    def test_finish_command_writes_outcome_and_returns_dict(self):
        c = FakeConn()
        res = commands._finish_command(c, "c7", "done", "result text", "log text", True)
        self.assertEqual(res["status"], "done")
        self.assertEqual(res["result"], "result text")
        self.assertTrue(res["restart"])
        sql, params = c.executed[-1]
        self.assertIn("update engine_commands set status", sql.lower())
        self.assertEqual(params[0], "done")
        self.assertEqual(params[1], "result text")
        self.assertEqual(params[3], "c7")


class AllowList(unittest.TestCase):
    def test_allowed_commands_match_handler_keys(self):
        self.assertEqual(commands.ALLOWED_COMMANDS, tuple(commands._COMMAND_HANDLERS.keys()))

    def test_every_allowed_command_has_callable_handler(self):
        for name in commands.ALLOWED_COMMANDS:
            self.assertTrue(callable(commands._COMMAND_HANDLERS[name]), name)


# ====================================================================== engine_ops façade
class Facade(unittest.TestCase):
    def test_reexports_are_the_same_objects(self):
        pairs = [
            (engine_ops.scope_to_run_args, manifest.scope_to_run_args),
            (engine_ops.summarize_manifest, manifest.summarize_manifest),
            (engine_ops._tail, manifest._tail),
            (engine_ops._FileLock, locking._FileLock),
            (engine_ops.LOCK_PATH, locking.LOCK_PATH),
            (engine_ops.connect, db.connect),
            (engine_ops.database_url, db.database_url),
            (engine_ops.EngineDB, db.EngineDB),
            (engine_ops.heartbeat_upstash, wake.heartbeat_upstash),
            (engine_ops.signal_wake, wake.signal_wake),
            (engine_ops.run_and_record, runner.run_and_record),
            (engine_ops._exec_run_daily, runner._exec_run_daily),
            (engine_ops.run_command, commands.run_command),
            (engine_ops._cmd_service_check, commands._cmd_service_check),
        ]
        for via_facade, source in pairs:
            self.assertIs(via_facade, source)

    def test_run_timeout_and_allowlist_reexported(self):
        self.assertEqual(engine_ops.RUN_TIMEOUT, db.RUN_TIMEOUT)
        self.assertEqual(engine_ops.ALLOWED_COMMANDS, commands.ALLOWED_COMMANDS)


# ====================================================================== control_server gaps
class JobEviction(unittest.TestCase):
    def setUp(self):
        self._jobs = dict(CS._JOBS)
        self._seq = list(CS._JOB_SEQ)
        self._max = CS._MAX_JOBS
        CS._JOBS.clear()
        CS._JOB_SEQ[0] = 0

    def tearDown(self):
        CS._JOBS.clear()
        CS._JOBS.update(self._jobs)
        CS._JOB_SEQ[0] = self._seq[0]
        CS._MAX_JOBS = self._max

    def test_new_job_evicts_oldest_finished_but_keeps_running(self):
        CS._MAX_JOBS = 2
        CS._JOBS.update({
            "j-a": {"id": "j-a", "status": "done", "created_at": "2020-01-01T00:00:00+00:00"},
            "j-b": {"id": "j-b", "status": "failed", "created_at": "2020-01-02T00:00:00+00:00"},
            "j-r": {"id": "j-r", "status": "running", "created_at": "2020-01-03T00:00:00+00:00"},
        })
        jid = CS._new_job("service_check", {})
        self.assertIn(jid, CS._JOBS)
        self.assertIn("j-r", CS._JOBS)          # a running job is never evicted
        self.assertNotIn("j-a", CS._JOBS)       # oldest finished dropped first
        self.assertNotIn("j-b", CS._JOBS)


class SnapshotErrors(unittest.TestCase):
    def test_state_error_when_engine_state_query_raises(self):
        class _C:
            def execute(self, sql, params=None):
                if "engine_state" in sql:
                    raise RuntimeError("state boom")
                return FakeCursor([])
        snap = CS.snapshot(_C())
        self.assertIn("state_error", snap)
        self.assertFalse(snap["online"])

    def test_runs_error_when_engine_runs_query_raises(self):
        class _C:
            def execute(self, sql, params=None):
                if "engine_state" in sql:
                    return FakeCursor([{"last_heartbeat_at": None, "automation_paused": False,
                                        "current_run_id": None}])
                raise RuntimeError("runs boom")
        snap = CS.snapshot(_C())
        self.assertIn("runs_error", snap)

    def test_naive_heartbeat_does_not_crash_snapshot(self):
        # a naive (tz-less) heartbeat triggers a TypeError on the age subtraction -> caught, offline.
        naive = datetime(2026, 6, 28, 0, 0, 0)
        c = FakeConn()
        c.results = {}

        class _C:
            def execute(self, sql, params=None):
                if "engine_state" in sql:
                    return FakeCursor([{"last_heartbeat_at": naive, "automation_paused": False,
                                        "current_run_id": None}])
                return FakeCursor([])
        snap = CS.snapshot(_C())
        self.assertFalse(snap["online"])
        self.assertIn("state_error", snap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
