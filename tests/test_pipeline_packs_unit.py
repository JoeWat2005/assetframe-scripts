"""Offline unit tests for scripts/pipeline/packs/* — the research / social context
packs and the social-distribution templater.

These modules are pure compilers/validators of an AI-supplied DRAFT JSON: they NEVER
call the web/Neon/Anthropic/R2. So every test here is fully offline and deterministic.
Coverage targets the GAPS left by the existing suite:

  * research_pack.py  — validate()/template()/parse_args() + the no-invention GATE
                        (entirely untested before this file).
  * social_pack.py    — validate()/template()/_check()/platform canon + subtract-only
                        aggregate shape (entirely untested before this file).
  * social_posts.py   — _meta()/confidence_band boundaries/build_posts edge cases +
                        parse_args (test_social_posts.py only covers the QA gate and
                        the happy build_posts path).

Import style mirrors the existing tests (rely on conftest's sys.path shim, which makes
the relocated subpackage modules importable by their flat names).

Run:  python -m pytest tests/test_pipeline_packs_unit.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import research_pack as RP
import social_pack as SP
import social_posts as SOC


# --------------------------------------------------------------------------- helpers
def _exit_code(fn, *args, **kwargs):
    """Call fn expecting a SystemExit; return its .code."""
    with pytest.raises(SystemExit) as ei:
        fn(*args, **kwargs)
    return ei.value.code


def _run_main(mod, argv, monkeypatch, tmp_path):
    """Drive a module's main() with a controlled argv, isolated in tmp_path cwd."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", argv)
    mod.main()


# ============================================================ research_pack: template
def test_rp_template_shape():
    t = RP.template("Apple (AAPL)")
    assert t["instrument"] == "Apple (AAPL)"
    assert t["generated_at_utc"].endswith(" UTC")
    assert isinstance(t["items"], list) and len(t["items"]) == 2
    # one thesis skeleton + one non-thesis skeleton, categories within the allow-list
    assert {i["category"] for i in t["items"]} <= set(RP.CATEGORIES)
    assert t["items"][0]["used_in_thesis"] is True
    assert t["items"][1]["used_in_thesis"] is False
    assert t["source_gaps"] == []


# ============================================================ research_pack: validate happy
def _rp_thesis_item(**over):
    base = {"category": "asset", "headline": "AAPL beats on services",
            "summary": "good", "source_url": "https://x.test/a",
            "timestamp": "2026-06-16 12:00 UTC", "source_quality": "high",
            "used_in_thesis": True}
    base.update(over)
    return base


def test_rp_validate_happy_normalizes_and_counts():
    draft = {"instrument": "Apple (AAPL)", "generated_at_utc": "2026-06-16 13:00 UTC",
             "items": [_rp_thesis_item()], "source_gaps": ["IV not sourced"]}
    pack = RP.validate(draft, "AAPL")
    assert pack["instrument"] == "Apple (AAPL)"
    assert pack["generated_at_utc"] == "2026-06-16 13:00 UTC"
    assert pack["counts"]["items"] == 1
    assert pack["counts"]["thesis_items"] == 1
    assert pack["counts"]["by_category"]["asset"] == 1
    assert pack["counts"]["by_category"]["macro"] == 0
    # the pre-existing gap is preserved (a sourced thesis item adds none)
    assert pack["counts"]["source_gaps"] == 1
    assert pack["source_gaps"] == ["IV not sourced"]


def test_rp_validate_category_and_quality_case_insensitive():
    item = _rp_thesis_item(category="MACRO", source_quality="High")
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["items"][0]["category"] == "macro"
    assert pack["items"][0]["source_quality"] == "high"


def test_rp_validate_url_mirrors_source_url_for_claim_tracing():
    pack = RP.validate({"items": [_rp_thesis_item(source_url="https://src.test/y")]}, "AAPL")
    it = pack["items"][0]
    # confidence._claim_traced reads item['url']; validate must mirror source_url -> url
    assert it["url"] == "https://src.test/y"
    assert it["source_url"] == "https://src.test/y"


