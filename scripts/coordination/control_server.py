#!/usr/bin/env python3
"""control_server.py — the box HTTP control plane (behind a Cloudflare Tunnel + Cloudflare Access).

ADDITIVE: this runs ALONGSIDE the poller as its own systemd service, binding 127.0.0.1:<port>;
cloudflared forwards engine.assetframe.co.uk (prod) / engine-dev.assetframe.co.uk (dev) to it. It is
the first step of retiring the 30s Neon poll + Upstash: an authenticated HTTP API the admin dashboard
talks to directly.

  GET  /health        -> liveness (NO auth; for cloudflared / uptime checks)
  GET  /status        -> one-shot snapshot: heartbeat/online, current run, recent runs, paused flag
  GET  /events        -> SSE: pushes that snapshot every ~POLL seconds (the live admin console)
  POST /control       -> {"command","args"}: dispatch an allow-listed engine command as a background
                         JOB; returns a job id immediately (long commands stream progress via /events)
  GET  /jobs/<id>     -> one command job's status/result (polling fallback to SSE)

AUTH — every request except /health must carry a valid `Cf-Access-Jwt-Assertion` header. That JWT is
injected by cloudflared AFTER Cloudflare Access has validated the caller (your email login in a
browser, OR Vercel's service token), so verifying it proves the request came THROUGH Access rather
than around it. We check: RS256 signature against the team JWKS, `aud` == this env's Access app AUD,
and the issuer. POST /control additionally requires a bearer token when ASSETFRAME_CONTROL_TOKEN is
set (defence in depth; the browser never POSTs, only Vercel does). The server binds LOCALHOST only, so
the tunnel + Access are the sole ingress to the box.

ENV (per-checkout .env):
  ASSETFRAME_CONTROL_PORT     8787 (prod) / 8788 (dev)
  ASSETFRAME_CF_TEAM          Zero Trust team name, e.g. wat044  -> issuer https://wat044.cloudflareaccess.com
  ASSETFRAME_CONTROL_AUD      this env's Access application AUD
  ASSETFRAME_CONTROL_TOKEN    optional bearer required on POST /control
  ASSETFRAME_CONTROL_INSECURE =1 skips the Access-JWT check (DEV/LOCAL ONLY — never on the box)

Run:  python -m scripts.coordination.control_server [--port N]
"""
import argparse
import hmac
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import ROOT          # repo-root anchor (scripts/__init__ shim is on sys.path under -m)
import config_loader
config_loader.apply_runtime_env(ROOT / "config" / "engine.json")   # seed engine.json knobs (env wins)
import os

import engine_ops                # façade: connect / ConfigError / run_command / ALLOWED_COMMANDS
import commands as _commands      # ALLOWED_COMMANDS + run_command live here (façade re-exports them too)

from _service import service_log
_log = service_log("control")

# Commands that restart the POLLER process via a self-exit (see commands._cmd_restart_poller /
# _cmd_pull_latest). They only work IN the poller, so the control server (a different process) does
# NOT run them — they stay on the existing dashboard->Neon->poller path. Everything else is in-process
# safe: each handler takes the cross-process run-lock, so it coordinates with the live poller.
_RESTART_ONLY = {"restart_poller", "pull_latest"}
ALLOWED = tuple(c for c in _commands.ALLOWED_COMMANDS if c not in _RESTART_ONLY)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------- config
class ControlConfig:
    """Server config resolved from the environment (after config_loader seeded engine.json)."""

    def __init__(self):
        engine_ops  # noqa  (ensure the façade imported cleanly)
        self.port = self._int_env("ASSETFRAME_CONTROL_PORT", 8787)
        self.team = (os.environ.get("ASSETFRAME_CF_TEAM") or "").strip()
        self.aud = (os.environ.get("ASSETFRAME_CONTROL_AUD") or "").strip()
        self.bearer = (os.environ.get("ASSETFRAME_CONTROL_TOKEN") or "").strip()
        self.insecure = os.environ.get("ASSETFRAME_CONTROL_INSECURE") == "1"

    @staticmethod
    def _int_env(name, default):
        try:
            return int(str(os.environ.get(name, default)).strip())
        except (TypeError, ValueError):
            return default


