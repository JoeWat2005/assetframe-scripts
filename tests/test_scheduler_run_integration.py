"""Phase-2 INTEGRATION tests for scripts/scheduler/run/ (run_daily orchestration).

Unlike test_scheduler_run_unit.py (which exercises each helper in isolation), this suite drives the
REAL top-level orchestrator RD.main() end-to-end and asserts the CROSS-MODULE flow + data contracts:

  * the scheduler's `plan` in the manifest is exactly what calendar_rules.is_due() decides for the
    REAL universe (config/assets.json, loaded by the REAL config_loader) — scheduler<->calendar wiring;
  * a DUE asset flows the REAL in-process pipeline _data_prep -> author_brief_step (+ the REAL
    _downgrade_unbacked_reject verdict path) -> _finish_asset -> manifest, with the per-asset status
    transitions and the runs/<date>/run_manifest.json that main() actually writes;
  * the on-disk hand-off contract BETWEEN stages: the (faked) intraday child writes
    data/analysis/<TK>_analysis.json that author_brief_step reads; the (faked) scaffold child writes
    data/payloads/<TK>_af_payload.json that _finish_asset reads back the report_id from. A path/format
    drift between "where a child writes" and "where the orchestrator reads" would surface here.

ONLY true external boundaries are faked: the spawning of OTHER processes (RD._run / RD._run_rc — the
intraday / scaffold / render / brief_writer / critic children) and the memory_pack builder (network).
Everything else — config_loader, calendar_rules, the whole run_daily orchestration, manifest writing —
is the REAL code. ROOT and the data dirs are repointed at tmp so nothing touches the real
ledger/reports/runs. The faked children EMULATE their real on-disk side effects (write the analysis /
payload / brief files at the exact paths the real children do) so the data contract is genuinely
exercised, not stubbed away.

Run:  python -m pytest tests/test_scheduler_run_integration.py -q
"""
import json
import os
import sys
from datetime import timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_daily as RD

UTC = timezone.utc

# Genuine repo root, captured BEFORE any per-test monkeypatch repoints RD.ROOT at tmp. Used to point
# the orchestrator at the REAL config/assets.json universe (a real fixture, per the task rules).
REPO_ROOT = RD.ROOT
REAL_UNIVERSE = str(REPO_ROOT / "config" / "assets.json")


# --------------------------------------------------------------------------- fake child processes
def _name_after(cmd, flag):
    cmd = [str(c) for c in cmd]
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _make_children(root, *, verdict, writer_tele=None, intraday_ok=True, scaffold_ok=True,
                   render_ok=True, report_id="daily-2026-06-26-BTC", calls=None):
    """Build (fake_run, fake_run_rc) standing in for the spawned engine children. They reproduce the
    REAL children's on-disk side effects so the cross-module file hand-off is actually tested:
      intraday        -> writes data/analysis/<TK>_analysis.json   (read by author_brief_step)
      scaffold_payload-> writes data/payloads/<TK>_af_payload.json (read back by _finish_asset)
      brief_writer    -> writes the --out brief file               (stamped + purged by the orchestrator)
      critic          -> prints the JSON `verdict` on stdout       (parsed by _parse_last_json)
    """
    writer_tele = writer_tele or {"input_tokens": 120, "output_tokens": 30,
                                  "web_searches": 2, "est_cost_usd": 0.15}

    def fake_run(cmd, timeout=180):
        j = " ".join(str(c) for c in cmd)
        if calls is not None:
            calls.append(j)
        if "marketdata.intraday" in j:
            tk = _name_after(cmd, "--name")
            if intraday_ok and tk:
                p = root / "data" / "analysis" / f"{tk}_analysis.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            return (intraday_ok, "{}", "" if intraday_ok else "intraday boom")
        if "scoring.scaffold_payload" in j:
            tk = [str(c) for c in cmd][2]   # positional NAME right after the -m module
            if scaffold_ok:
                p = root / "data" / "payloads" / f"{tk}_af_payload.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"report_id": report_id}), encoding="utf-8")
            return (scaffold_ok, "{}", "" if scaffold_ok else "scaffold boom")
        if "render.mvp_report" in j:
            return (render_ok, "{}", "" if render_ok else "qa boom")
        # ledger_context, calibrate, research_memory, ledger_db, scoring intraday, etc.
        return (True, "{}", "")

    def fake_run_rc(cmd, timeout=180):
        j = " ".join(str(c) for c in cmd)
        if calls is not None:
            calls.append(j)
        if "authoring.brief_writer" in j:
            out = _name_after(cmd, "--out")
            if out:
                p = root / out
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"thesis": "x"}), encoding="utf-8")
            return (True, 0, json.dumps(writer_tele), "")
        if "authoring.critic" in j:
            return (True, 0, json.dumps(verdict), "")
        return (True, 0, "{}", "")

    return fake_run, fake_run_rc


def _raiser(*_a, **_k):
    raise AssertionError("a child subprocess was spawned on a path that must not spawn one")