def test_rp_validate_url_falls_back_to_url_field_for_non_thesis_item():
    # The stored traceable url falls back source_url -> url (read by confidence._claim_traced).
    # Use a NON-thesis item to isolate the normalization from the GATE.
    item = _rp_thesis_item(used_in_thesis=False)
    del item["source_url"]
    item["url"] = "https://fallback.test/z"
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["items"][0]["source_url"] == "https://fallback.test/z"
    assert pack["items"][0]["url"] == "https://fallback.test/z"


def test_rp_gate_accepts_thesis_item_sourced_only_by_url_field():
    # FIXED: _has_source now accepts the `url` field — the same one the output build mirrors and
    # confidence._claim_traced reads — so a thesis item sourced only by `url` passes the gate (it used
    # to die with exit 2 despite being fully downstream-traceable).
    item = _rp_thesis_item()
    del item["source_url"]
    item["url"] = "https://only-url.test/z"
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["counts"]["thesis_items"] == 1
    assert pack["items"][0]["source_url"] == "https://only-url.test/z"   # url normalised into source_url


def test_rp_validate_instrument_falls_back_to_name():
    pack = RP.validate({"items": []}, "Fallback Name")
    assert pack["instrument"] == "Fallback Name"
    # blank/whitespace instrument also falls back
    pack2 = RP.validate({"instrument": "   ", "items": []}, "Fallback Name")
    assert pack2["instrument"] == "Fallback Name"


def test_rp_validate_thesis_item_can_be_sourced_by_named_source_without_url():
    # _has_source accepts a named `source` even when source_url is empty
    item = _rp_thesis_item(source_url="", source="Reuters")
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["items"][0]["source"] == "Reuters"
    assert pack["counts"]["thesis_items"] == 1


# ============================================================ research_pack: the GATE
def test_rp_validate_non_dict_draft_exits_2():
    assert _exit_code(RP.validate, ["not", "a", "dict"], "AAPL") == 2


def test_rp_validate_missing_items_exits_2():
    assert _exit_code(RP.validate, {"instrument": "X"}, "AAPL") == 2


def test_rp_validate_items_not_a_list_exits_2():
    assert _exit_code(RP.validate, {"items": {"a": 1}}, "AAPL") == 2


def test_rp_validate_item_not_object_exits_2():
    assert _exit_code(RP.validate, {"items": ["plain string"]}, "AAPL") == 2


def test_rp_validate_bad_category_exits_2():
    assert _exit_code(RP.validate, {"items": [_rp_thesis_item(category="rumor")]}, "AAPL") == 2


def test_rp_validate_empty_headline_exits_2():
    assert _exit_code(RP.validate, {"items": [_rp_thesis_item(headline="   ")]}, "AAPL") == 2


def test_rp_validate_bad_quality_exits_2():
    assert _exit_code(RP.validate, {"items": [_rp_thesis_item(source_quality="great")]}, "AAPL") == 2


def test_rp_validate_thesis_item_missing_source_exits_2():
    bad = _rp_thesis_item(source_url="", source="")
    assert _exit_code(RP.validate, {"items": [bad]}, "AAPL") == 2


def test_rp_validate_thesis_item_missing_timestamp_exits_2():
    bad = _rp_thesis_item(timestamp="")
    assert _exit_code(RP.validate, {"items": [bad]}, "AAPL") == 2


def test_rp_validate_nonthesis_unsourced_demoted_to_gaps_not_rejected():
    # a NON-thesis unsourced item is allowed but recorded as a source gap
    item = _rp_thesis_item(used_in_thesis=False, source_url="", source="", timestamp="")
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["counts"]["items"] == 1
    assert pack["counts"]["thesis_items"] == 0
    assert any(g.startswith("unsourced:") for g in pack["source_gaps"])


