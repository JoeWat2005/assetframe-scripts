"""Shared helpers for the long-running scheduler services (poller, scheduled_run)."""
from datetime import datetime, timezone


def service_log(prefix):
    """Return a timestamped stdout logger: '[<prefix> <UTC>Z] <msg>' (flushed for journald)."""
    def _log(msg):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{prefix} {ts}Z] {msg}", flush=True)
    return _log
