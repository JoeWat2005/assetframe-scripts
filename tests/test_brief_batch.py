"""Tests for brief_batch.py — the Anthropic Message-Batches author/critique orchestrator.

No live API: a FakeBatches stub stands in for client.messages.batches (create/retrieve/results/
cancel). The stub lets each test script per-request outcomes (valid brief, schema-miss, errored,
expired) and round-specific behaviour (so the repair round can be exercised). The real parsing
(brief_writer._extract_json + validate_brief, critic._extract_json + _verdict_errors) runs for real
against the committed BTC fixtures, so a drift in the schema contract fails here too.

Run:  python -m pytest tests/test_brief_batch.py -q
"""
import json
import os
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brief_batch as BB
import brief_writer as BW

FIX = Path(__file__).resolve().parent / "test_fixtures"
VALID_BRIEF = json.loads((FIX / "BTC_research_brief.json").read_text(encoding="utf-8-sig"))
ANALYSIS = json.loads((FIX / "BTC_analysis.json").read_text(encoding="utf-8-sig"))


def _usage(in_tok=120, out_tok=300, web=2, cache_read=0, cache_write=0):
    srv = types.SimpleNamespace(web_search_requests=web) if web else None
    return types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok,
                                 cache_read_input_tokens=cache_read,
                                 cache_creation_input_tokens=cache_write, server_tool_use=srv)


def _msg(text, usage=None):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                 usage=usage or _usage())


def _result(custom_id, *, text=None, rtype="succeeded", error_msg=None):
    if rtype == "succeeded":
        res = types.SimpleNamespace(type="succeeded", message=_msg(text), error=None)
    else:
        err = types.SimpleNamespace(error=types.SimpleNamespace(message=error_msg or rtype))
        res = types.SimpleNamespace(type=rtype, message=None, error=err)
    return types.SimpleNamespace(custom_id=custom_id, result=res)


class FakeBatches:
    """Stand-in for client.messages.batches. `responder(requests, round_idx)` returns a list of
    result objects; `ended` controls whether retrieve reports completion (for the timeout test)."""

    def __init__(self, responder, *, ended=True):
        self.responder = responder
        self.ended = ended
        self.round = 0
        self.created = []           # captured request lists, per round
        self.canceled = []
        self._store = {}

    def create(self, requests):
        self.round += 1
        self.created.append(list(requests))
        bid = f"batch_{self.round}"
        self._store[bid] = self.responder(requests, self.round)
        return types.SimpleNamespace(id=bid)

    def retrieve(self, bid):
        return types.SimpleNamespace(
            processing_status="ended" if self.ended else "in_progress")

    def results(self, bid):
        return iter(self._store.get(bid, []))

    def cancel(self, bid):
        self.canceled.append(bid)


def _install(monkeypatch, responder, *, ended=True):
    fake = FakeBatches(responder, ended=ended)
    client = types.SimpleNamespace(messages=types.SimpleNamespace(batches=fake))
    monkeypatch.setattr(BB.bw, "_require_sdk", lambda: object())
    monkeypatch.setattr(BB.bw, "_client", lambda _sdk: client)
    return fake


def _author_item(ticker):
    return {"ticker": ticker, "analysis": ANALYSIS, "memory_pack": {"x": 1},
            "research": None, "social": None, "include_news": True}


# --------------------------------------------------------------------- authoring

def test_author_happy_path(monkeypatch):
    def responder(requests, rnd):
        return [_result(r["custom_id"], text=json.dumps(VALID_BRIEF)) for r in requests]
    fake = _install(monkeypatch, responder)

    out = BB.author_briefs([_author_item("BTC"), _author_item("GOLD")],
                           model="m", max_tokens=100, poll_interval=1, deadline=time.time() + 10)
    assert set(out) == {"BTC", "GOLD"}
    for tk in ("BTC", "GOLD"):
        assert out[tk]["brief"] is not None
        assert out[tk]["error"] is None
        assert out[tk]["telemetry"]["batch"] is True
    assert fake.round == 1                       # no repair round needed


def test_author_schema_miss_then_repair_succeeds(monkeypatch):
    # round 1: BTC returns a parseable-but-invalid object; round 2 (repair) returns a valid brief.
    def responder(requests, rnd):
        results = []
        for r in requests:
            if rnd == 1:
                results.append(_result(r["custom_id"], text=json.dumps({"name": "BTC"})))
            else:
                results.append(_result(r["custom_id"], text=json.dumps(VALID_BRIEF)))
        return results
    fake = _install(monkeypatch, responder)

    out = BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert fake.round == 2                       # a repair batch was submitted
    assert out["BTC"]["brief"] is not None
    assert out["BTC"]["error"] is None
    # telemetry sums across both rounds
    assert out["BTC"]["telemetry"]["output_tokens"] == 600
    # the repair request carried the validation errors as guidance
    repair_user = fake.created[1][0]["params"]["messages"][0]["content"]
    assert "failed schema validation" in repair_user


def test_author_schema_miss_persists(monkeypatch):
    def responder(requests, rnd):
        return [_result(r["custom_id"], text=json.dumps({"name": "BTC"})) for r in requests]
    _install(monkeypatch, responder)

    out = BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"]["brief"] is None
    assert out["BTC"]["error"]                    # carries the validation errors