def test_rp_validate_nonthesis_sourced_item_adds_no_gap():
    item = _rp_thesis_item(used_in_thesis=False)  # still has source_url + ts
    pack = RP.validate({"items": [item]}, "AAPL")
    assert pack["source_gaps"] == []


# ============================================================ research_pack: small helpers
def test_rp_has_source_true_for_url_or_named_source():
    assert RP._has_source({"source_url": "https://x"}) is True
    assert RP._has_source({"source": "WSJ"}) is True
    assert RP._has_source({"source_url": "   "}) is False
    assert RP._has_source({}) is False


def test_rp_now_utc_format():
    s = RP._now_utc()
    assert s.endswith(" UTC")
    # "YYYY-MM-DD HH:MM UTC"
    assert len(s) == len("2026-06-16 13:00 UTC")


def test_rp_parse_args_all_flags():
    opts = RP.parse_args(["--in", "d.json", "--out", "o.json", "--print"])
    assert opts == {"in": "d.json", "out": "o.json", "print": True}


def test_rp_parse_args_unknown_exits_2():
    assert _exit_code(RP.parse_args, ["--bogus"]) == 2


# ============================================================ research_pack: main() IO
def test_rp_main_emits_template_when_no_in(tmp_path, monkeypatch):
    out = tmp_path / "tpl.json"
    _run_main(RP, ["research_pack", "AAPL", "--out", str(out)], monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["instrument"] == "AAPL"
    assert len(written["items"]) == 2


def test_rp_main_validates_in_draft(tmp_path, monkeypatch):
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"items": [_rp_thesis_item()]}), encoding="utf-8")
    out = tmp_path / "pack.json"
    _run_main(RP, ["research_pack", "AAPL", "--in", str(draft), "--out", str(out)],
              monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["counts"]["thesis_items"] == 1


def test_rp_main_missing_draft_exits_2_and_writes_nothing(tmp_path, monkeypatch):
    out = tmp_path / "never.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["research_pack", "AAPL", "--in", str(tmp_path / "nope.json"),
                         "--out", str(out)])
    assert _exit_code(RP.main) == 2
    assert not out.exists()


def test_rp_main_invalid_json_exits_2(tmp_path, monkeypatch):
    draft = tmp_path / "bad.json"
    draft.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["research_pack", "AAPL", "--in", str(draft)])
    assert _exit_code(RP.main) == 2


def test_rp_main_no_name_exits_2(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["research_pack"])
    assert _exit_code(RP.main) == 2


# ============================================================ social_pack: template
def test_sp_template_shape():
    t = SP.template("BTC")
    assert t["instrument"] == "BTC"
    assert t["generated_at_utc"].endswith(" UTC")
    assert t["sources"][0]["platform"] == "Reddit"
    agg = t["aggregate"]
    assert agg["sentiment"] == "neutral"
    assert agg["crowding_risk"] == "unknown" and agg["hype_risk"] == "unknown"


# ============================================================ social_pack: validate
def _sp_source(**over):
    base = {"platform": "Reddit", "url": "https://r.test/p", "timestamp": "2026-06-16 09:00 UTC",
            "summary": "chatter", "sentiment": "bullish", "themes": ["squeeze"],
            "signal_quality": "medium", "notes": ""}
    base.update(over)
    return base


def test_sp_validate_happy_shape_and_counts():
    draft = {"instrument": "BTC", "sources": [_sp_source()],
             "aggregate": {"sentiment": "mixed", "crowding_risk": "high",
                           "hype_risk": "medium", "contrarian_warning": "froth"}}
    pack = SP.validate(draft, "BTC")
    assert pack["instrument"] == "BTC"
    assert pack["counts"]["sources"] == 1
    agg = pack["aggregate"]
    assert agg["sentiment"] == "mixed"
    assert agg["crowding_risk"] == "high"
    assert agg["hype_risk"] == "medium"
    assert agg["contrarian_warning"] == "froth"
    # subtract-only contract: the documented note must be present
    assert "subtract-only" in pack["note"]


