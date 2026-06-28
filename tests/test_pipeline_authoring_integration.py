"""Integration tests for scripts/pipeline/authoring/* — the REAL modules of the authoring
directory WIRED TOGETHER over the committed fixtures, with the ONLY fake being the injected
Anthropic SDK client (canned responses; NO network / NO Anthropic API).

Phase 1 (test_pipeline_authoring_unit.py) already unit-tests each helper in isolation. This file
exercises the CROSS-MODULE FLOW + the data contracts BETWEEN modules:

  author flow   : AnthropicBriefClient.author -> brief_writer.build_user_message ->
                  summarize_analysis/summarize_research/summarize_social + _news_settings +
                  SYSTEM_PROMPT + _schema_doc  -> (fake SDK create) -> brief_writer._extract_json
                  -> brief_schema.validate_brief  (the writer re-exports the schema validator).
  critique flow : AnthropicBriefClient.critique -> critic.build_user_message ->
                  summarize_analysis -> (fake SDK create) -> critic._extract_json ->
                  critic._verdict_errors -> coherence guard.
  end-to-end    : author the REAL brief, then feed THAT authored brief straight into the critic;
                  and the critic-verdict -> re-author-with-guidance revise loop.
  module seam   : brief_writer.author_brief / critic.review_brief through the monkeypatched
                  _client/_require_sdk DI seam (the anthropic SDK is NOT installed in this env).

Real fixtures: tests/test_fixtures/BTC_research_brief.json + BTC_analysis.json.

Run:  python -m pytest tests/test_pipeline_authoring_integration.py -q
"""
import copy
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brief_schema as BS
import anthropic_client as AC
import brief_writer as BW
import critic as CR

SN = types.SimpleNamespace
FIX = Path(__file__).resolve().parent / "test_fixtures"
REAL_BRIEF = json.loads((FIX / "BTC_research_brief.json").read_text(encoding="utf-8-sig"))
REAL_ANALYSIS = json.loads((FIX / "BTC_analysis.json").read_text(encoding="utf-8-sig"))
REAL_BRIEF_TEXT = json.dumps(REAL_BRIEF)


# --------------------------------------------------------------------- fakes / fixtures

