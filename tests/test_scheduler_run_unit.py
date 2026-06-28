"""Offline unit tests for scripts/scheduler/run/ (run_daily + _subproc).

Targets the GAPS left by the existing suites (test_audit_fixes / test_retention / test_scoring_fixes
/ test_generate_batched / test_brief_batch / test_sandbox cover score_step, the batched path,
_parse_last_json basics, retention and the reject-downgrade core). Here we exercise the many small
pure helpers + the synchronous per-asset pipeline plumbing that those suites only touch transitively:
  _subproc:  _envint, _run, _run_rc (subprocess.run faked), _parse_last_json edge cases
  run_daily: parse_args, resolve_now, select_assets, _preserve_pending, _rel_to_root, _safe_unlink,
             _stamp_authored_brief, _sum_token_cost, _total_token_cost, _new_job_rec, _stage_runner,
             _read_json, _data_prep, _finish_asset, _apply_authored, generate_asset, _job_line,
             author_brief_step, _sync_critique_one, _batch_deadline, _retention_days.

Everything is OFFLINE & deterministic: NO real subprocess / network / Anthropic / Neon / R2. Child
launches are faked by monkeypatching RD._run / RD._run_rc (and subprocess.run for the _subproc layer)
and ROOT-anchored dirs are repointed at tmp_path.

Run:  python -m pytest tests/test_scheduler_run_unit.py -q
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_daily as RD
import _subproc as S

UTC = timezone.utc


# ----------------------------------------------------------------- helpers
def _asset(tk="BTC", **over):
    a = {"id": tk.lower(), "ticker": tk, "asset_class": "crypto",
         "provider_symbols": {"yahoo": f"{tk}-USD"}, "session_profile": "crypto_24_7",
         "roll_utc": 0, "cadence": "weekday"}
    a.update(over)
    return a


def _run_factory(fail_substrings=(), out="{}"):
    """Build a fake RD._run that fails for any cmd whose joined string contains a fail substring."""
    def _fake(cmd, timeout=180):
        joined = " ".join(str(c) for c in cmd)
        for s in fail_substrings:
            if s in joined:
                return (False, "", f"boom:{s}")
        return (True, out, "")
    return _fake


# ================================================================= _subproc._envint
def test_envint_returns_default_when_unset(monkeypatch):
    monkeypatch.delenv("AF_TEST_INT", raising=False)
    assert S._envint("AF_TEST_INT", 17) == 17


def test_envint_parses_valid_integer(monkeypatch):
    monkeypatch.setenv("AF_TEST_INT", "42")
    assert S._envint("AF_TEST_INT", 17) == 42


def test_envint_falls_back_on_non_integer(monkeypatch):
    monkeypatch.setenv("AF_TEST_INT", "not-a-number")
    assert S._envint("AF_TEST_INT", 17) == 17


def test_envint_falls_back_on_blank(monkeypatch):
    monkeypatch.setenv("AF_TEST_INT", "")
    assert S._envint("AF_TEST_INT", 5) == 5


# ================================================================= _subproc._run / _run_rc
class _FakeProc:
    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_run_rc_success_returns_code_and_streams(monkeypatch):
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeProc(0, "hello", ""))
    ok, rc, out, err = S._run_rc(["-m", "x"])
    assert (ok, rc, out, err) == (True, 0, "hello", "")


def test_run_rc_nonzero_exit_is_not_ok(monkeypatch):
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeProc(2, "o", "e"))
    ok, rc, out, err = S._run_rc(["-m", "x"])
    assert ok is False and rc == 2 and out == "o" and err == "e"


def test_run_rc_timeout_is_caught(monkeypatch):
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=5)
    monkeypatch.setattr(S.subprocess, "run", _boom)
    ok, rc, out, err = S._run_rc(["-m", "x"], timeout=5)
    assert ok is False and rc == -1 and out == "" and err == "timeout after 5s"


def test_run_rc_generic_exception_is_caught_and_truncated(monkeypatch):
    def _boom(*a, **k):
        raise ValueError("x" * 500)
    monkeypatch.setattr(S.subprocess, "run", _boom)
    ok, rc, out, err = S._run_rc(["-m", "x"])
    assert ok is False and rc == -1 and len(err) == 200


def test_run_drops_returncode(monkeypatch):
    monkeypatch.setattr(S.subprocess, "run", lambda *a, **k: _FakeProc(0, "out", "err"))
    assert S._run(["-m", "x"]) == (True, "out", "err")


# ================================================================= _parse_last_json edge cases
def test_parse_last_json_returns_last_of_multiple_top_level_objects():
    assert RD._parse_last_json('{"a": 1}\n{"b": 2}') == {"b": 2}


def test_parse_last_json_ignores_trailing_garbage_after_valid_object():
    assert RD._parse_last_json('{"decision": "approve"} then {oops not json') == {"decision": "approve"}


def test_parse_last_json_does_not_slice_into_nested_brace():
    # The documented rfind('{') trap: a pretty-printed verdict whose LAST '{' is a nested
    # _telemetry object. The top-level decision must survive.
    pretty = json.dumps({"decision": "revise", "summary": "ok",
                         "_telemetry": {"input_tokens": 9}}, indent=1)
    assert RD._parse_last_json(pretty).get("decision") == "revise"


def test_parse_last_json_top_level_array_yields_last_inner_dict():
    assert RD._parse_last_json('[{"x": 1}, {"y": 2}]') == {"y": 2}


def test_parse_last_json_bare_scalar_has_no_object():
    assert RD._parse_last_json("42") == {}
    assert RD._parse_last_json(None) == {}


# ================================================================= parse_args
def test_parse_args_defaults():
    o = RD.parse_args([])
    assert o["mode"] == "dry_run" and o["workers"] == 4 and o["asset"] == [] and o["sandbox"] is False


def test_parse_args_repeatable_asset_and_flags():
    o = RD.parse_args(["--asset", "btc", "--asset", "eth", "--mode", "production",
                       "--workers", "2", "--no-render", "--sandbox"])
    assert o["asset"] == ["btc", "eth"] and o["mode"] == "production"
    assert o["workers"] == 2 and o["no_render"] is True and o["sandbox"] is True


def test_parse_args_bad_mode_exits():
    with pytest.raises(SystemExit):
        RD.parse_args(["--mode", "turbo"])


def test_parse_args_non_integer_workers_exits():
    with pytest.raises(SystemExit):
        RD.parse_args(["--workers", "lots"])


def test_parse_args_unknown_arg_exits():
    with pytest.raises(SystemExit):
        RD.parse_args(["--frobnicate"])


def test_parse_args_missing_value_exits():
    with pytest.raises(SystemExit):
        RD.parse_args(["--asset"])
    with pytest.raises(SystemExit):
        RD.parse_args(["--universe"])


# ================================================================= resolve_now
def test_resolve_now_as_of_is_utc_and_minute_precise():
    n = RD.resolve_now({"as_of": "2026-06-27 04:30:59", "date": None})
    assert n == datetime(2026, 6, 27, 4, 30, tzinfo=UTC)


def test_resolve_now_date_backfills_as_of_and_is_utc():
    o = {"as_of": None, "date": "2026-06-27"}
    n = RD.resolve_now(o)
    assert n.tzinfo == UTC
    assert n.date() == datetime(2026, 6, 27).date()
    assert o["as_of"] is not None            # --date forwards itself as the as-of moment


def test_resolve_now_default_is_now_utc():
    n = RD.resolve_now({"as_of": None, "date": None})
    assert n.tzinfo == UTC
    assert abs((datetime.now(UTC) - n).total_seconds()) < 30


# ================================================================= select_assets
def test_select_assets_no_filter_returns_all(monkeypatch):
    monkeypatch.setattr(RD.config_loader, "load_assets", lambda u: [_asset("BTC"), _asset("ETH")])
    out = RD.select_assets({"universe": "x", "asset": [], "asset_class": None})
    assert {a["id"] for a in out} == {"btc", "eth"}


def test_select_assets_filters_by_id_case_insensitive(monkeypatch):
    monkeypatch.setattr(RD.config_loader, "load_assets", lambda u: [_asset("BTC"), _asset("ETH")])
    out = RD.select_assets({"universe": "x", "asset": ["BtC"], "asset_class": None})
    assert [a["id"] for a in out] == ["btc"]


def test_select_assets_filters_by_class(monkeypatch):
    monkeypatch.setattr(RD.config_loader, "load_assets",
                        lambda u: [_asset("BTC", asset_class="crypto"), _asset("EUR", asset_class="fx")])
    out = RD.select_assets({"universe": "x", "asset": [], "asset_class": "fx"})
    assert [a["id"] for a in out] == ["eur"]


def test_select_assets_unknown_id_exits(monkeypatch):
    monkeypatch.setattr(RD.config_loader, "load_assets", lambda u: [_asset("BTC")])
    with pytest.raises(SystemExit):
        RD.select_assets({"universe": "x", "asset": ["doge"], "asset_class": None})


# ================================================================= _preserve_pending
def test_preserve_pending_writes_report_id_file(tmp_path):
    pend = tmp_path / "pending"
    RD._preserve_pending(pend, "daily-2026-06-27-BTC", {"report_id": "daily-2026-06-27-BTC", "x": 1})
    f = pend / "daily-2026-06-27-BTC.json"
    assert f.exists()
    assert json.loads(f.read_text(encoding="utf-8"))["x"] == 1


def test_preserve_pending_noop_without_rid(tmp_path):
    pend = tmp_path / "pending"
    RD._preserve_pending(pend, "", {"x": 1})
    assert not pend.exists()                 # nothing created, no raise


# ================================================================= _rel_to_root / _safe_unlink
def test_rel_to_root_inside_root_is_relative(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    assert RD._rel_to_root(tmp_path / "data" / "x.json") == os.path.join("data", "x.json")


def test_rel_to_root_outside_root_is_absolute(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path / "repo")
    other = tmp_path / "elsewhere" / "y.json"
    assert RD._rel_to_root(other) == str(other)


def test_safe_unlink_removes_file_then_tolerates_missing(tmp_path):
    f = tmp_path / "z.json"
    f.write_text("x")
    RD._safe_unlink(f)
    assert not f.exists()
    RD._safe_unlink(f)                       # second call must not raise


# ================================================================= _stamp_authored_brief
def test_stamp_authored_brief_marks_date_and_flag(tmp_path):
    f = tmp_path / "BTC_research_brief.json"
    f.write_text(json.dumps({"thesis": "up"}), encoding="utf-8")
    RD._stamp_authored_brief(f, "2026-06-27")
    b = json.loads(f.read_text(encoding="utf-8"))
    assert b["_af_authored"] is True and b["_af_date"] == "2026-06-27" and b["thesis"] == "up"


def test_stamp_authored_brief_missing_file_is_noop(tmp_path):
    RD._stamp_authored_brief(tmp_path / "nope.json", "2026-06-27")   # best-effort, no raise


# ================================================================= token-cost rollups
def test_sum_token_cost_adds_writer_and_critic():
    tc = {"writer": [{"input_tokens": 100, "output_tokens": 20, "web_searches": 2, "est_cost_usd": 0.10}],
          "critic": [{"input_tokens": 50, "output_tokens": 5, "est_cost_usd": 0.025}]}
    out = RD._sum_token_cost(tc)
    assert out == {"input_tokens": 150, "output_tokens": 25, "web_searches": 2, "est_cost_usd": 0.125}


def test_sum_token_cost_skips_non_dicts_and_empty():
    assert RD._sum_token_cost({"writer": ["oops", None], "critic": []}) == {
        "input_tokens": 0, "output_tokens": 0, "web_searches": 0, "est_cost_usd": 0.0}


def test_total_token_cost_rolls_up_summaries_and_ignores_non_dict():
    tot = RD._total_token_cost([
        {"input_tokens": 10, "output_tokens": 1, "web_searches": 1, "est_cost_usd": 0.01},
        None,
        {"input_tokens": 5, "output_tokens": 2, "web_searches": 0, "est_cost_usd": 0.02}])
    assert tot == {"input_tokens": 15, "output_tokens": 3, "web_searches": 1, "est_cost_usd": 0.03}


# ================================================================= _new_job_rec / _stage_runner
def test_new_job_rec_shape():
    rec = RD._new_job_rec(_asset("BTC"))
    assert rec["asset_id"] == "btc" and rec["ticker"] == "BTC" and rec["status"] == "error"
    assert rec["report_id"] is None and rec["stages"] == {} and rec["errors"] == []
    assert rec["token_cost"]["est_cost_usd"] == 0.0


def test_stage_runner_records_ok(monkeypatch):
    monkeypatch.setattr(RD, "_run", _run_factory(out="payload"))
    rec = RD._new_job_rec(_asset())
    stage = RD._stage_runner(rec)
    ok, out = stage("intraday", ["-m", "scripts.pipeline.marketdata.intraday"])
    assert ok is True and out == "payload" and rec["stages"]["intraday"] == "ok" and rec["errors"] == []


def test_stage_runner_records_failure_with_error(monkeypatch):
    monkeypatch.setattr(RD, "_run", _run_factory(fail_substrings=("intraday",)))
    rec = RD._new_job_rec(_asset())
    stage = RD._stage_runner(rec)
    ok, _out = stage("intraday", ["-m", "scripts.pipeline.marketdata.intraday"])
    assert ok is False and rec["stages"]["intraday"] == "failed"
    assert rec["errors"] and "intraday" in rec["errors"][0]


# ================================================================= _read_json
def test_read_json_roundtrips_and_strips_bom(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps({"k": 1}), encoding="utf-8-sig")     # BOM
    assert RD._read_json(f) == {"k": 1}


def test_read_json_missing_or_malformed_is_none(tmp_path):
    assert RD._read_json(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert RD._read_json(bad) is None


# ================================================================= _data_prep
def test_data_prep_success_builds_memory_pack(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "_run", _run_factory())
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "memory_packs")
    monkeypatch.setattr(RD.mp, "build_pack", lambda a, as_of=None: {"budget": {"approx_tokens": 321}})
    rec = RD._new_job_rec(_asset("BTC"))
    stage = RD._stage_runner(rec)
    ok = RD._data_prep(_asset("BTC"), datetime(2026, 6, 27, 4, 0, tzinfo=UTC), None, rec, stage)
    assert ok is True
    assert rec["stages"]["intraday"] == "ok" and rec["stages"]["memory_pack"] == "ok"
    assert rec["memory_pack_tokens"] == 321
    assert (tmp_path / "memory_packs" / "BTC_memory_pack.json").exists()


def test_data_prep_intraday_failure_sets_data_error(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "_run", _run_factory(fail_substrings=("intraday",)))
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "memory_packs")
    rec = RD._new_job_rec(_asset())
    stage = RD._stage_runner(rec)
    ok = RD._data_prep(_asset(), datetime(2026, 6, 27, tzinfo=UTC), None, rec, stage)
    assert ok is False and rec["status"] == "data_error"


def test_data_prep_memory_pack_failure_is_best_effort(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "_run", _run_factory())
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "memory_packs")
    def _boom(a, as_of=None):
        raise RuntimeError("pack blew up")
    monkeypatch.setattr(RD.mp, "build_pack", _boom)
    rec = RD._new_job_rec(_asset())
    stage = RD._stage_runner(rec)
    ok = RD._data_prep(_asset(), datetime(2026, 6, 27, tzinfo=UTC), None, rec, stage)
    assert ok is True and rec["stages"]["memory_pack"] == "failed"


# ================================================================= _finish_asset
def _payload_on_disk(tmp_path, tk="BTC", rid="daily-2026-06-27-BTC"):
    pdir = tmp_path / "data" / "payloads"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / f"{tk}_af_payload.json").write_text(json.dumps({"report_id": rid}), encoding="utf-8")


def test_finish_asset_happy_path_generates(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "_run", _run_factory())
    _payload_on_disk(tmp_path)
    rec = RD._new_job_rec(_asset("BTC"))
    stage = RD._stage_runner(rec)
    out = RD._finish_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), False, None, rec, stage)
    assert out["status"] == "generated" and out["report_id"] == "daily-2026-06-27-BTC"
    assert rec["stages"]["scaffold"] == "ok" and rec["stages"]["mvp_report"] == "ok"


def test_finish_asset_no_render_is_forecast_only(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "_run", _run_factory())
    _payload_on_disk(tmp_path)
    rec = RD._new_job_rec(_asset("BTC"))
    RD._finish_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), True, None, rec, RD._stage_runner(rec))
    assert rec["status"] == "forecast_only"


def test_finish_asset_scaffold_failure_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "_run", _run_factory(fail_substrings=("scaffold_payload",)))
    rec = RD._new_job_rec(_asset("BTC"))
    RD._finish_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), False, None, rec, RD._stage_runner(rec))
    assert rec["status"] == "scaffold_error" and "mvp_report" not in rec["stages"]


def test_finish_asset_render_failure_is_qa_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "_run", _run_factory(fail_substrings=("mvp_report",)))
    _payload_on_disk(tmp_path)
    rec = RD._new_job_rec(_asset("BTC"))
    RD._finish_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), False, None, rec, RD._stage_runner(rec))
    assert rec["status"] == "qa_failed" and rec["report_id"] == "daily-2026-06-27-BTC"


# ================================================================= _apply_authored
def _ab(status, decision=None, **over):
    d = {"status": status, "decision": decision, "critic_summary": "summary text",
         "issues": [], "token_cost": {"writer": [{"input_tokens": 10}], "critic": []}}
    d.update(over)
    return d


def test_apply_authored_publishable_returns_true(monkeypatch):
    monkeypatch.setattr(RD, "_stamp_authored_brief", lambda *a, **k: None)
    rec = RD._new_job_rec(_asset())
    cont = RD._apply_authored(rec, _ab("authored", "approve"), "/tmp/b.json", "2026-06-27")
    assert cont is True and rec["brief_source"] == "authored"
    assert rec["stages"]["brief"] == "authored" and rec["critic_decision"] == "approve"
    assert rec["token_cost"]["input_tokens"] == 10


def test_apply_authored_writer_unavailable_degrades_to_needs_brief():
    rec = RD._new_job_rec(_asset())
    cont = RD._apply_authored(rec, _ab("writer_unavailable"), "/tmp/b.json", "2026-06-27")
    assert cont is False and rec["status"] == "needs_brief" and rec["stages"]["brief"] == "skipped"


def test_apply_authored_rejected_keeps_status_and_marks_brief():
    rec = RD._new_job_rec(_asset())
    cont = RD._apply_authored(rec, _ab("brief_rejected", "reject"), "/tmp/b.json", "2026-06-27")
    assert cont is False and rec["status"] == "brief_rejected" and rec["stages"]["brief"] == "authored"


# ================================================================= generate_asset (sync glue)
def test_generate_asset_data_error_returns_early(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "_run", _run_factory(fail_substrings=("intraday",)))
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "mp")
    rec = RD.generate_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), no_render=True)
    assert rec["status"] == "data_error" and "duration_s" in rec


def test_generate_asset_needs_brief_when_authoring_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "_run", _run_factory())
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "mp")
    monkeypatch.setattr(RD, "BRIEF_DIR", tmp_path / "briefs")     # empty -> no operator brief present
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", False)
    monkeypatch.setattr(RD.mp, "build_pack", lambda a, as_of=None: {"budget": {}})
    rec = RD.generate_asset(_asset("BTC"), datetime(2026, 6, 27, tzinfo=UTC), no_render=True)
    assert rec["status"] == "needs_brief" and "duration_s" in rec


# ================================================================= _job_line
def test_job_line_generated_has_no_diagnostic_note():
    rec = RD._new_job_rec(_asset("BTC"))
    rec.update(status="generated", report_id="rid-1", duration_s=1.2, brief_source="authored")
    line = RD._job_line(rec)
    assert "BTC" in line and "generated" in line and "->" not in line


def test_job_line_failure_surfaces_reason():
    rec = RD._new_job_rec(_asset("BTC"))
    rec.update(status="needs_brief", duration_s=0.5, critic_decision="revise",
               critic_summary="thin evidence")
    line = RD._job_line(rec)
    assert "->" in line and "critic=revise" in line and "thin evidence" in line


def test_job_line_falls_back_to_errors_when_no_summary():
    rec = RD._new_job_rec(_asset("BTC"))
    rec.update(status="data_error", duration_s=0.1, errors=[{"intraday": "fetch failed"}])
    line = RD._job_line(rec)
    assert "fetch failed" in line


# ================================================================= author_brief_step
def _make_inputs(tmp_path, tk="BTC"):
    monkey_root = tmp_path
    (monkey_root / "data" / "analysis").mkdir(parents=True, exist_ok=True)
    (monkey_root / "data" / "analysis" / f"{tk}_analysis.json").write_text("{}", encoding="utf-8")
    mp_dir = monkey_root / "data" / "memory_packs"
    mp_dir.mkdir(parents=True, exist_ok=True)
    (mp_dir / f"{tk}_memory_pack.json").write_text("{}", encoding="utf-8")
    return mp_dir


def _wire_author(monkeypatch, tmp_path, tk="BTC"):
    mp_dir = _make_inputs(tmp_path, tk)
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "MEMPACK_DIR", mp_dir)
    monkeypatch.setattr(RD, "RESEARCH_DIR", tmp_path / "data" / "research")
    monkeypatch.setattr(RD, "SOCIAL_DIR", tmp_path / "data" / "social")
    return tmp_path / "data" / "briefs" / f"{tk}_research_brief.json"


def test_author_brief_step_missing_inputs_fails_fast(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "mp")
    res = RD.author_brief_step(_asset("BTC"), tmp_path / "b.json")
    assert res["status"] == "brief_failed" and "missing analysis" in res["critic_summary"]


def test_author_brief_step_approve_is_authored(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)

    def fake_rc(cmd, timeout=180):
        j = " ".join(map(str, cmd))
        if "brief_writer" in j:
            return (True, 0, '{"input_tokens": 10, "output_tokens": 2}', "")
        return (True, 0, '{"decision": "approve", "summary": "solid", "_telemetry": {"input_tokens": 5}}', "")
    monkeypatch.setattr(RD, "_run_rc", fake_rc)
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "authored" and res["decision"] == "approve"
    assert res["token_cost"]["writer"] and res["token_cost"]["critic"]


def test_author_brief_step_backed_reject_is_rejected(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)

    def fake_rc(cmd, timeout=180):
        if "brief_writer" in " ".join(map(str, cmd)):
            return (True, 0, "{}", "")
        return (True, 0, '{"decision": "reject", "summary": "bad", "issues": [{"severity": "blocker"}]}', "")
    monkeypatch.setattr(RD, "_run_rc", fake_rc)
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "brief_rejected" and res["decision"] == "reject"


def test_author_brief_step_blockerless_reject_downgraded_to_authored(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)

    def fake_rc(cmd, timeout=180):
        if "brief_writer" in " ".join(map(str, cmd)):
            return (True, 0, "{}", "")
        return (True, 0, '{"decision": "reject", "summary": "feels weak"}', "")   # no blocker cited
    monkeypatch.setattr(RD, "_run_rc", fake_rc)
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "authored" and res["decision"] == "revise"


def test_author_brief_step_stand_aside(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)

    def fake_rc(cmd, timeout=180):
        if "brief_writer" in " ".join(map(str, cmd)):
            return (True, 0, "{}", "")
        return (True, 0, '{"decision": "stand_aside", "stand_aside_reason": "no edge", "summary": "x"}', "")
    monkeypatch.setattr(RD, "_run_rc", fake_rc)
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "brief_stand_aside" and "no edge" in res["critic_summary"]


def test_author_brief_step_keyless_writer_degrades(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)
    monkeypatch.setattr(RD, "_run_rc", lambda cmd, timeout=180: (False, 3, "", "no ANTHROPIC_API_KEY"))
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "writer_unavailable"


def test_author_brief_step_critic_no_decision_degrades(monkeypatch, tmp_path):
    brief = _wire_author(monkeypatch, tmp_path)

    def fake_rc(cmd, timeout=180):
        if "brief_writer" in " ".join(map(str, cmd)):
            return (True, 0, "{}", "")
        return (True, 0, "garbage with no json verdict", "")
    monkeypatch.setattr(RD, "_run_rc", fake_rc)
    res = RD.author_brief_step(_asset("BTC"), brief)
    assert res["status"] == "writer_unavailable"


# ================================================================= _sync_critique_one
def test_sync_critique_one_returns_parsed_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "RESEARCH_DIR", tmp_path / "data" / "research")
    monkeypatch.setattr(RD, "_run_rc",
                        lambda cmd, timeout=180: (True, 0, '{"decision": "approve"}', ""))
    out = RD._sync_critique_one({"ticker": "BTC", "brief_path": tmp_path / "b.json"})
    assert out == {"decision": "approve"}


def test_sync_critique_one_empty_output_is_empty_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "RESEARCH_DIR", tmp_path / "data" / "research")
    monkeypatch.setattr(RD, "_run_rc", lambda cmd, timeout=180: (True, 0, "no json", ""))
    assert RD._sync_critique_one({"ticker": "BTC", "brief_path": tmp_path / "b.json"}) == {}


# ================================================================= _batch_deadline
def test_batch_deadline_reserves_for_small_universe(monkeypatch):
    monkeypatch.setenv("ASSETFRAME_RUN_TIMEOUT", "5400")
    monkeypatch.setenv("ASSETFRAME_BATCH_TIMEOUT_S", "2400")
    budget = RD._batch_deadline(1) - time.time()
    assert 2390 < budget < 2410             # min(2400, 5400-1800) = 2400


def test_batch_deadline_floors_at_300_for_large_universe(monkeypatch):
    monkeypatch.setenv("ASSETFRAME_RUN_TIMEOUT", "5400")
    monkeypatch.setenv("ASSETFRAME_BATCH_TIMEOUT_S", "2400")
    budget = RD._batch_deadline(100) - time.time()   # reserve dwarfs run_to -> floored at 300
    assert 290 < budget < 310


# ================================================================= _retention_days
def test_retention_days_default(monkeypatch):
    monkeypatch.delenv("ASSETFRAME_RETENTION_DAYS", raising=False)
    assert RD._retention_days() == RD._RETENTION_DEFAULT_DAYS


def test_retention_days_env_override(monkeypatch):
    monkeypatch.setenv("ASSETFRAME_RETENTION_DAYS", "7")
    assert RD._retention_days() == 7


def test_retention_days_zero_disables(monkeypatch):
    monkeypatch.setenv("ASSETFRAME_RETENTION_DAYS", "0")
    assert RD._retention_days() == 0


def test_retention_days_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("ASSETFRAME_RETENTION_DAYS", "soon")
    assert RD._retention_days(default=9) == 9
