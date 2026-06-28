"""Tests for brief_writer.py — proving the schema VALIDATOR matches the real briefs.

The validator is the contract between the AI author and scaffold_payload.py, so the
load-bearing test loads every hand/AI-authored brief under data/briefs/ and asserts the
validator PASSES it (if it ever drifts from the real schema, this fails). Plus malformed
cases it must REJECT, and the no-network helpers (analysis compaction, prompt build).

No Anthropic API calls — the live authoring path is exercised by the user.

Run:  python -m pytest tests/test_brief_writer.py
"""
import copy
import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import brief_writer as BW

# Committed fixtures live next to the tests. The runtime data/ dir is gitignored, so a clean
# checkout (CI) has no real briefs — these tracked copies are the schema examples we validate.
BRIEF_DIR = Path(__file__).resolve().parent / "test_fixtures"


def _load(name):
    return json.loads((BRIEF_DIR / name).read_text(encoding="utf-8-sig"))


class TestValidatorAcceptsRealBriefs(unittest.TestCase):
    """Every committed brief is a real schema example — the validator must accept all."""

    def test_all_briefs_validate(self):
        briefs = sorted(BRIEF_DIR.glob("*_research_brief.json"))
        self.assertTrue(briefs, f"no briefs found in {BRIEF_DIR} to validate against")
        for bp in briefs:
            data = json.loads(bp.read_text(encoding="utf-8-sig"))
            errs = BW.validate_brief(data)
            self.assertEqual(errs, [], f"{bp.name} should validate but got: {errs}")

    def test_btc_brief_explicitly(self):
        # BTC is the reference fixture the task names — assert it explicitly.
        errs = BW.validate_brief(_load("BTC_research_brief.json"))
        self.assertEqual(errs, [])


class TestValidatorRejectsMalformed(unittest.TestCase):
    def setUp(self):
        # start from a real, valid brief and break it one field at a time
        self.base = _load("BTC_research_brief.json")

    def test_not_an_object(self):
        self.assertTrue(BW.validate_brief(["not", "a", "dict"]))
        self.assertTrue(BW.validate_brief("nope"))

    def test_missing_required_field(self):
        b = copy.deepcopy(self.base)
        del b["status"]
        errs = BW.validate_brief(b)
        self.assertTrue(any("status" in e for e in errs))

    def test_bad_risk_enum(self):
        b = copy.deepcopy(self.base)
        b["risk"] = "Extreme"
        errs = BW.validate_brief(b)
        self.assertTrue(any("risk" in e for e in errs))

    def test_bad_direction_enum(self):
        b = copy.deepcopy(self.base)
        b["directional_view"] = "up"
        errs = BW.validate_brief(b)
        self.assertTrue(any("directional_view" in e for e in errs))

    def test_bad_prediction_type(self):
        b = copy.deepcopy(self.base)
        b["primary_prediction"]["type"] = "momentum"
        errs = BW.validate_brief(b)
        self.assertTrue(any("primary_prediction.type" in e for e in errs))

    def test_weak_claim_driving_thesis_rejected(self):
        # mirrors scaffold._claims + mvp_report THESIS_BLOCKED
        b = copy.deepcopy(self.base)
        b["claims"] = [{"claim": "rumoured deal", "status": "unverified",
                        "source": "https://x", "used_in_thesis": True}]
        errs = BW.validate_brief(b)
        self.assertTrue(any("used_in_thesis" in e for e in errs))

    def test_invalid_claim_status_rejected(self):
        b = copy.deepcopy(self.base)
        b["claims"] = [{"claim": "x", "status": "rumor", "used_in_thesis": False}]
        errs = BW.validate_brief(b)
        self.assertTrue(any("status" in e for e in errs))

    def test_thesis_claim_without_source_rejected(self):
        b = copy.deepcopy(self.base)
        b["claims"] = [{"claim": "big macro fact", "status": "multiple-source",
                        "source": "", "used_in_thesis": True}]
        errs = BW.validate_brief(b)
        self.assertTrue(any("source" in e for e in errs))

    def test_empty_invalidators_rejected(self):
        b = copy.deepcopy(self.base)
        b["primary_prediction"]["invalidators"] = []
        errs = BW.validate_brief(b)
        self.assertTrue(any("invalidators" in e for e in errs))

    def test_missing_narrative_subfields_rejected(self):
        b = copy.deepcopy(self.base)
        b["narrative"].pop("long_short_view")
        errs = BW.validate_brief(b)
        self.assertTrue(any("long_short_view" in e for e in errs))

    def test_options_context_must_be_bool(self):
        b = copy.deepcopy(self.base)
        b["options_context_included"] = "yes"
        errs = BW.validate_brief(b)
        self.assertTrue(any("options_context_included" in e for e in errs))