class _FakeMessages:
    """Records every create(**kw) and replays canned responses in order — the ONLY external
    boundary we fake. Mirrors the existing tests' style so the wiring is identical."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def _fake_client(responses):
    fake = _FakeMessages(responses)
    return SN(messages=fake), fake


def _resp(text, *, stop_reason="end_turn", in_tok=120, out_tok=240, web=0,
          cache_read=0, cache_write=0):
    srv = SN(web_search_requests=web) if web else None
    usage = SN(input_tokens=in_tok, output_tokens=out_tok, server_tool_use=srv,
               cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write)
    return SN(content=[SN(type="text", text=text)], usage=usage, stop_reason=stop_reason)


@pytest.fixture(autouse=True)
def _deterministic_env(monkeypatch):
    # _load_market_weather() reads data/market_weather.json from the real repo ROOT; the sandbox
    # guard is the REAL look-ahead short-circuit -> {} (no real-data read, fully deterministic).
    monkeypatch.setenv("ASSETFRAME_SANDBOX", "1")
    # web_search budget is env-tunable; pin it to the documented default of 6 for the assertions.
    monkeypatch.delenv("ASSETFRAME_BRIEF_WEB_MAX_USES", raising=False)


def _seam(monkeypatch, module, responses):
    """Wire a module's REAL _client/_require_sdk DI seam to a fake SDK client (the anthropic SDK is
    not installed here, so _require_sdk() would otherwise exit 3). Returns the recorder."""
    client, fake = _fake_client(responses)
    monkeypatch.setattr(module, "_require_sdk", lambda: object())
    monkeypatch.setattr(module, "_client", lambda _sdk: client)
    return fake


# =====================================================================
# 1. The real brief survives a full author() round-trip (writer.validate_brief <- brief_schema)
# =====================================================================

def test_author_roundtrips_the_real_brief_over_the_real_analysis():
    # The canned model output IS the committed real brief; with the REAL analysis as context the
    # whole author flow (build_user_message -> summarize_analysis -> _extract_json -> validate_brief)
    # must accept it on the first attempt and hand back an object equal to the fixture.
    client, fake = _fake_client([_resp(REAL_BRIEF_TEXT, in_tok=900, out_tok=7000)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, tele = abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None)

    assert brief == REAL_BRIEF                       # round-trips byte-for-value through the flow
    assert BS.validate_brief(brief) == []            # and is still schema-clean (cross-module)
    assert tele["attempts"] == 1
    assert tele["input_tokens"] == 900 and tele["output_tokens"] == 7000
    assert isinstance(tele["est_cost_usd"], float) and tele["est_cost_usd"] > 0
    assert tele["model"] == "claude-sonnet-4-6"
    assert len(fake.calls) == 1


# =====================================================================
# 2. The REAL analysis is compacted by summarize_analysis and reaches the wire request
# =====================================================================

def test_real_analysis_compaction_reaches_the_author_request():
    client, fake = _fake_client([_resp(REAL_BRIEF_TEXT)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None, include_news=True)

    kw = fake.calls[0]
    user = kw["messages"][0]["content"]
    # summarize_analysis fields (the contract build_user_message depends on):
    assert "last_price_DO_NOT_AUTHOR" in user        # price is context-only, never authored
    assert "aligned-down" in user                    # trend block carried verbatim from analysis
    assert "64868.38" in user                         # pivots_classic.PP rounded(2) reached the wire
    assert "levels_context_only_never_author" in user
    # the schema doc + system rules are wired in too
    assert "THE BRIEF SCHEMA" in user
    assert "breakout" in user                         # PREDICTION_TYPES enum spelled into the schema
    sys_text = kw["system"][0]["text"]
    assert sys_text.startswith(BW.SYSTEM_PROMPT[:60])
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    # news-on -> the full web_search budget (default 6) and the live web_search tool
    assert kw["tools"][0]["max_uses"] == 6
    assert kw["tools"][0]["type"] == "web_search_20250305"


# =====================================================================
# 3. _news_settings -> author wiring: news-off trims the budget AND appends the directive
# =====================================================================

def test_news_off_trims_budget_and_appends_directive():
    client, fake = _fake_client([_resp(REAL_BRIEF_TEXT)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None, include_news=False)

    kw = fake.calls[0]
    assert kw["tools"][0]["max_uses"] == 2            # technical-focus budget
    assert BW.NEWS_OFF_DIRECTIVE.strip()[:40] in kw["system"][0]["text"]


# =====================================================================
# 4. research + social packs flow through their summarizers into the request
# =====================================================================

def test_research_and_social_packs_compact_into_the_request():
    research = {"instrument": "BTC", "generated_at_utc": "2026-06-18T13:00:00Z",
                "items": [{"category": "macro", "headline": "FOMC holds rates",
                           "summary": "x" * 800, "source_url": "https://example.com/fomc",
                           "timestamp": "2026-06-17T18:00:00Z", "source_quality": "high",
                           "used_in_thesis": True}],
                "source_gaps": ["Deribit IV not sourced"]}
    social = {"aggregate": {"sentiment": "bearish", "dominant_themes": ["fear"],
                            "crowding_risk": "low", "hype_risk": "low",
                            "contrarian_warning": "none", "secret": "DO_NOT_LEAK"}}
    client, fake = _fake_client([_resp(REAL_BRIEF_TEXT)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, research, social)

    user = fake.calls[0]["messages"][0]["content"]
    assert "FOMC holds rates" in user                 # research headline carried
    assert "https://example.com/fomc" in user         # source_url mapped through
    assert "Deribit IV not sourced" in user           # source_gaps carried
    assert ("x" * 600) in user and ("x" * 601) not in user   # summary capped at 600 (cross-module)
    assert "MARKET CONVERSATION ONLY" in user         # social compaction note present
    assert "DO_NOT_LEAK" not in user                  # only whitelisted aggregate keys survive


# =====================================================================
# 5. author repair loop is wired to the REAL schema validator's error strings
# =====================================================================

def test_author_repair_feeds_back_real_schema_error_then_recovers():
    bad = copy.deepcopy(REAL_BRIEF)
    bad["directional_view"] = "up"                    # invalid DIRECTIONS enum -> brief_schema flags it
    client, fake = _fake_client([
        _resp(json.dumps(bad), stop_reason="end_turn"),     # attempt 1: schema miss
        _resp(REAL_BRIEF_TEXT, stop_reason="end_turn"),     # attempt 2: clean real brief
    ])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, tele = abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None)

    assert brief == REAL_BRIEF
    assert tele["attempts"] == 2
    assert len(fake.calls) == 2
    repair_msg = fake.calls[1]["messages"][-1]["content"]
    assert "failed schema validation" in repair_msg
    # the ACTUAL brief_schema.validate_brief message (not a generic stub) was fed back to the model
    assert "directional_view" in repair_msg
    assert "'up'" in repair_msg or "up" in repair_msg


# =====================================================================
# 6. critique() over the REAL brief + REAL analysis (build_user_message <- summarize_analysis)
# =====================================================================

def test_critique_reviews_real_brief_against_real_analysis():
    verdict_json = json.dumps({
        "decision": "approve", "summary": "Coherent, honestly-hedged bearish-lean research view",
        "issues": [], "confidence_adjustments": [], "publish_blockers": [],
        "stand_aside_reason": ""})
    client, fake = _fake_client([_resp(verdict_json, in_tok=2000, out_tok=300)])
    abc = AC.AnthropicBriefClient(client, "claude-haiku-4-5", default_max_tokens=8000)
    verdict, tele = abc.critique("BTC", REAL_BRIEF, REAL_ANALYSIS, None)

    assert verdict["decision"] == "approve"
    assert CR._verdict_errors(verdict) == []           # the parsed verdict is itself well-formed
    kw = fake.calls[0]
    assert "tools" not in kw                            # critic runs WITHOUT the web_search tool
    user = kw["messages"][0]["content"]
    assert "brief_under_review" in user
    assert "Bitcoin / US Dollar" in user               # the real brief was embedded for review
    assert "last_price_DO_NOT_AUTHOR" in user          # summarize_analysis context wired in
    # critic costed at its own (Haiku) model rate, not the writer's Sonnet rate
    assert tele["est_cost_usd"] == pytest.approx(AC.AnthropicBriefClient(None, "claude-haiku-4-5")
                                                 .cost(2000, 300))


def test_critique_without_analysis_emits_the_judge_on_brief_alone_note():
    verdict_json = json.dumps({"decision": "revise", "summary": "tighten claim gating",
                               "issues": [{"severity": "minor", "field": "claims[0]",
                                           "problem": "p", "fix": "f"}]})
    client, fake = _fake_client([_resp(verdict_json)])
    abc = AC.AnthropicBriefClient(client, "claude-haiku-4-5", default_max_tokens=8000)
    verdict, _ = abc.critique("BTC", REAL_BRIEF, None, None)
    assert verdict["decision"] == "revise"
    user = fake.calls[0]["messages"][0]["content"]
    assert "not supplied" in user                      # both analysis + research absent -> the fallbacks


# =====================================================================
# 7. critique coherence guard fires on a REAL-brief review (approve + blockers -> revise)
# =====================================================================

def test_critique_downgrades_approve_with_blockers_on_real_brief():
    verdict_json = json.dumps({"decision": "approve", "summary": "looks fine",
                               "issues": [], "publish_blockers": ["a price was authored in verdict"]})
    client, _ = _fake_client([_resp(verdict_json)])
    abc = AC.AnthropicBriefClient(client, "claude-haiku-4-5", default_max_tokens=8000)
    verdict, _ = abc.critique("BTC", REAL_BRIEF, REAL_ANALYSIS, None)
    assert verdict["decision"] == "revise"             # contradictory approve was downgraded
    assert "downgraded" in verdict["summary"]


# =====================================================================
# 8. END-TO-END: author the real brief, then critique THAT authored brief
# =====================================================================

def test_authored_brief_feeds_straight_into_the_critic():
    a_client, _ = _fake_client([_resp(REAL_BRIEF_TEXT, in_tok=900, out_tok=7000)])
    authored, _ = AC.AnthropicBriefClient(a_client, "claude-sonnet-4-6",
                                          default_max_tokens=20000).author(
        "BTC", REAL_ANALYSIS, {"lessons": []}, None, None)

    verdict_json = json.dumps({"decision": "approve", "summary": "ok", "issues": []})
    c_client, c_fake = _fake_client([_resp(verdict_json)])
    verdict, _ = AC.AnthropicBriefClient(c_client, "claude-haiku-4-5",
                                         default_max_tokens=8000).critique(
        "BTC", authored, REAL_ANALYSIS, None)

    assert verdict["decision"] == "approve"
    # the EXACT object the author produced is what the critic reviewed (no drift between stages)
    critic_user = c_fake.calls[0]["messages"][0]["content"]
    assert authored["exec_summary"][:40] in critic_user
    assert authored["instrument"] in critic_user


# =====================================================================
# 9. REVISE LOOP: a critic verdict's issues become author guidance on the re-author pass
# =====================================================================

def test_critic_issues_become_guidance_on_reauthor():
    # critic verdict -> caller turns its issues into a guidance string -> author re-run with guidance.
    verdict = {"decision": "revise", "summary": "overstated a single-source claim",
               "issues": [{"severity": "major", "field": "claims[2]",
                           "problem": "single-source claim centres the thesis",
                           "fix": "downgrade used_in_thesis or add a second source"}]}
    guidance = verdict["summary"] + "; " + "; ".join(
        f"{i['field']}: {i['problem']} -> {i['fix']}" for i in verdict["issues"])

    client, fake = _fake_client([_resp(REAL_BRIEF_TEXT)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, _ = abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None, guidance=guidance)

    assert brief == REAL_BRIEF
    user = fake.calls[0]["messages"][0]["content"]
    assert "REVISION GUIDANCE" in user                 # build_user_message wired the guidance block
    assert "single-source claim centres the thesis" in user


# =====================================================================
# 10. The MODULE SEAM: brief_writer.author_brief / critic.review_brief through _client/_require_sdk
# =====================================================================

def test_author_brief_module_function_through_the_di_seam(monkeypatch):
    fake = _seam(monkeypatch, BW, [_resp(REAL_BRIEF_TEXT, in_tok=800, out_tok=6500, web=2)])
    brief, tele = BW.author_brief("BTC", REAL_ANALYSIS, {"lessons": []}, None, None,
                                  model="claude-sonnet-4-6", max_tokens=20000)
    assert brief == REAL_BRIEF
    assert tele["web_searches"] == 2                   # server-tool usage accumulated through the flow
    assert tele["attempts"] == 1
    assert len(fake.calls) == 1


def test_review_brief_module_function_through_the_di_seam(monkeypatch):
    verdict_json = json.dumps({"decision": "approve", "summary": "ok", "issues": []})
    fake = _seam(monkeypatch, CR, [_resp(verdict_json, in_tok=2100, out_tok=250)])
    verdict, tele = CR.review_brief("BTC", REAL_BRIEF, REAL_ANALYSIS, None,
                                    model="claude-haiku-4-5", max_tokens=8000)
    assert verdict["decision"] == "approve"
    assert tele["model"] == "claude-haiku-4-5"
    assert tele["input_tokens"] == 2100 and tele["output_tokens"] == 250
    # the critic embedded the compacted real analysis it was handed
    assert "last_price_DO_NOT_AUTHOR" in fake.calls[0]["messages"][0]["content"]


# =====================================================================
# 11. pause_turn resume drives the SAME wired flow to a valid real brief
# =====================================================================

def test_pause_turn_resume_then_real_brief():
    client, fake = _fake_client([
        _resp("(web_search tool turn, no JSON yet)", stop_reason="pause_turn",
              in_tok=300, out_tok=120, web=1),
        _resp(REAL_BRIEF_TEXT, stop_reason="end_turn", in_tok=200, out_tok=6000),
    ])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, tele = abc.author("BTC", REAL_ANALYSIS, {"lessons": []}, None, None)

    assert brief == REAL_BRIEF
    assert len(fake.calls) == 2                         # resumed once
    assert tele["attempts"] == 1                        # a resume is NOT a repair attempt
    assert tele["input_tokens"] == 500 and tele["output_tokens"] == 6120
    assert tele["web_searches"] == 1
    # the resume re-sent the conversation with the model's partial content appended
    assert any(m.get("role") == "assistant" for m in fake.calls[1]["messages"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