# ------------------------------------------------------------------------------- auth
class AccessVerifier:
    """Validates the Cloudflare-Access JWT (`Cf-Access-Jwt-Assertion`). Lazily builds a PyJWKClient
    that fetches + caches the team's signing keys, so import never needs the network."""

    def __init__(self, team, aud):
        self.team = team
        self.aud = aud
        self.issuer = f"https://{team}.cloudflareaccess.com" if team else None
        self._jwks = None

    def _client(self):
        if self._jwks is None:
            import jwt  # PyJWT
            self._jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs")
        return self._jwks

    def verify(self, token):
        """Return the decoded claims, or raise. RS256 + aud + issuer all enforced."""
        import jwt  # PyJWT
        if not (self.issuer and self.aud):
            raise ValueError("ASSETFRAME_CF_TEAM / ASSETFRAME_CONTROL_AUD not configured")
        key = self._client().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer)


def authorize(headers, cfg, verifier, *, require_bearer):
    """Gate one request. Returns (ok: bool, reason: str). INSECURE mode (dev) skips the JWT. Otherwise
    the Access JWT must verify; require_bearer (POST /control) also checks the bearer when configured."""
    if not cfg.insecure:
        token = headers.get("Cf-Access-Jwt-Assertion") or headers.get("cf-access-jwt-assertion")
        if not token:
            return False, "missing Cf-Access-Jwt-Assertion (request did not come through Access)"
        try:
            verifier.verify(token)
        except Exception as ex:
            return False, f"invalid Access token: {str(ex)[:160]}"
    if require_bearer:
        # The bearer is the only app-layer factor separating a destructive POST /control write from a
        # read-only GET (one Access app gates both methods), so:
        if cfg.bearer:                                          # configured -> always enforce it
            auth = headers.get("Authorization") or headers.get("authorization") or ""
            sent = auth[7:] if auth.startswith("Bearer ") else ""
            if not (sent and hmac.compare_digest(sent, cfg.bearer)):
                return False, "missing or wrong bearer token"
        elif not cfg.insecure:                                 # secure but UNSET -> fail closed
            return False, "server misconfigured: ASSETFRAME_CONTROL_TOKEN is unset (writes disabled)"
        # else: insecure (local dev) AND no token -> full local bypass
    return True, "ok"


# ------------------------------------------------------------------------------- jobs
# In-memory job table: each POST /control runs a command in a worker thread. Ephemeral by design — a
# command's durable record (for actual RUNS) is engine_runs; these are just the request-acks the SSE
# stream / GET /jobs surfaces. Lost on a control-server restart (acceptable: they're transient).
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOB_SEQ = [0]
_MAX_JOBS = 200


def _new_job(command, args):
    with _JOBS_LOCK:
        _JOB_SEQ[0] += 1
        jid = f"job-{_JOB_SEQ[0]}"
        _JOBS[jid] = {"id": jid, "command": command, "args": args, "status": "running",
                      "result": None, "log": None, "created_at": _utcnow_iso(), "finished_at": None}
        # bound the table — drop only the oldest FINISHED jobs (never evict a still-running one,
        # else its outcome would be lost from /jobs while it is still executing)
        if len(_JOBS) > _MAX_JOBS:
            done = sorted((k for k in _JOBS if _JOBS[k]["status"] in ("done", "failed")),
                          key=lambda k: _JOBS[k]["created_at"])
            for old in done[:len(_JOBS) - _MAX_JOBS]:
                _JOBS.pop(old, None)
    return jid


def _run_job(jid, command, args, runner=None):
    """Execute one command on its own Neon conn, recording the outcome on the in-memory job. Reuses
    engine_ops.run_command (allow-list dispatch + handler); the absent engine_commands id makes its
    bookkeeping UPDATE a no-op, so no row is inserted. `runner` is injectable for tests."""
    run_command = runner or engine_ops.run_command
    status, result, log = "failed", None, None
    try:
        with engine_ops.connect() as conn:
            res = run_command(conn, {"command": command, "args": args})
        status = res.get("status", "failed")
        result = res.get("result")
        log = res.get("log")
    except Exception as ex:
        result = f"control_server job error: {ex}"[:400]
    finally:
        with _JOBS_LOCK:
            j = _JOBS.get(jid)
            if j:
                j.update(status=status, result=result, log=log, finished_at=_utcnow_iso())
    _log(f"job {jid} {command} -> {status}: {result}")


def submit_command(command, args, *, spawn=True, runner=None):
    """Validate + start a command job. Returns (status_code, body_dict). Pure enough to unit-test."""
    if command not in ALLOWED:
        why = ("restart_poller/pull_latest run only via the poller path"
               if command in _RESTART_ONLY else "unknown command")
        return 400, {"error": f"command '{command}' not allowed here ({why})", "allowed": list(ALLOWED)}
    if not isinstance(args, dict):
        return 400, {"error": "args must be an object"}
    jid = _new_job(command, args)
    if spawn:
        threading.Thread(target=_run_job, args=(jid, command, args, runner), daemon=True).start()
    return 202, {"job_id": jid, "command": command, "status": "running"}