class TestAnalysisCompaction(unittest.TestCase):
    """summarize_analysis must surface the context fields and keep prices flagged as
    do-not-author — never an empty dump."""

    def _analysis(self):
        ap = Path(__file__).resolve().parent / "test_fixtures" / "BTC_analysis.json"
        return json.loads(ap.read_text(encoding="utf-8-sig"))

    def test_keys_present(self):
        s = BW.summarize_analysis(self._analysis())
        for k in ("trend", "momentum", "freshness", "daily_context",
                  "levels_context_only_never_author"):
            self.assertIn(k, s)
        # the explicit "do not author" naming must survive into the summary
        self.assertIn("last_price_DO_NOT_AUTHOR", s)
        self.assertIn("rsi14_hourly", s["momentum"])

    def test_handles_empty_analysis(self):
        s = BW.summarize_analysis({})           # must not raise
        self.assertIsInstance(s, dict)


class TestPromptBuild(unittest.TestCase):
    def test_system_prompt_encodes_rules(self):
        sp = BW.SYSTEM_PROMPT.lower()
        for needle in ("never author prices", "banned language", "claim gating",
                       "taxonomy", "not regulated financial advice"):
            self.assertIn(needle, sp, f"system prompt missing rule: {needle!r}")

    def test_schema_doc_lists_enums(self):
        doc = BW._schema_doc()
        for t in BW.PREDICTION_TYPES:
            self.assertIn(t, doc)
        for s in BW.CLAIM_STATUSES:
            self.assertIn(s, doc)

    def test_user_message_includes_guidance(self):
        msg = BW.build_user_message("BTC", {}, {"x": 1}, None, None,
                                    guidance="[blocker] claims[0]: unsourced -> add source")
        self.assertIn("REVISION GUIDANCE", msg)
        self.assertIn("unsourced", msg)

    def test_user_message_without_guidance(self):
        msg = BW.build_user_message("BTC", {}, {"x": 1}, None, None, guidance=None)
        self.assertNotIn("REVISION GUIDANCE", msg)
        self.assertIn("CONTEXT", msg)


class TestPriceResolution(unittest.TestCase):
    """resolve_prices precedence (Phase-4 review fix): an EXPLICIT operator override wins over the
    per-model table; otherwise the model-family table applies; otherwise the env-default fallback."""

    def test_model_table_beats_fallback(self):
        from anthropic_client import resolve_prices
        # a recognised family resolves from the table, NOT the (Sonnet) fallback — the critic-mispricing fix
        self.assertEqual(resolve_prices("claude-haiku-4-5", (3.0, 15.0)), (1.0, 5.0))
        self.assertEqual(resolve_prices("claude-sonnet-4-6", (3.0, 15.0)), (3.0, 15.0))

    def test_unknown_model_uses_fallback(self):
        from anthropic_client import resolve_prices
        self.assertEqual(resolve_prices("some-future-model", (2.5, 9.0)), (2.5, 9.0))

    def test_explicit_override_wins_over_model_table(self):
        from anthropic_client import resolve_prices
        # the documented ANTHROPIC_PRICE_IN/OUT knob: when set, it overrides even a recognised model
        self.assertEqual(resolve_prices("claude-haiku-4-5", (3.0, 15.0), (2.5, 9.0)), (2.5, 9.0))

    def test_brief_writer_override_only_set_when_env_present(self):
        # PRICE_OVERRIDE must stay None unless the operator actually set the env var, else the
        # Sonnet-default PRICE_* would wrongly force Sonnet rates onto a Haiku/Opus run.
        import importlib
        for present in (False, True):
            with mock.patch.dict(os.environ):   # snapshot the real env; restored on context exit
                os.environ.pop("ANTHROPIC_PRICE_IN", None)
                os.environ.pop("ANTHROPIC_PRICE_OUT", None)
                if present:
                    os.environ["ANTHROPIC_PRICE_IN"] = "4.2"
                bw = importlib.reload(BW)
                self.assertEqual(bw.PRICE_OVERRIDE is not None, present)
        importlib.reload(BW)   # restore module to ambient env for any later test


if __name__ == "__main__":
    unittest.main(verbosity=2)
