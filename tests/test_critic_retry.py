"""Tests for critic.review_brief's recovery retry — a verbose verdict that hits the output ceiling
(truncated JSON) or a malformed verdict must be re-prompted ONCE for a compact, complete object
rather than dropping the brief to needs_brief. No live API: critic._client is stubbed.

Run:  python -m pytest tests/test_critic_retry.py -q
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import critic as CR

VALID = json.dumps({"decision": "revise", "summary": "minor edits", "issues": []})
# truncated mid-object the way a max_tokens cut looks (no closing brace)
TRUNCATED = '{"decision": "revise", "summary": "x", "issues": [{"severity": "low",'


def _resp(text, stop_reason="end_turn", in_tok=1000, out_tok=500):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)],
        usage=types.SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
        stop_reason=stop_reason)


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


def _install(monkeypatch, responses):
    fake = _FakeMessages(responses)
    client = types.SimpleNamespace(messages=fake)
    monkeypatch.setattr(CR, "_require_sdk", lambda: object())
    monkeypatch.setattr(CR, "_client", lambda _sdk: client)
    return fake


def test_truncated_then_valid_recovers(monkeypatch):
    fake = _install(monkeypatch, [_resp(TRUNCATED, stop_reason="max_tokens", out_tok=3000),
                                  _resp(VALID, out_tok=400)])
    verdict, telemetry = CR.review_brief("BTC", {"x": 1}, None, None, model="m", max_tokens=3000)
    assert verdict["decision"] == "revise"
    assert len(fake.calls) == 2                       # it retried
    # retry bumped the budget after a max_tokens stop, and summed both calls' tokens
    assert fake.calls[1]["max_tokens"] >= 8000
    assert telemetry["output_tokens"] == 3400


def test_valid_first_try_no_retry(monkeypatch):
    fake = _install(monkeypatch, [_resp(VALID)])
    verdict, _ = CR.review_brief("BTC", {"x": 1}, None, None, model="m", max_tokens=8000)
    assert verdict["decision"] == "revise"
    assert len(fake.calls) == 1                       # no retry needed


def test_malformed_twice_exits(monkeypatch):
    _install(monkeypatch, [_resp(TRUNCATED), _resp(TRUNCATED)])
    with pytest.raises(SystemExit) as ei:
        CR.review_brief("BTC", {"x": 1}, None, None, model="m", max_tokens=8000)
    assert ei.value.code == 3


def test_approve_with_blockers_downgrades(monkeypatch):
    v = json.dumps({"decision": "approve", "summary": "ok", "publish_blockers": ["no price"]})
    _install(monkeypatch, [_resp(v)])
    verdict, _ = CR.review_brief("BTC", {"x": 1}, None, None, model="m", max_tokens=8000)
    assert verdict["decision"] == "revise"            # downgraded