def job_status(jid):
    with _JOBS_LOCK:
        j = _JOBS.get(jid)
        return (200, dict(j)) if j else (404, {"error": "no such job"})


# ----------------------------------------------------------------------------- status
def snapshot(conn, *, runs=8, requests=8, cmds=10):
    """Read-only engine status for /status and /events: heartbeat/online, paused, current run, and the
    most recent engine_runs. `online` mirrors the dashboard rule (heartbeat within 90s)."""
    out = {"now": _utcnow_iso(), "online": False, "paused": None, "current_run_id": None,
           "last_heartbeat_at": None, "runs": []}
    try:
        st = conn.execute(
            "SELECT last_heartbeat_at, automation_paused, current_run_id FROM engine_state "
            "WHERE id = 1").fetchone()
        if st:
            hb = st.get("last_heartbeat_at")
            out["last_heartbeat_at"] = hb.isoformat() if hasattr(hb, "isoformat") else hb
            out["paused"] = bool(st.get("automation_paused"))
            out["current_run_id"] = st.get("current_run_id")
            if hb is not None:
                age = (datetime.now(timezone.utc) - hb).total_seconds() if hasattr(hb, "isoformat") else 1e9
                out["online"] = age < 90
    except Exception as ex:
        out["state_error"] = str(ex)[:160]
    try:
        rows = conn.execute(
            "SELECT id, trigger, status, started_at, finished_at, errors FROM engine_runs "
            "ORDER BY started_at DESC LIMIT %s", (int(runs),)).fetchall()
        for r in (rows or []):
            for k in ("started_at", "finished_at"):
                v = r.get(k)
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
            out["runs"].append(r)
    except Exception as ex:
        out["runs_error"] = str(ex)[:160]

    # Generation queue (requests + history) — the dashboard's "Generation queue".
    try:
        rows = conn.execute(
            "SELECT id, scope, status, coalesce(run_id,'') AS run_id, coalesce(requested_by,'') AS requested_by, "
            "cancel_requested, created_at, started_at, finished_at, coalesce(error,'') AS error "
            "FROM generation_requests ORDER BY created_at DESC LIMIT %s", (int(requests),)).fetchall()
        out["requests"] = []
        for r in (rows or []):
            for k in ("created_at", "started_at", "finished_at"):
                v = r.get(k)
                if hasattr(v, "isoformat"):
                    r[k] = v.isoformat()
            out["requests"].append(r)
    except Exception as ex:
        out["requests_error"] = str(ex)[:160]

    # Box command log — recent HTTP control jobs (ephemeral; since the control server started).
    try:
        with _JOBS_LOCK:
            recent = sorted(_JOBS.values(), key=lambda j: j["created_at"], reverse=True)[:int(cmds)]
        out["commands"] = [
            {**j, "log": (j["log"][:600] if isinstance(j.get("log"), str) else j.get("log"))}
            for j in recent
        ]
    except Exception as ex:
        out["commands_error"] = str(ex)[:160]

    # Schedule — for each enabled asset, when it next generates ("waiting for its next generation").
    try:
        import calendar_rules        # bare-name imports via the sys.path shim
        import config_loader
        nowdt = datetime.now(timezone.utc)
        hol = calendar_rules.load_holidays()
        sched = []
        for a in config_loader.load_assets(enabled_only=True):
            nd = calendar_rules.next_due_at(a, nowdt, hol)
            sched.append({"id": a.get("id"), "asset_class": a.get("asset_class"),
                          "cadence": a.get("cadence", "weekday"),
                          "due_now": bool(calendar_rules.is_due(a, nowdt, hol)[0]),
                          "next_due_at": nd.isoformat() if nd else None})
        sched.sort(key=lambda s: (not s["due_now"], s["next_due_at"] or "9999"))
        out["schedule"] = sched
    except Exception as ex:
        out["schedule_error"] = str(ex)[:160]

    return out


def read_snapshot(runs=8):
    """snapshot() on a fresh own-mode conn (None on a DB error, so a tick never raises)."""
    try:
        with engine_ops.connect() as conn:
            return snapshot(conn, runs=runs)
    except Exception as ex:
        return {"now": _utcnow_iso(), "online": False, "error": str(ex)[:200]}


