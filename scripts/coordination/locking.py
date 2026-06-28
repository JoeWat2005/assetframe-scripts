"""Cross-process run lock (extracted from engine_ops).

A best-effort exclusive lock at LOCK_PATH that serialises run_daily across the daily timer and the
poller. POSIX (the OCI VM) uses fcntl.flock — released automatically if the process dies; Windows
(dev/test) uses msvcrt.locking. blocking=False raises Locked when the lock is already held."""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from _paths import ROOT

LOCK_PATH = ROOT / ".run.lock"          # serialises run_daily across timer + poller


class _FileLock:
    """A best-effort cross-process exclusive lock at LOCK_PATH.

    POSIX (the OCI VM): fcntl.flock — released automatically if the process dies.
    Windows (dev/test): msvcrt.locking — good enough for local structural runs.
    blocking=False raises Locked if the lock is already held (a concurrent run)."""

    class Locked(Exception):
        pass

    def __init__(self, path=LOCK_PATH, blocking=False, timeout=0):
        self.path = Path(path)
        self.blocking = blocking
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            import fcntl   # POSIX
            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            if self.blocking and self.timeout:
                deadline = time.time() + self.timeout
                while True:
                    try:
                        fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.time() >= deadline:
                            raise self.Locked("run lock held (timeout)")
                        time.sleep(0.5)
            else:
                try:
                    fcntl.flock(self._fh, flags)
                except OSError:
                    raise self.Locked("another run holds the lock")
        except ImportError:
            import msvcrt   # Windows
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise self.Locked("another run holds the lock")
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"pid={os.getpid()} at={datetime.now(timezone.utc).isoformat()}\n")
            self._fh.flush()
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            try:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except ImportError:
                import msvcrt
                try:
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
        return False
