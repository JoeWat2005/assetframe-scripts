#!/usr/bin/env python3
"""poller.py — the always-on engine loop (systemd service on the OCI VM).

Each tick (default every 30s):
  1. only open Neon when there is work -> the web sets an Upstash WAKE flag on enqueue; plus a
                                          periodic Neon safety sweep. (No Upstash env? fall back to
                                          opening Neon every tick — the original behaviour.) Upstash
                                          now carries ONLY the wake flag (+ the web's rate-limiting);
                                          the box's `online` is the control server's systemd
                                          liveness check, so the poller writes no Upstash heartbeat.
  2. when on Neon: heartbeat(conn) + drain every queued request (run_and_record, manual trigger).

MANUAL requests run even when automation_paused is true — enqueuing a request is an
explicit admin action, distinct from the scheduled timer (scheduled_run.py respects
the pause flag; the poller does not). The pause flag is for the *automatic* daily run.

Robustness (this process must basically never die):
  - A DB error in a single tick is logged and the loop continues — one bad tick never
    exits the service (systemd would restart it, but we prefer to ride out a Neon blip).
  - Each tick uses a fresh connection so a broken connection self-heals next tick.
  - SIGTERM (systemd stop) finishes the current tick's bookkeeping and exits cleanly.

Flags:
  --interval N   seconds between ticks (default 30)
  --once         run exactly one tick then exit (used by tests / manual checks)
"""
import argparse
import signal
import sys
import time

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)
import config_loader
# Seed runtime settings from config/engine.json BEFORE engine_ops reads RUN_TIMEOUT at import (env wins).
config_loader.apply_runtime_env(ROOT / "config" / "engine.json")
import engine_ops


_STOP = False


def _handle_sigterm(signum, frame):       # pragma: no cover - signal path
    global _STOP
    _STOP = True
    _log("SIGTERM received — shutting down after the current tick")


from _service import service_log
_log = service_log("poller")


SAFETY_NEON_EVERY = 60   # ticks — a periodic Neon claim (~30 min at 30s) in case a wake was missed


def _drain(conn):
    """Claim and run EVERY queued request until the queue is empty (or SIGTERM). Returns the last
    run_id, or None if nothing was queued."""
    last = None
    while not _STOP:
        row = engine_ops.claim_next_request(conn)
        if not row:
            break
        req_id = row.get("id")
        scope = row.get("scope") or {}
        _log(f"claimed request {req_id} scope={scope} -> running (manual; ignores pause)")
        last = engine_ops.run_and_record(conn, trigger="manual", scope=scope, request_id=req_id)
        _log(f"request {req_id} finished as run {last}")
    return last


def _drain_commands(conn):
    """Claim and run EVERY queued admin box-command (restart/pull/maintenance/logs/config) until
    the queue is empty. Returns True if a command asked the poller to restart — the caller then
    self-exits and systemd Restart=always relaunches it (the only restart path, since the poller
    can't sudo). Commands are drained at the TOP of a tick, before generation, so a restart is only
    ever handled when no generation subprocess is running (single-threaded loop)."""
    while not _STOP:
        row = engine_ops.claim_next_command(conn)
        if not row:
            break
        cmd = (row.get("command") or "").strip()
        _log(f"claimed command {row.get('id')} -> {cmd}")
        res = engine_ops.run_command(conn, row)
        _log(f"command {row.get('id')} {cmd} -> {res.get('status')}: {res.get('result')}")
        if res.get("restart"):
            return True
    return False


def tick(conn):
    """One Neon service pass on an open connection: stamp the Neon heartbeat (display only), drain
    admin commands, then drain the generation queue. The cheap idle wake-check happens in
    loop()/run_once() BEFORE Neon opens."""
    global _STOP
    engine_ops.heartbeat(conn)
    if _drain_commands(conn):
        # A restart/pull command ran: record-then-exit so systemd relaunches us (onto new code if
        # pull_latest ran). Skip the generation drain this tick; the queued run is picked up after
        # relaunch. Setting _STOP returns cleanly from loop() -> systemd restarts (it didn't stop us).
        _log("restart requested by admin command — exiting for systemd to relaunch")
        _STOP = True
        return None
    return _drain(conn)