def test_sp_validate_sources_none_defaults_to_empty_list():
    pack = SP.validate({"instrument": "BTC"}, "BTC")
    assert pack["sources"] == []
    assert pack["counts"]["sources"] == 0
    # aggregate still defaults cleanly
    assert pack["aggregate"]["sentiment"] == "neutral"


def test_sp_validate_platform_canonicalized_case_insensitively():
    for raw, canon in (("reddit", "Reddit"), ("STOCKTWITS", "Stocktwits"),
                       ("x", "X"), ("news_comments", "news_comments")):
        pack = SP.validate({"sources": [_sp_source(platform=raw)]}, "BTC")
        assert pack["sources"][0]["platform"] == canon


def test_sp_validate_themes_stringified_and_blanks_dropped():
    pack = SP.validate({"sources": [_sp_source(themes=["squeeze", "  ", "", 42])]}, "BTC")
    assert pack["sources"][0]["themes"] == ["squeeze", "42"]


def test_sp_validate_unsourced_source_recorded_as_gap():
    pack = SP.validate({"sources": [_sp_source(url="")]}, "BTC")
    assert any(g.startswith("unsourced social") for g in pack["aggregate"]["source_gaps"])


def test_sp_validate_low_quality_source_recorded_as_gap():
    pack = SP.validate({"sources": [_sp_source(signal_quality="low")]}, "BTC")
    assert any(g.startswith("low-signal source") for g in pack["aggregate"]["source_gaps"])


def test_sp_validate_aggregate_defaults_when_absent():
    pack = SP.validate({"sources": []}, "BTC")
    agg = pack["aggregate"]
    assert agg["sentiment"] == "neutral"
    assert agg["crowding_risk"] == "unknown"
    assert agg["hype_risk"] == "unknown"
    assert agg["contrarian_warning"] == ""
    assert agg["dominant_themes"] == []


def test_sp_validate_preexisting_source_gaps_preserved():
    draft = {"sources": [], "aggregate": {"source_gaps": ["options IV missing"]}}
    pack = SP.validate(draft, "BTC")
    assert "options IV missing" in pack["aggregate"]["source_gaps"]


# ---- social_pack: error branches
def test_sp_validate_non_dict_draft_exits_2():
    assert _exit_code(SP.validate, [1, 2, 3], "BTC") == 2


def test_sp_validate_sources_not_list_exits_2():
    assert _exit_code(SP.validate, {"sources": "nope"}, "BTC") == 2


def test_sp_validate_source_not_object_exits_2():
    assert _exit_code(SP.validate, {"sources": ["str"]}, "BTC") == 2


def test_sp_validate_bad_platform_exits_2():
    assert _exit_code(SP.validate, {"sources": [_sp_source(platform="Discord")]}, "BTC") == 2


def test_sp_validate_source_missing_sentiment_exits_2():
    bad = _sp_source()
    del bad["sentiment"]
    assert _exit_code(SP.validate, {"sources": [bad]}, "BTC") == 2


def test_sp_validate_source_bad_signal_quality_exits_2():
    assert _exit_code(SP.validate, {"sources": [_sp_source(signal_quality="superb")]}, "BTC") == 2


def test_sp_validate_aggregate_bad_sentiment_exits_2():
    draft = {"sources": [], "aggregate": {"sentiment": "euphoric"}}
    assert _exit_code(SP.validate, draft, "BTC") == 2


def test_sp_validate_aggregate_bad_risk_exits_2():
    draft = {"sources": [], "aggregate": {"crowding_risk": "extreme"}}
    assert _exit_code(SP.validate, draft, "BTC") == 2


# ---- social_pack: _check helper
def test_sp_check_normalizes_and_rejects():
    assert SP._check("HIGH", SP.RISK, "x") == "high"
    assert _exit_code(SP._check, "nonsense", SP.RISK, "x") == 2
    assert _exit_code(SP._check, None, SP.SENTIMENT, "sentiment", 3) == 2


