"""Integration tests for run_daily's batched generation orchestration (generate_due_batched) and the
batch/sync selector (_generate_due). The Anthropic batch calls + the subprocess stages (intraday /
scaffold / render) are stubbed; the test asserts the PHASING and the per-asset status bookkeeping —
authored -> critique decision -> finish, and the robust fallback to the synchronous path.

Run:  python -m pytest tests/test_generate_batched.py -q
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json

import run_daily as RD

NOW = datetime(2026, 6, 25, 8, 0, tzinfo=timezone.utc)


# --- regression: _parse_last_json must read the critic's PRETTY-PRINTED verdict ----------------
# critic.py prints json.dumps({**verdict, "_telemetry": telemetry}, indent=1). The last '{' is the
# nested _telemetry object; the old rfind('{') parsed THAT (no "decision") and silently dropped a
# good verdict to needs_brief.

def test_parse_last_json_pretty_critic_verdict():
    verdict = {"decision": "revise", "summary": "minor edits",
               "issues": [{"severity": "low", "field": "thesis", "problem": "x", "fix": "y"}],
               "publish_blockers": []}
    telemetry = {"model": "claude-haiku-4-5-20251001", "input_tokens": 11277, "output_tokens": 1871}
    stdout = json.dumps({**verdict, "_telemetry": telemetry}, ensure_ascii=False, indent=1)
    parsed = RD._parse_last_json(stdout)
    assert parsed.get("decision") == "revise"
    assert parsed.get("_telemetry", {}).get("model") == "claude-haiku-4-5-20251001"


def test_parse_last_json_single_line_writer_summary():
    summary = json.dumps({"ok": True, "out": "x.json", "ticker": "BTC",
                          "model": "claude-sonnet-4-6", "input_tokens": 5, "output_tokens": 9})
    assert RD._parse_last_json(summary).get("ticker") == "BTC"


def test_parse_last_json_ignores_leading_log_lines():
    text = "INFO starting\nWARN something {not json}\n" + json.dumps({"decision": "approve",
                                                                      "summary": "ok"})
    assert RD._parse_last_json(text).get("decision") == "approve"


def test_parse_last_json_empty():
    assert RD._parse_last_json("") == {}
    assert RD._parse_last_json("no json here") == {}


def _asset(tk, cls="crypto"):
    return {"id": tk.lower(), "ticker": tk, "asset_class": cls, "session_profile": "crypto_247",
            "provider_symbols": {"yahoo": f"{tk}-USD"}, "cadence": "daily"}


def _prep_ok(asset, now, as_of, rec, stage):
    rec["stages"]["intraday"] = "ok"
    rec["stages"]["memory_pack"] = "ok"
    return True


def _finish_generated(asset, now, no_render, as_of, rec, stage):
    rec["status"] = "generated"
    rec["report_id"] = f"AF-20260625-{asset['ticker']}"
    return rec


def _common(monkeypatch, tmp_path):
    """Stub the data prep, file IO and finish stages so only the batch orchestration is exercised."""
    monkeypatch.setattr(RD, "BRIEF_DIR", tmp_path)
    monkeypatch.setattr(RD, "_data_prep", _prep_ok)
    monkeypatch.setattr(RD, "_read_json", lambda p: {"stub": True})   # analysis + memory_pack present
    monkeypatch.setattr(RD, "_finish_asset", _finish_generated)
    monkeypatch.setattr(RD, "_stamp_authored_brief", lambda *a, **k: None)


def test_batched_happy_path(monkeypatch, tmp_path):
    _common(monkeypatch, tmp_path)
    monkeypatch.setattr(RD.brief_batch, "author_briefs", lambda items, **kw: {
        it["ticker"]: {"brief": {"name": it["ticker"]}, "telemetry": {"batch": True}, "error": None}
        for it in items})
    monkeypatch.setattr(RD.brief_batch, "review_briefs", lambda items, **kw: {
        it["ticker"]: {"decision": "approve", "summary": "ok", "_telemetry": {"batch": True}}
        for it in items})

    jobs = RD.generate_due_batched([_asset("BTC"), _asset("GOLD")], NOW, no_render=True,
                                   as_of=None, workers=1)
    by = {j["ticker"]: j for j in jobs}
    assert by["BTC"]["status"] == "generated"
    assert by["GOLD"]["status"] == "generated"
    assert by["BTC"]["brief_source"] == "authored"
    assert by["BTC"]["critic_decision"] == "approve"
    # each authored brief was written to BRIEF_DIR for the scaffold to read
    assert (tmp_path / "BTC_research_brief.json").exists()


def test_batched_reject_skips_finish(monkeypatch, tmp_path):
    _common(monkeypatch, tmp_path)
    monkeypatch.setattr(RD.brief_batch, "author_briefs", lambda items, **kw: {
        it["ticker"]: {"brief": {"name": it["ticker"]}, "telemetry": {}, "error": None}
        for it in items})
    monkeypatch.setattr(RD.brief_batch, "review_briefs", lambda items, **kw: {
        it["ticker"]: {"decision": "reject", "summary": "fabricated level", "_telemetry": {}}
        for it in items})
    # finish must NOT run for a rejected brief
    monkeypatch.setattr(RD, "_finish_asset", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("finish should not run on reject")))

    jobs = RD.generate_due_batched([_asset("BTC")], NOW, no_render=True, as_of=None, workers=1)
    assert jobs[0]["status"] == "brief_rejected"
    assert not (tmp_path / "BTC_research_brief.json").exists()   # rejected draft removed


def test_batched_author_failure_needs_brief(monkeypatch, tmp_path):
    _common(monkeypatch, tmp_path)
    monkeypatch.setattr(RD.brief_batch, "author_briefs", lambda items, **kw: {
        it["ticker"]: {"brief": None, "telemetry": {}, "error": "rate_limit"} for it in items})
    monkeypatch.setattr(RD.brief_batch, "review_briefs",
                        lambda items, **kw: (_ for _ in ()).throw(
                            AssertionError("review should not run when nothing authored")))

    jobs = RD.generate_due_batched([_asset("BTC")], NOW, no_render=True, as_of=None, workers=1)
    assert jobs[0]["status"] == "needs_brief"
    assert "rate_limit" in jobs[0]["critic_summary"]


def test_batched_critic_failure_degrades(monkeypatch, tmp_path):
    _common(monkeypatch, tmp_path)
    monkeypatch.setattr(RD.brief_batch, "author_briefs", lambda items, **kw: {
        it["ticker"]: {"brief": {"name": it["ticker"]}, "telemetry": {}, "error": None}
        for it in items})
    # a whole-batch critic failure must degrade to needs_brief, not publish unreviewed.
    monkeypatch.setattr(RD.brief_batch, "review_briefs",
                        lambda items, **kw: (_ for _ in ()).throw(RuntimeError("critic boom")))
    monkeypatch.setattr(RD, "_finish_asset", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("finish should not run when critic failed")))

    jobs = RD.generate_due_batched([_asset("BTC")], NOW, no_render=True, as_of=None, workers=1)
    assert jobs[0]["status"] == "needs_brief"


def test_generate_due_falls_back_on_batch_exception(monkeypatch):
    monkeypatch.setattr(RD, "BRIEF_BATCH", True)
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", True)
    monkeypatch.setattr(RD, "generate_due_batched",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("batch down")))
    called = {"sync": 0}

    def _sync(asset, now, no_render, as_of):
        called["sync"] += 1
        return {"asset_id": asset["id"], "ticker": asset["ticker"], "status": "generated",
                "duration_s": 0.1}
    monkeypatch.setattr(RD, "generate_asset", _sync)

    jobs = RD._generate_due([_asset("BTC"), _asset("GOLD")], NOW, True, None, 1)
    assert called["sync"] == 2                       # fell back to per-asset sync authoring
    assert all(j["status"] == "generated" for j in jobs)


def test_generate_due_falls_back_on_no_clean_outcome(monkeypatch):
    # batch returns ONLY needs_brief (signature of a broken parse) -> fall back to sync.
    monkeypatch.setattr(RD, "BRIEF_BATCH", True)
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", True)
    monkeypatch.setattr(RD, "generate_due_batched", lambda *a, **k: [
        {"asset_id": "btc", "ticker": "BTC", "status": "needs_brief", "duration_s": 0.1}])
    called = {"sync": 0}
    monkeypatch.setattr(RD, "generate_asset", lambda *a, **k: (
        called.__setitem__("sync", called["sync"] + 1),
        {"asset_id": "btc", "ticker": "BTC", "status": "generated", "duration_s": 0.1})[1])

    jobs = RD._generate_due([_asset("BTC")], NOW, True, None, 1)
    assert called["sync"] == 1
    assert jobs[0]["status"] == "generated"


def test_generate_due_keeps_batch_on_clean_outcome(monkeypatch):
    # a genuinely quiet day (stand_aside present) is NOT a failure -> keep the batch result.
    monkeypatch.setattr(RD, "BRIEF_BATCH", True)
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", True)
    monkeypatch.setattr(RD, "generate_due_batched", lambda *a, **k: [
        {"asset_id": "btc", "ticker": "BTC", "status": "brief_stand_aside", "duration_s": 0.1}])
    monkeypatch.setattr(RD, "generate_asset", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not fall back on a clean outcome")))

    jobs = RD._generate_due([_asset("BTC")], NOW, True, None, 1)
    assert jobs[0]["status"] == "brief_stand_aside"