# One SHARED snapshot, refreshed at most every _SNAP_TTL seconds, so N concurrent /events streams +
# /status hit Neon ~once per window instead of once per stream per 2s tick. Concurrent SSE streams are
# capped (each is a worker thread) so an authorised client can't open unbounded live connections.
_SNAP = {"data": None, "at": -1e9}
_SNAP_LOCK = threading.Lock()
_SNAP_TTL = 2.0
_SSE_SLOTS = threading.BoundedSemaphore(8)


def cached_snapshot():
    now = time.monotonic()
    with _SNAP_LOCK:
        if _SNAP["data"] is not None and (now - _SNAP["at"]) < _SNAP_TTL:
            return _SNAP["data"]
        data = read_snapshot()
        _SNAP["data"], _SNAP["at"] = data, time.monotonic()
        return data


# ------------------------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    server_version = "AssetFrameControl/1"
    timeout = 30        # per-request socket deadline: bounds a slow-loris body + a stuck SSE reader
    cfg = None          # set by serve()
    verifier = None     # set by serve()

    def log_message(self, *a):    # silence the default stderr access log; we use service_log
        return

    def _send(self, code, obj, ctype="application/json"):
        body = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, *, require_bearer):
        ok, reason = authorize(self.headers, self.cfg, self.verifier, require_bearer=require_bearer)
        if not ok:
            self._send(403, {"error": reason})
        return ok

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            return self._send(200, {"ok": True, "service": "assetframe-control", "now": _utcnow_iso()})
        if not self._auth(require_bearer=False):
            return
        if path == "/status":
            return self._send(200, cached_snapshot())
        if path == "/events":
            return self._sse()
        if path.startswith("/jobs/"):
            code, body = job_status(path[len("/jobs/"):])
            return self._send(code, body)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/control":
            return self._send(404, {"error": "not found"})
        if not self._auth(require_bearer=True):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self._send(400, {"error": "body must be JSON"})
        code, body = submit_command((payload.get("command") or "").strip(), payload.get("args") or {})
        return self._send(code, body)

    def _sse(self):
        """Stream the SHARED status snapshot every ~2s until the client disconnects. Concurrent
        streams are capped (each is a worker thread); the per-handler socket timeout bounds a stuck
        reader so a half-open client can't pin a thread forever."""
        if not _SSE_SLOTS.acquire(blocking=False):
            return self._send(503, {"error": "too many live streams open; retry shortly"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            while True:
                payload = json.dumps(cached_snapshot()).encode("utf-8")
                self.wfile.write(b"data: " + payload + b"\n\n")
                self.wfile.flush()
                time.sleep(2.0)
        except (BrokenPipeError, ConnectionError, OSError):
            return   # client went away (or the socket timed out) — end the thread
        finally:
            _SSE_SLOTS.release()


def serve(cfg=None):
    cfg = cfg or ControlConfig()
    if cfg.insecure:
        _log("WARNING: ASSETFRAME_CONTROL_INSECURE=1 — Access JWT + bearer checks DISABLED (dev only)")
    else:
        if not (cfg.team and cfg.aud):
            _log("CONFIG ERROR: ASSETFRAME_CF_TEAM and ASSETFRAME_CONTROL_AUD must be set (or use "
                 "ASSETFRAME_CONTROL_INSECURE=1 for local dev)")
            return 1
        if not cfg.bearer:
            # Fail closed (see authorize): the bearer is the write factor for POST /control; without
            # it a destructive write collapses to the same gate as a read. Refuse to start.
            _log("CONFIG ERROR: ASSETFRAME_CONTROL_TOKEN must be set — it is the write factor for "
                 "POST /control. Put it in .env (and the matching Vercel env var), or use "
                 "ASSETFRAME_CONTROL_INSECURE=1 for local dev.")
            return 1
    _Handler.cfg = cfg
    _Handler.verifier = AccessVerifier(cfg.team, cfg.aud)
    httpd = ThreadingHTTPServer(("127.0.0.1", cfg.port), _Handler)
    httpd.daemon_threads = True
    _log(f"listening on 127.0.0.1:{cfg.port} (commands: {', '.join(ALLOWED)})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="AssetFrame box control server (HTTP, behind cloudflared).")
    ap.add_argument("--port", type=int, default=None, help="override ASSETFRAME_CONTROL_PORT")
    args = ap.parse_args(argv)
    cfg = ControlConfig()
    if args.port:
        cfg.port = args.port
    return serve(cfg)


if __name__ == "__main__":
    sys.exit(main())