def test_author_per_request_error(monkeypatch):
    def responder(requests, rnd):
        return [_result(r["custom_id"], rtype="errored", error_msg="overloaded")
                for r in requests]
    _install(monkeypatch, responder)

    out = BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"]["brief"] is None
    assert "overloaded" in out["BTC"]["error"]


def test_author_uses_web_search_and_cache(monkeypatch):
    fake = _install(monkeypatch, lambda reqs, rnd:
                    [_result(r["custom_id"], text=json.dumps(VALID_BRIEF)) for r in reqs])
    BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100, poll_interval=1, deadline=time.time() + 10)
    params = fake.created[0][0]["params"]
    assert params["tools"][0]["name"] == "web_search"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_custom_id_sanitised_and_mapped(monkeypatch):
    # a ticker with a non-alnum char must still round-trip to the right result.
    fake = _install(monkeypatch, lambda reqs, rnd:
                    [_result(r["custom_id"], text=json.dumps(VALID_BRIEF)) for r in reqs])
    out = BB.author_briefs([_author_item("XAU/USD")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["XAU/USD"]["brief"] is not None
    cid = fake.created[0][0]["custom_id"]
    assert "/" not in cid and __import__("re").match(r"^[A-Za-z0-9_-]{1,64}$", cid)


def test_submission_error_propagates(monkeypatch):
    # a create() failure must raise so run_daily falls back to the synchronous path.
    client = types.SimpleNamespace(messages=types.SimpleNamespace(
        batches=types.SimpleNamespace(create=lambda requests: (_ for _ in ()).throw(RuntimeError("boom")))))
    monkeypatch.setattr(BB.bw, "_require_sdk", lambda: object())
    monkeypatch.setattr(BB.bw, "_client", lambda _sdk: client)
    try:
        BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100, poll_interval=1, deadline=time.time() + 10)
        assert False, "expected RuntimeError to propagate"
    except RuntimeError as ex:
        assert "boom" in str(ex)


def test_timeout_raises_and_cancels(monkeypatch):
    fake = _install(monkeypatch, lambda reqs, rnd:
                    [_result(r["custom_id"], text=json.dumps(VALID_BRIEF)) for r in reqs],
                    ended=False)
    try:
        # deadline already in the past -> first retrieve (in_progress) trips the budget immediately
        BB.author_briefs([_author_item("BTC")], model="m", max_tokens=100,
                         poll_interval=1, deadline=time.time() - 1)
        assert False, "expected BatchTimeout"
    except BB.BatchTimeout:
        pass
    assert fake.canceled                          # the stuck batch was canceled


# --------------------------------------------------------------------- critique

def _critic_item(ticker):
    return {"ticker": ticker, "brief": VALID_BRIEF, "analysis": ANALYSIS, "research": None}


def test_review_valid_verdict(monkeypatch):
    verdict = {"decision": "approve", "summary": "clean", "issues": []}
    _install(monkeypatch, lambda reqs, rnd:
             [_result(r["custom_id"], text=json.dumps(verdict)) for r in reqs])
    out = BB.review_briefs([_critic_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"]["decision"] == "approve"
    assert out["BTC"]["_telemetry"]["batch"] is True


def test_review_approve_with_blockers_downgrades(monkeypatch):
    verdict = {"decision": "approve", "summary": "ok", "publish_blockers": ["missing price"]}
    _install(monkeypatch, lambda reqs, rnd:
             [_result(r["custom_id"], text=json.dumps(verdict)) for r in reqs])
    out = BB.review_briefs([_critic_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"]["decision"] == "revise"


def test_review_truncated_then_repair_recovers(monkeypatch):
    # round 1 returns a malformed verdict (truncated); the repair round returns a valid one.
    valid = {"decision": "approve", "summary": "clean", "issues": []}
    fake = _install(monkeypatch, lambda reqs, rnd:
                    [_result(r["custom_id"],
                             text=('{"decision": "approve", "issues": [{"x":' if rnd == 1
                                   else json.dumps(valid))) for r in reqs])
    out = BB.review_briefs([_critic_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert fake.round == 2                       # a critic repair batch was submitted
    assert out["BTC"]["decision"] == "approve"   # recovered instead of dropping to needs_brief
    # the repair request carried the compact-JSON directive
    repair_user = fake.created[1][0]["params"]["messages"][0]["content"]
    assert "compact JSON verdict" in repair_user


def test_review_malformed_is_none(monkeypatch):
    _install(monkeypatch, lambda reqs, rnd:
             [_result(r["custom_id"], text=json.dumps({"decision": "maybe"})) for r in reqs])
    out = BB.review_briefs([_critic_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"] is None


def test_review_errored_is_none(monkeypatch):
    _install(monkeypatch, lambda reqs, rnd:
             [_result(r["custom_id"], rtype="expired") for r in reqs])
    out = BB.review_briefs([_critic_item("BTC")], model="m", max_tokens=100,
                           poll_interval=1, deadline=time.time() + 10)
    assert out["BTC"] is None


if __name__ == "__main__":
    unittest.main()
