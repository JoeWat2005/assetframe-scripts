#!/usr/bin/env python3
"""poller.py — the always-on engine loop (systemd service on the OCI VM).

Each tick (default every 30s):
  1. heartbeat()                       -> engine_state.last_heartbeat_at = now()
                                          (this is how the admin console flips to "online")
  2. claim_next_request()              -> atomically grab the oldest queued request
  3. if claimed: run_and_record(trigger='manual', scope=row.scope, request_id=row.id)

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
from datetime import datetime, timezone

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine_ops


_STOP = False


def _handle_sigterm(signum, frame):       # pragma: no cover - signal path
    global _STOP
    _STOP = True
    _log("SIGTERM received — shutting down after the current tick")


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[poller {ts}Z] {msg}", flush=True)


def tick(conn):
    """One poll cycle against an open connection. Returns the run_id if a request was
    claimed and run this tick, else None. Raises only if the caller's connection is
    unusable (the caller treats that as a tick failure and reconnects next time)."""
    engine_ops.heartbeat(conn)
    row = engine_ops.claim_next_request(conn)
    if not row:
        return None
    req_id = row.get("id")
    scope = row.get("scope") or {}
    _log(f"claimed request {req_id} scope={scope} -> running (manual; ignores pause)")
    run_id = engine_ops.run_and_record(conn, trigger="manual", scope=scope,
                                       request_id=req_id)
    _log(f"request {req_id} finished as run {run_id}")
    return run_id


def run_once():
    """A single tick with its own connection. Returns the run_id or None. Never raises
    (a DB/connection error is logged and swallowed so --once is safe in any state)."""
    try:
        with engine_ops.connect() as conn:
            return tick(conn)
    except engine_ops.ConfigError as ex:
        _log(f"CONFIG ERROR: {ex}")
        raise
    except Exception as ex:
        _log(f"tick error (continuing): {ex}")
        return None


def loop(interval):
    """The long-lived poll loop. One bad tick is logged and skipped; the loop only
    exits on SIGTERM. A fresh connection per tick means a dropped Neon connection
    self-heals on the following tick."""
    _log(f"starting — interval={interval}s. Manual requests run even when paused; "
         f"the daily timer (scheduled_run.py) respects the pause flag.")
    while not _STOP:
        t0 = time.time()
        try:
            with engine_ops.connect() as conn:
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
            time.sleep(min(1.0, end - time.time()))
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
