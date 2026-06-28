"""engine_ops.py — FAÇADE over the OCI engine runner's coordination layer.

The Oracle Cloud VM has NO inbound ports. It coordinates with the AssetFrame web
app ONLY through three Neon tables: it POLLS + WRITES, the web app READS + ENQUEUES.

    generation_requests  — the admin "Engine console" enqueues a manual scoped run;
                           the poller claims + runs it and writes status/run_id/error.
    engine_runs          — one row per run (schedule or manual): status, results,
                           errors, log tail. The console reads this for live status.
    engine_state         — singleton (id=1): automation_paused, last_heartbeat_at,
                           current_run_id. The heartbeat is how the console knows the
                           VM is "online".

This module used to hold the whole DB + run layer; it has been SPLIT into five sibling
modules in scripts/coordination/ (acyclic DAG: db -> wake, manifest -> runner -> commands):

    db        — DB/run-state foundation: connect/heartbeat/claim/state + config seed + RUN_TIMEOUT.
    wake      — Upstash heartbeat/wake cluster.
    manifest  — run-args scoping + run-manifest read/summary.
    runner    — run lifecycle (run_and_record / backtest batch / _exec_run_daily).
    commands  — engine_commands control channel (allow-listed box commands).

engine_ops re-exports every name those modules expose, so poller.py / scheduled_run.py /
sync_backtest.py and every test's `engine_ops.<name>` reference keep working unchanged. No
behaviour changed in the split — this file is purely a re-export façade now.
"""
import psycopg

from _paths import ROOT, SCRIPTS         # repo-root anchors (the scripts/__init__ shim is on sys.path)
from locking import _FileLock, LOCK_PATH   # run lock lives in locking.py now; re-exported here

from db import (ConfigError, _int_env, RUN_TIMEOUT, _load_dotenv_into_environ, database_url,
                connect, _utcnow, heartbeat, is_paused, set_current_run, claim_next_request,
                is_cancel_requested, reap_stale_runs, EngineDB, OpsContext, RunRecorder, _empty_dir)
from wake import (KEY_PREFIX, HEARTBEAT_KEY, WAKE_KEY, HEARTBEAT_TTL, _upstash_creds,
                  upstash_enabled, _upstash, heartbeat_upstash, start_heartbeat_daemon,
                  stop_heartbeat_daemon, wake_pending, clear_wake, signal_wake)
from manifest import (LOG_EXCERPT_BYTES, scope_to_run_args, _read_run_manifest,
                      summarize_manifest, _new_run_id, _tail)
from runner import (RUN_DAILY, SYNC_BACKTEST, SANDBOX_DIRS, MAX_BACKTEST_DAYS, CANCEL_POLL_SECONDS,
                    _publish_chain, run_and_record, _backdated_as_of, _wipe_sandbox_state,
                    run_backtest_batch, _run_sync_backtest, _exec_run_daily, _terminate,
                    _request_status, _finish_request)
from commands import (_SETTABLE_CONFIG_KEYS, _CONFIG_VALUE_VALIDATORS, _KNOWN_POLLER_UNITS,
                      claim_next_command, reap_stale_commands, run_command, _finish_command,
                      _cmd_restart_poller, _cmd_pull_latest, _cmd_run_maintenance, _cmd_tail_logs,
                      _cmd_set_config, _cmd_sync_assets, _cmd_reset_ledger, _cmd_clear_reports,
                      _cmd_run_scoring, _cmd_compute_due, _cmd_service_check, _cmd_clear_r2,
                      _cmd_clear_wake, _cmd_run_backtest, _cmd_clear_sandbox, _sync_assets_from_neon,
                      _r2_client, _COMMAND_HANDLERS, ALLOWED_COMMANDS)