def test_sp_parse_args_and_unknown():
    assert SP.parse_args(["--in", "a", "--print"]) == {"in": "a", "out": None, "print": True}
    assert _exit_code(SP.parse_args, ["--huh"]) == 2


# ---- social_pack: main() IO
def test_sp_main_template_no_in(tmp_path, monkeypatch):
    out = tmp_path / "soc.json"
    _run_main(SP, ["social_pack", "BTC", "--out", str(out)], monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["instrument"] == "BTC"
    assert "aggregate" in written


def test_sp_main_validates_in_draft(tmp_path, monkeypatch):
    draft = tmp_path / "d.json"
    draft.write_text(json.dumps({"sources": [_sp_source()]}), encoding="utf-8")
    out = tmp_path / "p.json"
    _run_main(SP, ["social_pack", "BTC", "--in", str(draft), "--out", str(out)],
              monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["counts"]["sources"] == 1


# ============================================================ social_posts: confidence_band
@pytest.mark.parametrize("score,band", [
    (0, "Low"), (49, "Low"), (49.999, "Low"),
    (50, "Moderate"), (64, "Moderate"),
    (65, "Elevated"), (79.9, "Elevated"),
    (80, "High"), (100, "High"),
    ("64", "Moderate"),                  # numeric strings coerced
    (None, "Unknown"), ("abc", "Unknown"),
])
def test_soc_confidence_band_boundaries(score, band):
    assert SOC.confidence_band(score) == band


# ============================================================ social_posts: _meta
def _full_payload():
    return {
        "report_id": "AF-20260616-AAPL", "title": "Apple (AAPL)",
        "status": "Active", "risk": "Medium", "confidence": 64,
        "meta": {
            "instrument": "Apple", "ticker": "AAPL", "confidence_band": "Moderate",
            "research_view": "Constructive into the next session while above the floor.",
            "prediction_window_start_report_tz": "Mon 16 Jun 2026 14:30 UK",
            "prediction_window_end_report_tz": "Tue 17 Jun 2026 21:00 UK",
            "report_date": "2026-06-16",
        },
    }


def test_soc_meta_extracts_all_fields():
    d = SOC._meta(_full_payload())
    assert d["title"] == "Apple (AAPL)"
    assert d["ticker"] == "AAPL"
    assert d["status"] == "Active"
    assert d["risk"] == "Medium"
    assert d["band"] == "Moderate"
    assert d["view"].startswith("Constructive")
    assert d["window"] == "Mon 16 Jun 2026 14:30 UK -> Tue 17 Jun 2026 21:00 UK"
    assert d["report_id"] == "AF-20260616-AAPL"


def test_soc_meta_title_falls_back_to_instrument_then_default():
    d = SOC._meta({"meta": {"instrument": "Apple"}})
    assert d["title"] == "Apple"
    d2 = SOC._meta({})
    assert d2["title"] == "this instrument"


def test_soc_meta_band_from_confidence_when_no_band_label():
    # no meta.confidence_band -> derive from numeric confidence
    d = SOC._meta({"confidence": 82, "meta": {}})
    assert d["band"] == "High"
    # nothing at all -> Unknown
    d2 = SOC._meta({})
    assert d2["band"] == "Unknown"


def test_soc_meta_window_empty_when_both_bounds_missing():
    d = SOC._meta({"meta": {}})
    assert d["window"] == ""


def test_soc_meta_view_falls_back_to_primary_bias():
    d = SOC._meta({"meta": {"primary_bias": "Cautiously bullish"}})
    assert d["view"] == "Cautiously bullish"


# ============================================================ social_posts: build_posts edges
def test_soc_build_posts_truncates_long_research_view():
    long_view = "A" * 250
    payload = _full_payload()
    payload["meta"]["research_view"] = long_view
    posts = SOC.build_posts(payload)
    # the long view is trimmed to <=180 chars with an ellipsis in the short channels
    assert ("A" * 177 + "...") in posts["linkedin"]
    assert long_view not in posts["x"]


def test_soc_build_posts_omits_empty_status_and_risk_clauses():
    payload = _full_payload()
    payload["status"] = ""
    payload["risk"] = ""
    payload["meta"]["status"] = ""
    payload["meta"]["risk_rating"] = ""
    posts = SOC.build_posts(payload)
    assert "Status:" not in posts["x"]
    assert "Risk:" not in posts["x"]
    # band line still present
    assert "Confidence band: Moderate" in posts["x"]


def test_soc_build_posts_handles_minimal_payload():
    posts = SOC.build_posts({})
    assert set(posts) == {"x", "linkedin", "newsletter_snippet", "reddit_summary"}
    for text in posts.values():
        assert "AssetFrame" in text
        assert SOC.REPORT_LINK in text
    # unknown confidence with empty payload
    assert "Confidence band: Unknown" in posts["x"]


def test_soc_build_posts_output_passes_safe_wording_gate():
    # generated drafts must always clear their own QA gate
    SOC.safe_wording_check(SOC.build_posts(_full_payload()))  # no raise


# ============================================================ social_posts: parse_args + main
def test_soc_parse_args_all_flags():
    opts = SOC.parse_args(["--payload", "p.json", "--date", "2026-06-16",
                           "--out", "o.json", "--print"])
    assert opts == {"payload": "p.json", "date": "2026-06-16", "out": "o.json", "print": True}


def test_soc_parse_args_unknown_exits_2():
    assert _exit_code(SOC.parse_args, ["--nope"]) == 2


def test_soc_main_missing_payload_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv",
                        ["social_posts", "AAPL", "--payload", str(tmp_path / "missing.json")])
    assert _exit_code(SOC.main) == 2


def test_soc_main_writes_four_drafts(tmp_path, monkeypatch):
    payload = tmp_path / "pl.json"
    payload.write_text(json.dumps(_full_payload()), encoding="utf-8")
    out = tmp_path / "posts.json"
    _run_main(SOC, ["social_posts", "AAPL", "--payload", str(payload),
                    "--date", "2026-06-16", "--out", str(out)], monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["auto_post"] is False
    assert written["safe_wording_qa"] == "passed"
    assert set(written["posts"]) == {"x", "linkedin", "newsletter_snippet", "reddit_summary"}
    assert written["date"] == "2026-06-16"


def test_soc_main_invalid_payload_json_exits_2(tmp_path, monkeypatch):
    payload = tmp_path / "pl.json"
    payload.write_text("{ broken", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["social_posts", "AAPL", "--payload", str(payload)])
    assert _exit_code(SOC.main) == 2


def test_soc_main_date_defaults_to_report_date_from_meta(tmp_path, monkeypatch):
    payload = tmp_path / "pl.json"
    payload.write_text(json.dumps(_full_payload()), encoding="utf-8")
    out = tmp_path / "posts.json"
    # no --date: should fall back to meta.report_date
    _run_main(SOC, ["social_posts", "AAPL", "--payload", str(payload), "--out", str(out)],
              monkeypatch, tmp_path)
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["date"] == "2026-06-16"


# ============================================================ research_pack: gaps dedup quirk
def test_rp_unsourced_gap_dedup_collapses_duplicates():
    # FIXED: the dedup now compares the ACTUAL gap string, so duplicate unsourced non-thesis items
    # with the same headline collapse to ONE gap.
    base = {"category": "macro", "headline": "Same headline", "summary": "", "source_url": "",
            "source": "", "timestamp": "", "source_quality": "medium", "used_in_thesis": False}
    pack = RP.validate({"items": [dict(base), dict(base)]}, "AAPL")
    gaps = [g for g in pack["source_gaps"] if g.startswith("unsourced:")]
    assert len(gaps) == 1