# --------------------------------------------------------------------------- main() driver
def _wire(monkeypatch, tmp_path):
    """Repoint every ROOT-anchored path at tmp + kill the network boundary (memory_pack)."""
    monkeypatch.setattr(RD, "ROOT", tmp_path)
    monkeypatch.setattr(RD, "BRIEF_DIR", tmp_path / "data" / "briefs")
    monkeypatch.setattr(RD, "MEMPACK_DIR", tmp_path / "data" / "memory_packs")
    monkeypatch.setattr(RD, "RESEARCH_DIR", tmp_path / "data" / "research")
    monkeypatch.setattr(RD, "SOCIAL_DIR", tmp_path / "data" / "social")
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path / "data" / "predictions")
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", True)   # exercise the author+critic path
    monkeypatch.setattr(RD, "BRIEF_BATCH", False)      # synchronous per-asset path
    monkeypatch.setattr(RD.mp, "build_pack", lambda a, as_of=None: {"budget": {"approx_tokens": 222}})
    monkeypatch.delenv("ASSETFRAME_SANDBOX", raising=False)


def _drive(monkeypatch, tmp_path, argv, fake_run, fake_run_rc):
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(RD, "_run", fake_run)
    monkeypatch.setattr(RD, "_run_rc", fake_run_rc)
    monkeypatch.setattr(sys, "argv", ["run_daily"] + argv)
    RD.main()
    return _read_manifest(tmp_path)


def _read_manifest(root):
    files = list((root / "runs").glob("*/run_manifest.json"))
    assert files, "main() wrote no runs/<date>/run_manifest.json"
    return json.loads(files[0].read_text(encoding="utf-8"))


def _job(manifest, ticker):
    return next(j for j in manifest["jobs"] if j["ticker"] == ticker)


# ===========================================================================================
# 1. dry_run: the manifest is JUST the plan, and the plan IS calendar_rules.is_due over the
#    REAL universe. No child subprocess may be spawned.
# ===========================================================================================
def test_dry_run_plan_matches_calendar_rules_over_real_universe(monkeypatch, tmp_path):
    # A Saturday: crypto (btc/eth) is still due 24/7 while every other class skips ("market closed
    # - weekend"), so BOTH plan branches + their reasons are checked against calendar_rules.
    as_of = "2026-06-27 06:00"
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "dry_run", "--universe", REAL_UNIVERSE, "--as-of", as_of],
                      _raiser, _raiser)

    # the orchestrator loaded the REAL 8-asset universe via the REAL config_loader
    assets = RD.config_loader.load_assets(REAL_UNIVERSE)
    holidays = RD.calendar_rules.load_holidays()
    now = RD.resolve_now({"as_of": as_of, "date": None})

    assert manifest["mode"] == "dry_run"
    assert manifest["assets_selected"] == len(assets)
    plan_by_id = {p["asset_id"]: p for p in manifest["plan"]}
    assert set(plan_by_id) == {a["id"] for a in assets}

    # DATA CONTRACT: every plan row's decision/reason equals calendar_rules.is_due for that asset.
    expected_due = 0
    for a in assets:
        due, reason = RD.calendar_rules.is_due(a, now, holidays)
        row = plan_by_id[a["id"]]
        assert row["decision"] == ("generate" if due else "skip"), a["id"]
        assert row["reason"] == reason, a["id"]
        assert row["asset_class"] == a["asset_class"]
        expected_due += 1 if due else 0
    assert manifest["assets_due"] == expected_due
    # the Saturday genuinely exercises BOTH branches: crypto due, the rest weekend-skipped.
    assert plan_by_id["btc"]["decision"] == "generate"
    assert plan_by_id["aapl"]["decision"] == "skip"
    assert "weekend" in plan_by_id["aapl"]["reason"]
    decisions = {p["decision"] for p in manifest["plan"]}
    assert decisions == {"generate", "skip"}

    # dry_run writes the plan ONLY — no generation/scoring artefacts leaked in.
    for k in ("jobs", "generated", "score", "needs_brief", "token_cost"):
        assert k not in manifest, f"dry_run manifest must not carry {k!r}"
    assert "retention" not in manifest   # pruning is skipped on dry runs