def run_once():
    """A single pass (used by --once / tests): check Neon once. Never raises on a transient error,
    so --once is safe in any state."""
    try:
        with engine_ops.connect() as conn:
            engine_ops.clear_wake()
            return tick(conn)
    except engine_ops.ConfigError as ex:
        _log(f"CONFIG ERROR: {ex}")
        raise
    except Exception as ex:
        _log(f"tick error (continuing): {ex}")
        return None


def loop(interval):
    """The long-lived poll loop. Each tick only opens a Neon connection when there is work (the web's
    Upstash wake flag), on a periodic safety sweep, or when Upstash isn't configured (it then falls
    back to the original per-tick Neon polling). This lets the Neon compute auto-suspend while idle.
    The box's `online` is the control server's systemd liveness check — the poller writes no Upstash
    heartbeat. One bad tick is logged and skipped; only SIGTERM exits."""
    using = "Upstash wake flag" if engine_ops.upstash_enabled() else "Neon (no Upstash env — per-tick polling)"
    _log(f"starting — interval={interval}s; work signal via {using}. "
         f"Manual requests run even when paused; the daily timer respects the pause flag.")
    n = 0
    reaped = False
    while not _STOP:
        t0 = time.time()
        n += 1
        try:
            do_neon = (not reaped                       # FORCE the first pass: run startup reap +
                       or not engine_ops.upstash_enabled()  # config-sync immediately on (re)start, not
                       or engine_ops.wake_pending()         # up to SAFETY_NEON_EVERY ticks (~30 min)
                       or n % SAFETY_NEON_EVERY == 0)       # later — else the box runs the stale
                                                            # bootstrap universe after a deploy.
            if do_neon:
                with engine_ops.connect() as conn:
                    engine_ops.clear_wake()   # going to Neon now; a request enqueued mid-drain re-sets it
                    if not reaped:
                        # First Neon pass of this process: clear any phantom 'running' COMMAND left by
                        # the process we just replaced (commands run in the poller, so any 'running' one
                        # at startup is provably orphaned), and rebuild config/assets.json from Neon so
                        # the box's universe always reflects the dashboard after a deploy/restart (the
                        # git-tracked config/assets.json is only a bootstrap default).
                        engine_ops.reap_stale_commands(conn)
                        try:
                            _ok, _msg = engine_ops._sync_assets_from_neon(conn)
                            _log(f"startup config sync: {_msg}")
                        except Exception as ex:
                            _log(f"startup config sync skipped: {ex}")
                        reaped = True
                    # Reap orphaned 'running' RUNS by AGE every Neon pass (not just startup): the daily
                    # run is a separate oneshot, so a run it leaves stuck (killed mid-run) is reconciled
                    # within a tick rather than waiting for a manual poller restart. Age-based, so a
                    # legitimately in-flight run is never swept.
                    engine_ops.reap_stale_runs(conn)
                    tick(conn)
        except engine_ops.ConfigError as ex:
            # No DATABASE_URL is not transient — fail loudly so systemd surfaces it.
            _log(f"CONFIG ERROR: {ex}")
            return 1
        except Exception as ex:
            _log(f"tick error (continuing): {ex}")
        # sleep the remainder of the interval, but wake promptly on SIGTERM.
        elapsed = time.time() - t0
        remaining = max(0.0, interval - elapsed)
        end = time.time() + remaining
        while not _STOP and time.time() < end:
            time.sleep(max(0.0, min(1.0, end - time.time())))   # never negative across the clock edge
    _log("stopped cleanly")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="AssetFrame engine poller — claims queued generation_requests from "
                    "Neon and runs run_daily.py (manual trigger). Long-lived systemd service.")
    ap.add_argument("--interval", type=int, default=30,
                    help="seconds between poll ticks (default: 30)")
    ap.add_argument("--once", action="store_true",
                    help="run a single tick then exit (for testing)")
    args = ap.parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        signal.signal(signal.SIGINT, _handle_sigterm)
    except Exception:
        pass

    if args.once:
        try:
            run_once()
        except engine_ops.ConfigError:
            return 1
        return 0
    return loop(args.interval)


if __name__ == "__main__":
    sys.exit(main())
