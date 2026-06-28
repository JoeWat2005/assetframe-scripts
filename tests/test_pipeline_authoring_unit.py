"""Offline unit tests for scripts/pipeline/authoring/* — the AI brief author/critic surface.

Targets the GAPS the existing suite (test_brief_writer / test_critic_retry / test_brief_batch /
test_market_context / test_news_toggle) leaves uncovered after the second-level subgroup refactor:

  * brief_schema.validate_brief — the many branches the writer tests don't exercise
    (alternative_prediction, preferred_setup, verdict, catalysts, scenario_matrix, narrative
    non-empty-list rules, claim object/text/weak-gating, quality/asset_class_key/horizon enums).
  * anthropic_client — the cost() formula, usage() extraction edge cases, resolve_prices() edges,
    _create()'s API-error -> exit(3), and the author() flow (happy / pause_turn resume /
    max_tokens repair / validation-fail-twice) which is NOT exercised offline elsewhere.
  * critic — _verdict_errors, _extract_json, build_user_message, parse_args.
  * brief_writer — _load_json, summarize_research/summarize_social, _extract_json, _usage_line,
    _client (no-key + with-key).
  * brief_batch — _cid, _merge_tele, _err_str, _batch_timeout_s/_poll_s, _model_prices.

NO network / Anthropic / Neon / R2 / subprocess: the SDK is never built (the client is injected as a
fake), file inputs use tmp_path, and the only env reads are monkeypatched.

Run:  python -m pytest tests/test_pipeline_authoring_unit.py -q
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
import brief_batch as BB

SN = types.SimpleNamespace
FIX = Path(__file__).resolve().parent / "test_fixtures"
VALID_BRIEF = json.loads((FIX / "BTC_research_brief.json").read_text(encoding="utf-8-sig"))
VALID_BRIEF_TEXT = json.dumps(VALID_BRIEF)


def _base():
    """A deep copy of the committed valid brief, to mutate one field at a time."""
    return copy.deepcopy(VALID_BRIEF)


# =====================================================================
# brief_schema.validate_brief — uncovered branches + enum parity
# =====================================================================

def test_valid_base_brief_passes():
    assert BS.validate_brief(_base()) == []


def test_schema_enums_match_documented_vocab():
    # guards against a refactor silently dropping/reordering an enum the scaffold depends on
    assert BS.DIRECTIONS == ("bullish", "bearish", "neutral", "mixed")
    assert BS.SETUP_SIDES == ("long", "short", "wait")
    assert BS.HORIZONS == ("intraday", "next_session", "multi_session")
    assert BS.RISK_LEVELS == ("Low", "Medium", "High")
    assert set(BS.CLAIM_STATUSES) == {"confirmed", "multiple-source", "single-source",
                                      "unverified", "stale", "unavailable"}


def test_schema_reexported_through_brief_writer_is_same_object():
    # the refactor moved the contract to brief_schema and re-exports it via brief_writer; both
    # callers (critic/brief_batch reach validate_brief through brief_writer) must see ONE function
    assert BW.validate_brief is BS.validate_brief
    assert BW.CLAIM_STATUSES == BS.CLAIM_STATUSES


def test_non_dict_brief_short_circuits():
    assert BS.validate_brief(["x"]) == ["brief is not a JSON object"]
    assert BS.validate_brief(None) == ["brief is not a JSON object"]


def test_bad_asset_class_key_enum():
    b = _base(); b["asset_class_key"] = "stonks"
    assert any("asset_class_key" in e for e in BS.validate_brief(b))


def test_bad_horizon_enum():
    b = _base(); b["horizon"] = "weekly"
    assert any("horizon" in e for e in BS.validate_brief(b))


def test_bad_quality_enums():
    b = _base(); b["long_scenario_quality"] = "Amazing"
    assert any("long_scenario_quality" in e for e in BS.validate_brief(b))


def test_primary_prediction_type_missing():
    b = _base(); b["primary_prediction"].pop("type")
    assert any("primary_prediction.type" in e for e in BS.validate_brief(b))


def test_primary_prediction_missing_subfield():
    b = _base(); b["primary_prediction"].pop("reasoning")
    assert any("primary_prediction.reasoning" in e for e in BS.validate_brief(b))


def test_primary_prediction_invalidators_not_a_list():
    b = _base(); b["primary_prediction"]["invalidators"] = "nope"
    assert any("invalidators" in e for e in BS.validate_brief(b))


def test_alternative_prediction_bad_type():
    b = _base(); b["alternative_prediction"]["type"] = "momentum"
    assert any("alternative_prediction.type" in e for e in BS.validate_brief(b))


def test_alternative_prediction_missing_reasoning():
    b = _base(); b["alternative_prediction"].pop("reasoning")
    assert any("alternative_prediction.reasoning" in e for e in BS.validate_brief(b))


def test_alternative_prediction_type_optional_when_absent():
    # alternative_prediction.type is only enum-checked when present (ap.get('type') is truthy)
    b = _base(); b["alternative_prediction"].pop("type")
    errs = BS.validate_brief(b)
    assert not any("alternative_prediction.type" in e for e in errs)


def test_preferred_setup_bad_side():
    b = _base(); b["preferred_setup"]["side"] = "hold"
    assert any("preferred_setup.side" in e for e in BS.validate_brief(b))


def test_preferred_setup_missing_why():
    b = _base(); b["preferred_setup"].pop("why_this_setup")
    assert any("why_this_setup" in e for e in BS.validate_brief(b))


def test_verdict_missing_subfield():
    b = _base(); b["verdict"].pop("best")
    assert any("verdict.best" in e for e in BS.validate_brief(b))


def test_catalyst_missing_label():
    b = _base(); b["catalysts"] = [{"when": "x"}]
    assert any("catalysts[0]" in e for e in BS.validate_brief(b))


def test_scenario_matrix_missing_case():
    b = _base(); b["scenario_matrix"] = [{"trigger": "x"}]
    assert any("scenario_matrix[0]" in e for e in BS.validate_brief(b))


def test_narrative_free_bullets_must_be_nonempty():
    b = _base(); b["narrative"]["free_bullets"] = []
    assert any("free_bullets" in e for e in BS.validate_brief(b))


def test_narrative_market_summary_missing():
    b = _base(); b["narrative"].pop("market_summary")
    assert any("market_summary" in e for e in BS.validate_brief(b))


def test_narrative_missing_string_subfield():
    b = _base(); b["narrative"].pop("technicals_note")
    assert any("technicals_note" in e for e in BS.validate_brief(b))


def test_claim_not_an_object():
    b = _base(); b["claims"] = ["just a string"]
    assert any("claims[0] is not an object" in e for e in BS.validate_brief(b))


def test_claim_missing_text():
    b = _base(); b["claims"] = [{"status": "confirmed", "used_in_thesis": False}]
    assert any("missing 'claim' text" in e for e in BS.validate_brief(b))


def test_claim_stale_cannot_drive_thesis():
    b = _base()
    b["claims"] = [{"claim": "old", "status": "stale", "source": "https://s",
                    "used_in_thesis": True}]
    assert any("weak claims cannot drive the thesis" in e for e in BS.validate_brief(b))


def test_claim_unavailable_cannot_drive_thesis():
    b = _base()
    b["claims"] = [{"claim": "x", "status": "unavailable", "source": "https://s",
                    "used_in_thesis": True}]
    assert any("used_in_thesis" in e for e in BS.validate_brief(b))


def test_claim_status_is_case_insensitive():
    # status is lower()'d before the enum check, so an upper-case status is still valid
    b = _base()
    b["claims"] = [{"claim": "x", "status": "CONFIRMED", "source": "https://s",
                    "used_in_thesis": True}]
    errs = BS.validate_brief(b)
    assert not any("claims[0]" in e for e in errs)


def test_options_context_must_be_bool():
    b = _base(); b["options_context_included"] = "yes"
    assert any("options_context_included" in e for e in BS.validate_brief(b))


# =====================================================================
# anthropic_client.resolve_prices — edges
# =====================================================================

def test_resolve_prices_none_and_empty_model_fall_back_to_sonnet():
    assert AC.resolve_prices(None) == (3.0, 15.0)
    assert AC.resolve_prices("") == (3.0, 15.0)


def test_resolve_prices_opus_and_case_insensitive():
    assert AC.resolve_prices("claude-opus-4-8") == (15.0, 75.0)
    assert AC.resolve_prices("CLAUDE-HAIKU-4-5") == (1.0, 5.0)


def test_resolve_prices_unknown_uses_fallback_then_sonnet():
    assert AC.resolve_prices("mystery-model", (2.5, 9.0)) == (2.5, 9.0)
    assert AC.resolve_prices("mystery-model") == (3.0, 15.0)


def test_resolve_prices_explicit_override_beats_everything():
    assert AC.resolve_prices("claude-haiku-4-5", (2.5, 9.0), (7.0, 8.0)) == (7.0, 8.0)
    # override even wins for an unknown model
    assert AC.resolve_prices("mystery", None, (1.0, 1.0)) == (1.0, 1.0)


# =====================================================================
# anthropic_client.AnthropicBriefClient — cost / usage / _create
# =====================================================================

def _client_obj(responses):
    fake = _FakeMessages(responses)
    return SN(messages=fake), fake


class _FakeMessages:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._responses.pop(0)


def _resp(text, *, stop_reason="end_turn", in_tok=100, out_tok=200, web=0,
          cache_read=0, cache_write=0):
    srv = SN(web_search_requests=web) if web else None
    usage = SN(input_tokens=in_tok, output_tokens=out_tok, server_tool_use=srv,
               cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write)
    return SN(content=[SN(type="text", text=text)], usage=usage, stop_reason=stop_reason)


def test_cost_base_formula_sonnet():
    abc = AC.AnthropicBriefClient(None, "claude-sonnet-4-6")
    assert abc.cost(1_000_000, 0) == pytest.approx(3.0)
    assert abc.cost(0, 1_000_000) == pytest.approx(15.0)
    assert abc.cost(1_000_000, 1_000_000) == pytest.approx(18.0)


def test_cost_cache_read_and_write_multipliers():
    abc = AC.AnthropicBriefClient(None, "claude-sonnet-4-6")
    # cache reads bill at 0.1x of price_in, writes at 1.25x
    assert abc.cost(0, 0, cache_read=1_000_000) == pytest.approx(0.3)
    assert abc.cost(0, 0, cache_write=1_000_000) == pytest.approx(3.75)


def test_cost_batch_halves():
    abc = AC.AnthropicBriefClient(None, "claude-sonnet-4-6", batch=True)
    assert abc.cost(1_000_000, 0) == pytest.approx(1.5)


def test_usage_full_extraction():
    abc = AC.AnthropicBriefClient(None, "m")
    r = _resp("x", in_tok=10, out_tok=20, web=3, cache_read=4, cache_write=5)
    assert abc.usage(r) == {"input": 10, "output": 20, "web_search": 3,
                            "cache_read": 4, "cache_write": 5}


def test_usage_missing_usage_object_all_zero():
    abc = AC.AnthropicBriefClient(None, "m")
    r = SN(content=[], stop_reason="end_turn")          # no .usage attribute at all
    assert abc.usage(r) == {"input": 0, "output": 0, "web_search": 0,
                            "cache_read": 0, "cache_write": 0}


def test_usage_no_server_tool_use_means_zero_web():
    abc = AC.AnthropicBriefClient(None, "m")
    r = _resp("x", web=0)                                 # srv None when web falsy
    assert abc.usage(r)["web_search"] == 0


def test_usage_partial_usage_defaults_missing_to_zero():
    abc = AC.AnthropicBriefClient(None, "m")
    r = SN(usage=SN(input_tokens=7))                      # only input present
    u = abc.usage(r)
    assert u["input"] == 7 and u["output"] == 0 and u["cache_read"] == 0


def test_create_api_error_exits_3():
    class _Boom:
        def create(self, **kw):
            raise RuntimeError("network down")
    abc = AC.AnthropicBriefClient(SN(messages=_Boom()), "m")
    with pytest.raises(SystemExit) as ei:
        abc._create(model="m", max_tokens=1, messages=[])
    assert ei.value.code == 3


# =====================================================================
# anthropic_client.author — the flow (happy / pause_turn / repair / fail)
# =====================================================================

@pytest.fixture(autouse=True)
def _no_market_weather(monkeypatch):
    # author()/build_user_message would read data/market_weather.json; force {} for determinism
    monkeypatch.setattr(BW, "_load_market_weather", lambda: {})


def test_author_happy_path_returns_brief_and_telemetry():
    client, fake = _client_obj([_resp(VALID_BRIEF_TEXT, in_tok=100, out_tok=200)])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, tele = abc.author("BTC", {}, {}, None, None)
    assert brief["ticker"] == "BTC"
    assert tele["attempts"] == 1
    assert tele["input_tokens"] == 100 and tele["output_tokens"] == 200
    assert tele["model"] == "claude-sonnet-4-6"
    assert isinstance(tele["est_cost_usd"], float)
    assert len(fake.calls) == 1


def test_author_resumes_on_pause_turn():
    # a pause_turn carries no finished JSON; the flow re-sends the turn, then parses the next response
    client, fake = _client_obj([
        _resp("(partial tool turn)", stop_reason="pause_turn", in_tok=100, out_tok=200),
        _resp(VALID_BRIEF_TEXT, stop_reason="end_turn", in_tok=50, out_tok=60),
    ])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    brief, tele = abc.author("BTC", {}, {}, None, None)
    assert brief["ticker"] == "BTC"
    assert len(fake.calls) == 2                          # it resumed once
    assert tele["attempts"] == 1                         # resume is NOT a repair attempt
    assert tele["input_tokens"] == 150 and tele["output_tokens"] == 260


def test_author_reprompts_on_validation_miss_and_names_truncation():
    # attempt 1: a parseable-but-invalid object that hit max_tokens -> repair attempt with the
    # max_tokens cause named in the fed-back errors; attempt 2: a valid brief.
    client, fake = _client_obj([
        _resp(json.dumps({"name": "BTC"}), stop_reason="max_tokens"),
        _resp(VALID_BRIEF_TEXT, stop_reason="end_turn"),
    ])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=8000)
    brief, tele = abc.author("BTC", {}, {}, None, None)
    assert brief["ticker"] == "BTC"
    assert tele["attempts"] == 2
    last_user = fake.calls[1]["messages"][-1]["content"]
    assert "failed schema validation" in last_user
    assert "max_tokens" in last_user                     # the truncation cause was surfaced


def test_author_exits_2_after_two_invalid_attempts():
    client, _ = _client_obj([
        _resp(json.dumps({"name": "BTC"})),
        _resp(json.dumps({"name": "BTC"})),
    ])
    abc = AC.AnthropicBriefClient(client, "claude-sonnet-4-6", default_max_tokens=20000)
    with pytest.raises(SystemExit) as ei:
        abc.author("BTC", {}, {}, None, None)
    assert ei.value.code == 2


def test_critique_costs_at_model_rate():
    # the critic-mispricing fix: a Haiku critique is costed at Haiku, not Sonnet, rates
    client, _ = _client_obj([
        _resp(json.dumps({"decision": "approve", "summary": "ok", "issues": []}),
              in_tok=1_000_000, out_tok=0),
    ])
    abc = AC.AnthropicBriefClient(client, "claude-haiku-4-5", default_max_tokens=8000)
    verdict, tele = abc.critique("BTC", VALID_BRIEF, None, None)
    assert verdict["decision"] == "approve"
    assert tele["est_cost_usd"] == pytest.approx(1.0)    # 1M input @ Haiku $1/MTok, not $3 Sonnet


# =====================================================================
# critic — _verdict_errors / _extract_json / build_user_message / parse_args
# =====================================================================

def test_verdict_errors_non_dict():
    assert CR._verdict_errors(["x"]) == ["verdict is not a JSON object"]


def test_verdict_errors_bad_decision_and_missing_summary():
    errs = CR._verdict_errors({"decision": "maybe"})
    assert any("decision=" in e for e in errs)
    assert any("summary" in e for e in errs)


def test_verdict_errors_non_list_fields():
    errs = CR._verdict_errors({"decision": "approve", "summary": "s", "issues": "nope"})
    assert any("'issues' must be a list" in e for e in errs)


def test_verdict_errors_valid_is_empty():
    assert CR._verdict_errors({"decision": "approve", "summary": "s"}) == []


def test_critic_extract_json_no_text():
    assert CR._extract_json([SN(type="text", text="   ")]) == (None, "critic returned no text content")


def test_critic_extract_json_no_braces():
    obj, err = CR._extract_json([SN(type="text", text="prose only, no json")])
    assert obj is None and "no JSON object" in err


def test_critic_extract_json_malformed():
    obj, err = CR._extract_json([SN(type="text", text='{"decision": }')])
    assert obj is None and err.startswith("could not parse critic JSON")


def test_critic_extract_json_ignores_non_text_blocks():
    blocks = [SN(type="tool_use", text="ignore me"),
              SN(type="text", text='{"decision": "approve", "summary": "ok"}')]
    obj, err = CR._extract_json(blocks)
    assert err is None and obj["decision"] == "approve"


def test_critic_build_user_message_without_context():
    msg = CR.build_user_message("BTC", {"name": "BTC"}, None, None)
    assert "brief_under_review" in msg
    assert "not supplied" in msg                          # both analysis and research absent


def test_critic_build_user_message_with_analysis_compacts_it():
    analysis = {"symbol": "BTC", "last_price": 100, "hourly": {}, "daily": {}}
    msg = CR.build_user_message("BTC", {"name": "BTC"}, analysis, {"items": []})
    assert "last_price_DO_NOT_AUTHOR" in msg               # came through summarize_analysis
    assert "research_pack" in msg


def test_critic_parse_args_defaults_and_required_asset():
    args = CR.parse_args(["brief.json", "--asset", "BTC"])
    assert args.asset == "BTC"
    assert args.model == CR.DEFAULT_MODEL
    assert args.max_tokens == CR.DEFAULT_MAX_TOKENS
    with pytest.raises(SystemExit):                       # --asset is required
        CR.parse_args(["brief.json"])


def test_critic_decision_buckets_partition_decisions():
    assert set(CR.PASS_DECISIONS) | set(CR.FAIL_DECISIONS) == set(CR.DECISIONS)
    assert set(CR.PASS_DECISIONS) & set(CR.FAIL_DECISIONS) == set()


# =====================================================================
# brief_writer — _load_json / summarize_* / _extract_json / _usage_line / _client
# =====================================================================

def test_load_json_valid(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"k": 1}', encoding="utf-8")
    assert BW._load_json(str(p)) == {"k": 1}


def test_load_json_handles_utf8_bom(tmp_path):
    p = tmp_path / "bom.json"
    p.write_text('{"k": 2}', encoding="utf-8-sig")        # writes a BOM
    assert BW._load_json(str(p)) == {"k": 2}


def test_load_json_none_required_exits_2():
    with pytest.raises(SystemExit) as ei:
        BW._load_json(None, required=True, what="analysis")
    assert ei.value.code == 2


def test_load_json_none_optional_returns_none():
    assert BW._load_json(None, required=False) is None


def test_load_json_missing_required_exits_2(tmp_path):
    with pytest.raises(SystemExit) as ei:
        BW._load_json(str(tmp_path / "nope.json"), required=True, what="x")
    assert ei.value.code == 2


def test_load_json_missing_optional_returns_none(tmp_path):
    assert BW._load_json(str(tmp_path / "nope.json"), required=False) is None


def test_load_json_invalid_json_exits_2(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid", encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        BW._load_json(str(p))
    assert ei.value.code == 2


def test_summarize_research_none_returns_none():
    assert BW.summarize_research(None) is None


def test_summarize_research_maps_and_truncates():
    pack = {"instrument": "BTC", "generated_at_utc": "t",
            "items": [{"category": "macro", "headline": "H", "summary": "a" * 700,
                       "url": "http://u", "timestamp": "ts", "source_quality": "high",
                       "used_in_thesis": True}],
            "source_gaps": ["gap1"]}
    out = BW.summarize_research(pack)
    item = out["items"][0]
    assert len(item["summary"]) == 600                    # summary capped at 600 chars
    assert item["source_url"] == "http://u"               # falls back to 'url' when no 'source_url'
    assert out["source_gaps"] == ["gap1"]
    assert out["instrument"] == "BTC"


def test_summarize_research_handles_missing_summary():
    out = BW.summarize_research({"items": [{"headline": "H"}]})
    assert out["items"][0]["summary"] == ""               # None summary -> empty string, no crash


def test_summarize_social_none_returns_none():
    assert BW.summarize_social(None) is None


def test_summarize_social_picks_aggregate_keys_only():
    pack = {"aggregate": {"sentiment": "bearish", "dominant_themes": ["fear"],
                          "crowding_risk": "high", "hype_risk": "low",
                          "contrarian_warning": "w", "secret": "dropme"}}
    out = BW.summarize_social(pack)
    assert "MARKET CONVERSATION ONLY" in out["note"]
    assert out["aggregate"]["sentiment"] == "bearish"
    assert "secret" not in out["aggregate"]               # only the whitelisted keys survive


def test_writer_extract_json_happy_and_defensive_slice():
    obj, err = BW._extract_json([SN(type="text", text='garbage {"a": 1} trailing')])
    assert err is None and obj == {"a": 1}


def test_writer_extract_json_no_text():
    assert BW._extract_json([SN(type="text", text="")]) == (None, "model returned no text content")


def test_writer_extract_json_no_object():
    obj, err = BW._extract_json([SN(type="text", text="no braces here")])
    assert obj is None and "no JSON object found" in err


def test_writer_extract_json_malformed():
    obj, err = BW._extract_json([SN(type="text", text='{"a": }')])
    assert obj is None and err.startswith("could not parse JSON")


def test_usage_line_format_and_cost():
    line = BW._usage_line("claude-sonnet-4-6", 1_000_000, 1_000_000, 3, 2)
    p_in, p_out = AC.resolve_prices("claude-sonnet-4-6",
                                    (BW.PRICE_IN_PER_MTOK, BW.PRICE_OUT_PER_MTOK),
                                    BW.PRICE_OVERRIDE)
    expected_cost = (1_000_000 / 1e6) * p_in + (1_000_000 / 1e6) * p_out
    assert "model=claude-sonnet-4-6" in line
    assert "attempts=2" in line
    assert "web_searches=3" in line
    assert f"est_cost_usd=${expected_cost:.4f}" in line


def test_client_no_key_exits_3(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit) as ei:
        BW._client(object())                              # the SDK arg is unused on the no-key path
    assert ei.value.code == 3


def test_client_with_key_builds_with_retries(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured = {}
    fake_sdk = SN(Anthropic=lambda **kw: captured.update(kw) or "CLIENT")
    client = BW._client(fake_sdk)
    assert client == "CLIENT"
    assert captured["api_key"] == "sk-test"
    assert captured["max_retries"] == 8                   # generous retries for rate-limit bursts


# =====================================================================
# brief_batch — pure helpers
# =====================================================================

def test_cid_sanitises_disambiguates_and_defaults():
    id2tk = {}
    assert BB._cid("XAU/USD", id2tk) == "XAU_USD"
    assert BB._cid("XAU/USD", id2tk) == "XAU_USD_2"       # collision -> suffixed
    assert BB._cid("", id2tk) == "asset"                  # empty ticker -> 'asset'
    assert id2tk["XAU_USD"] == "XAU/USD"                  # reverse map kept


def test_cid_truncates_to_48():
    id2tk = {}
    cid = BB._cid("A" * 80, id2tk)
    assert len(cid) == 48 and set(cid) == {"A"}


def test_merge_tele_sums_and_prefers_b_model():
    a = {"model": "ma", "input_tokens": 10, "output_tokens": 20, "web_searches": 1,
         "cache_read_tokens": 5, "est_cost_usd": 0.10}
    b = {"model": "mb", "input_tokens": 3, "output_tokens": 4, "web_searches": 0,
         "cache_read_tokens": 2, "est_cost_usd": 0.05}
    m = BB._merge_tele(a, b)
    assert m["model"] == "mb"                             # repair-round model wins
    assert (m["input_tokens"], m["output_tokens"]) == (13, 24)
    assert m["web_searches"] == 1
    assert m["cache_read_tokens"] == 7
    assert m["est_cost_usd"] == pytest.approx(0.15)
    assert m["batch"] is True


def test_merge_tele_handles_none_inputs():
    m = BB._merge_tele(None, {"input_tokens": 5})
    assert m["input_tokens"] == 5 and m["output_tokens"] == 0
    m2 = BB._merge_tele({"model": "only-a"}, None)
    assert m2["model"] == "only-a" and m2["input_tokens"] == 0


def test_err_str_nested_message():
    res = SN(type="errored", error=SN(error=SN(message="overloaded")))
    assert BB._err_str(res) == "errored: overloaded"


def test_err_str_top_level_message():
    res = SN(type="errored", error=SN(message="top level"))
    assert BB._err_str(res) == "errored: top level"


def test_err_str_no_error_returns_type():
    res = SN(type="expired", error=None)
    assert BB._err_str(res) == "expired"


def test_err_str_truncates_to_200():
    res = SN(type="errored", error=SN(error=SN(message="x" * 400)))
    assert len(BB._err_str(res)) == 200


def test_batch_timeout_s_default_override_floor_and_garbage(monkeypatch):
    monkeypatch.delenv("ASSETFRAME_BATCH_TIMEOUT_S", raising=False)
    assert BB._batch_timeout_s() == 2400
    monkeypatch.setenv("ASSETFRAME_BATCH_TIMEOUT_S", "100")
    assert BB._batch_timeout_s() == 100
    monkeypatch.setenv("ASSETFRAME_BATCH_TIMEOUT_S", "5")
    assert BB._batch_timeout_s() == 60                    # floored at 60
    monkeypatch.setenv("ASSETFRAME_BATCH_TIMEOUT_S", "nope")
    assert BB._batch_timeout_s() == 2400                  # garbage -> default


def test_poll_s_default_floor_and_garbage(monkeypatch):
    monkeypatch.delenv("ASSETFRAME_BATCH_POLL_S", raising=False)
    assert BB._poll_s() == BB._DEFAULT_POLL_S
    monkeypatch.setenv("ASSETFRAME_BATCH_POLL_S", "0")
    assert BB._poll_s() == 1                              # floored at 1
    monkeypatch.setenv("ASSETFRAME_BATCH_POLL_S", "bad")
    assert BB._poll_s() == BB._DEFAULT_POLL_S


def test_model_prices_delegates_to_table_with_bw_fallback():
    assert BB._model_prices("claude-haiku-4-5") == (1.0, 5.0)
    assert BB._model_prices("claude-opus-4-8") == (15.0, 75.0)
    # unrecognised model falls back to brief_writer's env-configured (Sonnet) prices
    assert BB._model_prices("mystery") == (BW.PRICE_IN_PER_MTOK, BW.PRICE_OUT_PER_MTOK)


def test_summarize_analysis_tolerates_none():
    # regression: build_user_message feeds analysis straight to summarize_analysis; a None analysis
    # (failed load) must not crash — parity with critic, which already guarded it.
    assert isinstance(BW.summarize_analysis(None), dict)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