# ===========================================================================================
# 2. generate path, critic APPROVE: a due asset flows the full in-process pipeline to "generated"
#    and the report_id round-trips through the scaffold->payload->_finish_asset file contract.
# ===========================================================================================
def test_generate_only_approve_flows_data_prep_to_generated(monkeypatch, tmp_path):
    verdict = {"decision": "approve", "summary": "solid setup",
               "_telemetry": {"input_tokens": 40, "output_tokens": 8, "est_cost_usd": 0.02}}
    fr, frc = _make_children(tmp_path, verdict=verdict, report_id="daily-2026-06-26-BTC")
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)

    assert manifest["assets_selected"] == 1 and manifest["assets_due"] == 1
    assert manifest["run_id"] == f"daily-{manifest['run_date']}"
    job = _job(manifest, "BTC")
    assert job["status"] == "generated"
    assert job["brief_source"] == "authored"
    assert job["critic_decision"] == "approve"
    # the full ordered stage chain ran (cross-module: intraday -> mempack -> brief -> ledger_context
    # -> scaffold -> render).
    assert job["stages"]["intraday"] == "ok"
    assert job["stages"]["memory_pack"] == "ok"
    assert job["stages"]["brief"] == "authored"
    assert job["stages"]["ledger_context"] == "ok"
    assert job["stages"]["scaffold"] == "ok"
    assert job["stages"]["mvp_report"] == "ok"
    # FILE-CONTRACT: report_id the orchestrator surfaced == what the scaffold child wrote to the
    # payload _finish_asset reads back. Proves the data/payloads/<TK>_af_payload.json path is shared.
    assert job["report_id"] == "daily-2026-06-26-BTC"
    payload = tmp_path / "data" / "payloads" / "BTC_af_payload.json"
    assert json.loads(payload.read_text())["report_id"] == job["report_id"]
    # the faked intraday wrote the analysis the brief writer required
    assert (tmp_path / "data" / "analysis" / "BTC_analysis.json").exists()

    # token cost rolls writer (120) + critic _telemetry (40) up into the job AND the run total
    assert job["token_cost"]["input_tokens"] == 160
    assert manifest["token_cost"]["input_tokens"] == 160
    assert manifest["generated"] == 1
    assert manifest["needs_brief"] == [] and manifest["brief_rejected"] == []


# ===========================================================================================
# 3. verdict path: a critic 'reject' with NO concrete blocker is DOWNGRADED to 'revise'
#    (_downgrade_unbacked_reject) and the brief still PUBLISHES -> generated. This is the
#    cross-module verdict edge the task calls out.
# ===========================================================================================
def test_generate_only_blockerless_reject_is_downgraded_and_publishes(monkeypatch, tmp_path):
    verdict = {"decision": "reject", "summary": "feels a bit thin"}  # no publish_blockers, no issues
    fr, frc = _make_children(tmp_path, verdict=verdict)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "generated"
    assert job["critic_decision"] == "revise"          # downgraded from reject
    assert "[reject downgraded" in job.get("critic_summary", "")
    assert manifest["generated"] == 1 and manifest["brief_rejected"] == []


# ===========================================================================================
# 4. verdict path: a BACKED 'reject' (blocker-severity issue cited) skips the asset -> the
#    pipeline stops BEFORE _finish_asset (no scaffold/render).
# ===========================================================================================
def test_generate_only_backed_reject_skips_before_finish(monkeypatch, tmp_path):
    verdict = {"decision": "reject", "summary": "fabricated level",
               "issues": [{"severity": "blocker", "msg": "invented support at 50k"}]}
    fr, frc = _make_children(tmp_path, verdict=verdict)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "brief_rejected"
    assert job["critic_decision"] == "reject"
    assert "scaffold" not in job["stages"] and "mvp_report" not in job["stages"]
    assert manifest["generated"] == 0
    assert manifest["brief_rejected"] == ["BTC"]
    # the rejected draft was purged (not left to leak into the next stage)
    assert not (tmp_path / "data" / "briefs" / "BTC_research_brief.json").exists()


# ===========================================================================================
# 5. a data-prep (intraday) failure short-circuits to data_error and never reaches authoring.
# ===========================================================================================
def test_generate_only_intraday_failure_is_data_error(monkeypatch, tmp_path):
    calls = []
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"},
                             intraday_ok=False, calls=calls)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "data_error"
    assert "brief" not in job["stages"] and "scaffold" not in job["stages"]
    assert manifest["generated"] == 0
    assert not any("authoring.brief_writer" in c for c in calls)  # authoring never spawned


# ===========================================================================================
# 6. render/QA failure -> qa_failed, but the report_id was still read from the payload first.
# ===========================================================================================
def test_generate_only_render_failure_is_qa_failed(monkeypatch, tmp_path):
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"},
                             render_ok=False, report_id="daily-2026-06-26-BTC")
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "qa_failed"
    assert job["report_id"] == "daily-2026-06-26-BTC"
    assert manifest["generated"] == 0


# ===========================================================================================
# 7. --no-render takes the forecast-only branch (and still counts as a successful generation).
# ===========================================================================================
def test_generate_only_no_render_is_forecast_only(monkeypatch, tmp_path):
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"})
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--no-render", "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "forecast_only"
    assert manifest["generated"] == 1   # forecast_only is counted alongside generated


# ===========================================================================================
# 8. production mode wires score_step AND generation into the SAME manifest. With an empty
#    predictions dir the score block is present-but-empty and generation still completes.
# ===========================================================================================
def test_production_mode_carries_both_score_and_generation(monkeypatch, tmp_path):
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"})
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "production", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    # score_step ran (empty universe of predictions -> nothing scored, memory refreshed)
    assert "score" in manifest
    s = manifest["score"]
    assert s["scored"] == [] and s["errors"] == []
    assert isinstance(s["memory_refresh"], dict)
    # generation ran in the SAME run/manifest
    assert manifest["generated"] == 1
    assert _job(manifest, "BTC")["status"] == "generated"
    # non-dry run prunes -> a retention summary is attached
    assert "retention" in manifest
