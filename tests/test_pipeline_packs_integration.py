"""Phase 2 INTEGRATION tests for scripts/pipeline/packs/* WIRED TO THEIR CONSUMERS.

Phase 1 (test_pipeline_packs_unit.py) already pins each pack module's functions in
isolation. This file instead exercises the CROSS-MODULE data contracts that only
hold when the real modules combine:

  research_pack.validate(draft)  --pack-->  confidence._claim_traced / catalyst_confidence
  social_pack.validate(draft)    --pack-->  confidence.social_adjustment   (subtract-only)
  scaffold-shaped payload        --------->  social_posts.build_posts -> safe_wording_check

The packs are pure compilers of an AI-supplied DRAFT (no web/Neon/Anthropic/R2), and
confidence is pure stdlib, so the whole flow is offline + deterministic. We drive it
with the REAL repo fixtures (tests/test_fixtures/BTC_research_brief.json) so the claim
sources / statuses are realistic, and we mirror the EXACT meta keys that
scripts/pipeline/scoring/scaffold_payload.py writes (the artifact social_posts reads).

Import style mirrors the existing tests: conftest puts the repo root on sys.path and
imports `scripts` (its subpackage shim makes the relocated modules importable by their
flat names); we also insert the tests dir for the fixture path.

Run:  python -m pytest tests/test_pipeline_packs_integration.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_pack as RP          # noqa: E402
import social_pack as SP            # noqa: E402
import social_posts as SOC          # noqa: E402
import confidence as CONF           # noqa: E402  (the real cross-subdir consumer)

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_fixtures")
# A real CoinDesk claim source that appears TWICE in the BTC brief's weak thesis claims.
_COINDESK = ("https://www.coindesk.com/tech/2026/06/17/live-markets-a-bitcoin-bottom-"
             "signal-flashed-as-holders-absorbed-125-000-btc-in-june")
_COINFLIP = "https://coinflip.trade/blog/bitcoin-open-interest-and-funding-rate-analysis"


# --------------------------------------------------------------------------- helpers
def _btc_brief():
    with open(os.path.join(_FIXTURES, "BTC_research_brief.json"), encoding="utf-8") as f:
        return json.load(f)


def _exit_code(fn, *args, **kwargs):
    with pytest.raises(SystemExit) as ei:
        fn(*args, **kwargs)
    return ei.value.code


def _weak_thesis_claims(brief):
    return [c for c in brief["claims"]
            if c.get("used_in_thesis") and (c.get("status") or "").lower() in CONF._WEAK_STATUSES]


def _multi_item_draft():
    """A realistic draft: a URL-sourced thesis item, a named-source-only thesis item,
    a sourced non-thesis item, and an UNSOURCED non-thesis item (-> demoted to a gap)."""
    return {
        "instrument": "Bitcoin / US Dollar",
        "generated_at_utc": "2026-06-17 18:45 UTC",
        "items": [
            {"category": "macro", "headline": "FOMC hawkish hold, dot-plot revised up",
             "summary": "9 of 18 project a 2026 hike", "source_url": _COINDESK,
             "timestamp": "2026-06-17 18:00 UTC", "source_quality": "high",
             "used_in_thesis": True},
            {"category": "regulatory", "headline": "CFTC perpetual futures guidance",
             "summary": "named source only, no url", "source": "coinflip.trade",
             "timestamp": "2026-06-12 00:00 UTC", "source_quality": "medium",
             "used_in_thesis": True},
            {"category": "asset", "headline": "ETF flow recap (sourced, not thesis)",
             "summary": "context", "source_url": "https://news.bitcoin.com/x",
             "timestamp": "2026-06-12 00:00 UTC", "source_quality": "medium",
             "used_in_thesis": False},
            {"category": "geopolitical", "headline": "Unsourced rumor of a deal",
             "summary": "no source at all", "source_url": "", "source": "",
             "timestamp": "", "source_quality": "low", "used_in_thesis": False},
        ],
        "source_gaps": ["Deribit options IV not sourced"],
    }


# ===================================================================================
# A. research_pack.validate  ->  confidence._claim_traced / catalyst_confidence
# ===================================================================================
def test_research_pack_validate_output_feeds_claim_traced_contract():
    """validate() must emit per-item `url` (mirroring source_url) AND `source`, the two
    fields confidence._claim_traced reads. Prove a real brief claim traces to the pack."""
    pack = RP.validate(_multi_item_draft(), "BTC")

    # The clean pack honoured the gate (both thesis items were sourced) and demoted the
    # one unsourced non-thesis item into source_gaps (cross-checking the validator wiring).
    assert pack["counts"]["thesis_items"] == 2
    assert pack["counts"]["items"] == 4
    assert any(g.startswith("unsourced:") for g in pack["source_gaps"])
    assert "Deribit options IV not sourced" in pack["source_gaps"]   # pre-existing gap preserved

    # contract: url mirrors source_url so _claim_traced can match a URL-sourced claim...
    url_item = pack["items"][0]
    assert url_item["url"] == url_item["source_url"] == _COINDESK
    claim_url = {"source": _COINDESK, "status": "single-source", "used_in_thesis": True}
    assert CONF._claim_traced(claim_url, pack) is True

    # ...and a named-source-only item is traced via substring (item.source in claim.source).
    named_item = pack["items"][1]
    assert named_item["url"] == "" and named_item["source"] == "coinflip.trade"
    claim_named = {"source": _COINFLIP, "status": "single-source", "used_in_thesis": True}
    assert CONF._claim_traced(claim_named, pack) is True

    # a claim whose source is in NEITHER item does not trace.
    claim_miss = {"source": "https://example.test/unrelated", "status": "single-source",
                  "used_in_thesis": True}
    assert CONF._claim_traced(claim_miss, pack) is False


def test_real_brief_claims_trace_against_validated_pack():
    """Over the REAL BTC brief: a pack covering the CoinDesk URL traces both CoinDesk weak
    claims but not the coinflip one -> the data contract holds across the refactor."""
    brief = _btc_brief()
    pack = RP.validate(
        {"items": [{"category": "macro", "headline": "CoinDesk on-chain + FOMC",
                    "summary": "x", "source_url": _COINDESK, "timestamp": "2026-06-17 18:00 UTC",
                    "source_quality": "high", "used_in_thesis": True}]},
        "BTC")
    weak = _weak_thesis_claims(brief)
    traced = [CONF._claim_traced(c, pack) for c in weak]
    # the BTC fixture has 3 weak thesis claims: two share the CoinDesk URL (both traced),
    # one is coinflip.trade (not in the pack -> not traced).
    assert traced.count(True) == 2
    assert traced.count(False) == 1
    coinflip = [c for c in weak if "coinflip" in c["source"]]
    assert coinflip and not any(CONF._claim_traced(c, pack) for c in coinflip)


def test_catalyst_confidence_downgrades_only_untraced_weak_claims():
    """End-to-end: catalyst_confidence(brief, pack) must penalise ONLY the weak thesis
    claim that is not traceable to a validated pack item; a covering pack scores higher
    than an empty pack but lower than no-pack (where nothing is downgraded)."""
    brief = _btc_brief()
    cover = RP.validate(
        {"items": [{"category": "macro", "headline": "h", "summary": "x",
                    "source_url": _COINDESK, "timestamp": "t", "source_quality": "high",
                    "used_in_thesis": True}]}, "BTC")
    empty = RP.validate({"items": []}, "BTC")

    s_none, d_none = CONF.catalyst_confidence(brief, None)
    s_cover, d_cover = CONF.catalyst_confidence(brief, cover)
    s_empty, d_empty = CONF.catalyst_confidence(brief, empty)

    # no pack -> no downgrade is the ceiling; covering the CoinDesk source rescues 2 of the
    # 3 weak claims; an empty pack traces nothing so all 3 are downgraded.
    assert s_none > s_cover > s_empty
    assert d_none["claim_support"] > d_cover["claim_support"] > d_empty["claim_support"]
    # gaps component is identical (it comes from the BRIEF, not the pack) — proves the
    # delta is purely the claim-trace downgrade, not gap drift.
    assert d_none["source_gaps"] == d_cover["source_gaps"] == d_empty["source_gaps"]


def test_named_source_pack_item_traces_url_claim_end_to_end():
    """A pack item the AI gathered with only a NAMED source (no url) must still rescue a
    weak thesis claim whose source is a URL containing that name — proving validate()'s
    `source` field is the one _claim_traced falls back to."""
    brief = _btc_brief()
    pack = RP.validate(
        {"items": [{"category": "asset", "headline": "on-chain", "summary": "x",
                    "source": "coindesk.com", "source_url": "",
                    "timestamp": "2026-06-17 18:00 UTC", "source_quality": "high",
                    "used_in_thesis": True}]}, "BTC")
    coindesk_weak = [c for c in _weak_thesis_claims(brief) if "coindesk" in c["source"]]
    assert coindesk_weak, "fixture should have a coindesk weak thesis claim"
    assert all(CONF._claim_traced(c, pack) for c in coindesk_weak)


def test_validate_gate_rejects_unsourced_thesis_before_any_consumer_sees_it():
    """The no-invention GATE is the first half of the contract: an unsourced thesis item
    never reaches confidence at all (exit 2), so _claim_traced is never asked to rescue
    an invented claim."""
    bad = {"items": [{"category": "macro", "headline": "invented", "summary": "",
                      "source_url": "", "source": "", "timestamp": "",
                      "source_quality": "high", "used_in_thesis": True}]}
    assert _exit_code(RP.validate, bad, "BTC") == 2


# ===================================================================================
# B. social_pack.validate  ->  confidence.social_adjustment  (subtract-only)
# ===================================================================================
def test_social_pack_feeds_subtract_only_adjustment():
    """validate() normalises the AI's mixed-case risk strings into the EXACT aggregate
    shape social_adjustment() reads, and the resulting penalty is strictly <= 0."""
    draft = {
        "instrument": "BTC",
        "sources": [{"platform": "reddit", "url": "https://r.test/p",
                     "timestamp": "2026-06-17 09:00 UTC", "summary": "hype thread",
                     "sentiment": "BULLISH", "themes": ["squeeze"],
                     "signal_quality": "HIGH", "notes": ""}],
        "aggregate": {"sentiment": "bullish", "crowding_risk": "MEDIUM",
                      "hype_risk": "HIGH", "contrarian_warning": "froth and retail FOMO"},
    }
    pack = SP.validate(draft, "BTC")
    # validate canonicalised the case so the consumer's lower() is redundant, not load-bearing.
    assert pack["aggregate"]["hype_risk"] == "high"
    assert pack["aggregate"]["crowding_risk"] == "medium"

    adj, detail = CONF.social_adjustment(pack)
    # high hype (-5) + medium crowding (-1) + contrarian warning (-2) = -8, and never positive.
    assert adj == -8.0
    assert adj <= 0.0
    assert "high hype risk" in detail["notes"] and "contrarian warning" in detail["notes"]


def test_social_pack_template_yields_zero_adjustment():
    """The neutral template (all-unknown aggregate) is the OPTIONALITY contract: a present-
    but-empty social pack contributes a 0 adjustment, never a penalty or a boost."""
    adj, _ = CONF.social_adjustment(SP.template("BTC"))
    assert adj == 0.0


def test_social_pack_penalty_clamped_at_floor_through_validate():
    """Worst-case validated aggregate (high/high/contrarian) still floors at -10, proving
    the subtract-only cap survives the validate->adjust round-trip."""
    pack = SP.validate({"sources": [],
                        "aggregate": {"hype_risk": "high", "crowding_risk": "high",
                                      "contrarian_warning": "blow-off top"}}, "BTC")
    adj, _ = CONF.social_adjustment(pack)
    assert adj == -10.0          # -5 + -3 + -2 = -10, exactly the floor


# ===================================================================================
# C. scaffold-shaped payload  ->  social_posts.build_posts  ->  safe_wording_check
# ===================================================================================
def _scaffold_shaped_payload(brief, **meta_over):
    """Mirror the EXACT keys scripts/pipeline/scoring/scaffold_payload.py emits and that
    social_posts._meta reads (top-level status/risk/confidence/title/report_id + the meta
    block with research_view, *_report_tz window bounds, confidence_band)."""
    meta = {
        "instrument": brief["instrument"], "ticker": brief["ticker"],
        "status": brief["status"], "risk_rating": brief["risk"],
        "research_view": brief["research_view"], "primary_bias": brief["primary_bias"],
        "confidence_band": "Low", "report_date": "2026-06-17",
        "prediction_window_start_report_tz": "Wed 17 Jun 2026 19:30 UTC",
        "prediction_window_end_report_tz": "Thu 18 Jun 2026 21:00 UTC",
    }
    meta.update(meta_over)
    return {
        "report_id": "AF-20260617-BTC",
        "title": f"{brief['instrument']} ({brief['ticker']})",
        "status": brief["status"], "risk": brief["risk"], "confidence": 52.0,
        "meta": meta,
    }


def test_social_posts_build_over_scaffold_shaped_payload_passes_qa():
    """The four drafts templated from a realistic scaffold-shaped payload must carry the
    safe framing and clear their own safe-wording gate — the publish-time contract."""
    brief = _btc_brief()
    posts = SOC.build_posts(_scaffold_shaped_payload(brief))
    assert set(posts) == {"x", "linkedin", "newsletter_snippet", "reddit_summary"}
    for text in posts.values():
        assert "AssetFrame" in text
        assert SOC.REPORT_LINK in text
        assert SOC.DISCLAIMER in text
    # meta fields actually flowed through (band + the report-tz window).
    assert "Confidence band: Low" in posts["x"]
    assert ("Wed 17 Jun 2026 19:30 UTC -> Thu 18 Jun 2026 21:00 UTC") in posts["linkedin"]
    SOC.safe_wording_check(posts)        # no raise on the realistic, neutral research_view


def test_social_posts_qa_gate_catches_pump_language_from_payload_view():
    """Cross-module safety: AI-authored research_view flows from the payload INTO the
    linkedin/newsletter/reddit drafts, so a banned pump phrase there must trip the QA gate
    (exit 2) — build_posts and safe_wording_check are wired, not independent."""
    brief = _btc_brief()
    payload = _scaffold_shaped_payload(brief,
                                       research_view="you should buy now — a sure thing, easy profit")
    posts = SOC.build_posts(payload)
    assert "buy now" in posts["linkedin"].lower()        # the pump phrase did flow through
    assert _exit_code(SOC.safe_wording_check, posts) == 2


def test_social_posts_negated_guaranteed_disclaimer_clears_gate():
    """The standing DISCLAIMER says 'No outcome is guaranteed.' — the gate must allow the
    word 'guaranteed' in that negated compliance form on every generated draft."""
    posts = SOC.build_posts(_scaffold_shaped_payload(_btc_brief()))
    assert all("No outcome is guaranteed" in t for t in posts.values())
    SOC.safe_wording_check(posts)        # negated 'guaranteed' is permitted -> no raise


# ===================================================================================
# D. all three packs feeding ONE confidence picture (the combined contract)
# ===================================================================================
def test_all_three_packs_combine_consistently_for_one_instrument():
    """research_pack + social_pack + the social_posts payload for the SAME instrument feed
    confidence without contradicting each other: the traced research pack keeps catalyst
    support up while the hot social pack applies a subtract-only haircut, and the posts
    templated from the published payload still clear QA."""
    brief = _btc_brief()
    research = RP.validate(
        {"items": [{"category": "macro", "headline": "FOMC + on-chain", "summary": "x",
                    "source_url": _COINDESK, "timestamp": "t", "source_quality": "high",
                    "used_in_thesis": True}]}, "BTC")
    social = SP.validate({"sources": [], "aggregate": {"hype_risk": "medium",
                          "crowding_risk": "low", "contrarian_warning": ""}}, "BTC")

    cat, _ = CONF.catalyst_confidence(brief, research)
    cat_no_pack, _ = CONF.catalyst_confidence(brief, None)
    soc, _ = CONF.social_adjustment(social)

    # catalyst is rescued (>= empty-pack floor) and social only ever subtracts.
    assert cat <= cat_no_pack            # a pack can only hold-or-lower vs the no-downgrade ceiling
    assert cat == CONF.catalyst_confidence(brief, research)[0]   # deterministic
    assert soc == -2.0                   # medium hype only

    posts = SOC.build_posts(_scaffold_shaped_payload(brief))
    SOC.safe_wording_check(posts)        # the same instrument's distribution copy is clean
