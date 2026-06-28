#!/usr/bin/env python3
"""scheduled_run.py — the daily-timer target (systemd oneshot, fired at 05:00 UTC).

This is the AUTOMATIC run. Unlike a manual request from the admin console, it RESPECTS
the automation_paused flag:

  1. heartbeat()
  2. if is_paused()  -> log "automation paused, skipping", record a skipped engine_run
                        note (so the console shows the timer fired but stood down), exit 0.
  3. else            -> run_and_record(trigger='schedule', scope={"all_due": true})
                        which maps to `run_daily.py --mode production` (score closed
                        windows + generate every due asset).

Exit code: 0 on success OR on a clean paused-skip; non-zero only if the run itself
failed to be recorded. run_and_record never raises, so a failed generation still exits
0 (the failure is recorded on the engine_runs row, which is what the console reads).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)
import config_loader
# Seed runtime settings from config/engine.json BEFORE engine_ops reads RUN_TIMEOUT at import (env wins).
config_loader.apply_runtime_env(ROOT / "config" / "engine.json")
import engine_ops


from _service import service_log
_log = service_log("scheduled")


def _record_skip(conn):
    """Write a short engine_runs row noting the timer fired but automation was paused,
    so the admin console reflects it. Best-effort — never fatal."""
    run_id = f"daily-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-skipped"
    try:
        conn.execute(
            "INSERT INTO engine_runs (id, trigger, scope, status, results, "
            "  log_excerpt, started_at, finished_at) "
            "VALUES (%s, 'schedule', %s, 'done', %s, %s, now(), now()) "
            "ON CONFLICT (id) DO UPDATE SET status = 'done', finished_at = now()",
            (run_id, '{"all_due": true}',
             '{"skipped": true, "reason": "automation_paused"}',
             "automation paused — scheduled run skipped"))
    except Exception as ex:
        _log(f"(note: could not record skip row: {ex})")


def main():
    try:
        with engine_ops.connect() as conn:
            engine_ops.heartbeat(conn)
            if engine_ops.is_paused(conn):
                _log("automation paused, skipping the scheduled run")
                _record_skip(conn)
                return 0
            _log("automation active — running the daily production batch (all_due)")
            run_id = engine_ops.run_and_record(
                conn, trigger="schedule", scope={"all_due": True})
            _log(f"scheduled run recorded as {run_id}")
            return 0
    except engine_ops.ConfigError as ex:
        _log(f"CONFIG ERROR: {ex}")
        return 1
    except Exception as ex:
        # run_and_record itself never raises; this guards connect()/heartbeat() only.
        _log(f"scheduled run failed before recording: {ex}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
