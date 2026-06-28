"""wake.py — Upstash heartbeat/wake cluster (split out of engine_ops)."""
import os, json, threading, urllib.request

from db import _load_dotenv_into_environ, _utcnow

# --------------------------------------------------------------- Upstash (wake signal)
# The poller's idle loop heartbeats + checks for queued work via Upstash Redis (REST), NOT Neon,
# so the Neon compute can auto-suspend and we stay inside the free-tier compute-hours. Neon is only
# touched when there is real work (a wake flag the web sets on enqueue) or on a periodic safety
# sweep. GRACEFUL: with no Upstash env these return None/False and the poller falls back to polling
# Neon every tick (the original behaviour) — so this is inert until UPSTASH_* is set on the box.
# UPSTASH_KEY_PREFIX namespaces a 2nd environment (e.g. "dev:") onto the SAME Upstash DB so the
# dev + prod pollers don't collide on heartbeat/wake; unset = prod (no prefix). The web must set
# the SAME prefix on its matching environment (Vercel Preview = dev).
KEY_PREFIX = os.environ.get("UPSTASH_KEY_PREFIX", "")
HEARTBEAT_KEY = f"{KEY_PREFIX}af:engine:heartbeat"
WAKE_KEY = f"{KEY_PREFIX}af:engine:wake"
HEARTBEAT_TTL = 180  # seconds; matches the web console's online window


def _upstash_creds():
    _load_dotenv_into_environ()
    url = (os.environ.get("UPSTASH_REDIS_REST_URL")
           or os.environ.get("UPSTASH_KV_REST_API_URL")
           or os.environ.get("KV_REST_API_URL"))
    token = (os.environ.get("UPSTASH_REDIS_REST_TOKEN")
             or os.environ.get("UPSTASH_KV_REST_API_TOKEN")
             or os.environ.get("KV_REST_API_TOKEN"))
    if url and token:
        return url.rstrip("/"), token
    return None, None


def upstash_enabled():
    """True when Upstash REST creds are configured (else the poller stays on Neon polling)."""
    return _upstash_creds()[0] is not None


def _upstash(command):
    """Run one Upstash REST command (e.g. ["SET","k","v"]). Returns the result, or None on any
    error / when Upstash isn't configured. Never raises — a wake-signal blip must not crash the loop."""
    url, token = _upstash_creds()
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url, data=json.dumps(command).encode("utf-8"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("result")
    except Exception:
        return None


def heartbeat_upstash():
    """Write the engine heartbeat to Upstash with a TTL (expires if the poller dies)."""
    return _upstash(["SET", HEARTBEAT_KEY, _utcnow().isoformat(), "EX", str(HEARTBEAT_TTL)])


# --- background heartbeat daemon --------------------------------------------
# A long run_and_record() (a multi-minute run_daily / backtest subprocess) BLOCKS the poller's
# single-threaded loop, so the in-loop heartbeat never fires and the box flips to OFFLINE for the
# whole run. This daemon keeps the Upstash heartbeat (the web's primary online signal) fresh every
# few seconds INDEPENDENTLY of the blocking run. Upstash-only (no Neon connection sharing across
# threads); best-effort (never raises). The in-loop heartbeat(conn)/heartbeat_upstash() calls stay
# as a top-up that also refreshes the Neon heartbeat between runs.
_HB_THREAD = None
_HB_STOP = None


def start_heartbeat_daemon(interval=10):
    """Start (idempotently) a daemon thread that calls heartbeat_upstash() every `interval` seconds
    until stop_heartbeat_daemon(). Call once at poller startup."""
    global _HB_THREAD, _HB_STOP
    if _HB_THREAD is not None and _HB_THREAD.is_alive():
        return
    _HB_STOP = threading.Event()
    stop = _HB_STOP

    def _run():
        while True:
            try:
                heartbeat_upstash()
            except Exception:
                pass
            if stop.wait(interval):     # returns True once stop is set -> exit
                return

    _HB_THREAD = threading.Thread(target=_run, name="assetframe-heartbeat", daemon=True)
    _HB_THREAD.start()


def stop_heartbeat_daemon():
    """Signal the heartbeat daemon to stop (best-effort; the thread is a daemon so it never blocks exit)."""
    if _HB_STOP is not None:
        _HB_STOP.set()


def wake_pending():
    """True if the web flagged that a generation request is waiting (set on enqueue)."""
    return bool(_upstash(["GET", WAKE_KEY]))


def clear_wake():
    """Clear the wake flag — call it right before draining Neon so a request enqueued mid-drain
    re-sets the flag and is picked up next tick (no lost requests)."""
    _upstash(["DEL", WAKE_KEY])


def signal_wake():
    """Set the wake flag (the web does this on enqueue; also handy for tests / manual pokes)."""
    return _upstash(["SET", WAKE_KEY, "1", "EX", "3600"])
