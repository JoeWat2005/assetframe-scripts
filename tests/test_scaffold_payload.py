"""Tests for scaffold_payload.py — QA-by-construction (every setup/ladder/ledger
price is a canonical level value), the claim-sourcing THESIS_BLOCKED gate, the
free/pro split guard (no Pro vocab leaks into the free Snapshot), and the level
catalog / RR formatting helpers.

Run:  python -m pytest tests/test_scaffold_payload.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scaffold_payload as SP


def _analysis():
    return {
        "pivots_classic": {"PP": 100, "R1": 103, "R2": 106, "S1": 97, "S2": 94, "R3": 109, "S3": 91},
        "atr_day_bands": {"open": 100, "inner_hi": 102, "inner_lo": 98,
                          "outer_hi": 105, "outer_lo": 95},
        "hourly": {"swing_highs": [{"p": 104.2}], "swing_lows": [{"p": 96.1}]},
        "daily": {"atr14": 5.0},
    }


class TestLevelCatalog(unittest.TestCase):
    def test_levels_sorted_high_to_low_and_deduped(self):
        levels, by_id = SP.build_levels(_analysis(), last_price=99.0)
        vals = [l["value"] for l in levels]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(len(vals), len(set(round(v, 4) for v in vals)))  # de-duped
        self.assertIn("anchor", by_id)
        self.assertEqual(by_id["anchor"]["value"], 99.0)

    def test_every_level_has_id_class_label(self):
        levels, _ = SP.build_levels(_analysis(), 99.0)
        for l in levels:
            self.assertTrue(l["id"])
            self.assertTrue(l["cls"])
            self.assertTrue(l["label"])
            self.assertIsInstance(l["value"], float)


class TestQAByConstruction(unittest.TestCase):
    """Setups/ladder/ledger levels must reference ONLY canonical level values."""

    def setUp(self):
        self.levels, self.by_id = SP.build_levels(_analysis(), 99.0)
        self.level_vals = {round(l["value"], 4) for l in self.levels}
        self.setups = SP.build_setups(self.by_id, self.levels)

    def test_setups_built(self):
        self.assertTrue(self.setups)

    def test_every_setup_price_is_a_canonical_level(self):
        for s in self.setups:
            for key in ("entry_lo", "entry_hi", "invalidation", "t1", "t2"):
                v = s.get(key)
                if v is None:
                    continue
                self.assertIn(round(v, 4), self.level_vals,
                              f"{s['name']} {key}={v} is not a canonical level")

    def test_rr_string_format(self):
        for s in self.setups:
            self.assertTrue(s["rr"].startswith("T1 ") or s["rr"].startswith("No valid"))

    def test_ladder_ids_all_canonical_and_capped_at_12(self):
        ladder = SP.build_ladder(self.levels, self.setups)
        ids = {l["id"] for l in self.levels}
        for lid in ladder:
            self.assertIn(lid, ids)
        self.assertLessEqual(len(ladder), 12)
        self.assertNotIn("anchor", ladder)  # anchor renders as LAST, not in ladder

    def test_ladder_contains_every_setup_target_and_invalidation(self):
        ladder = SP.build_ladder(self.levels, self.setups)
        val_to_id = {round(l["value"], 4): l["id"] for l in self.levels}
        for s in self.setups:
            for key in ("invalidation", "t1", "t2"):
                v = s.get(key)
                if v is None:
                    continue
                self.assertIn(val_to_id[round(v, 4)], ladder)

    def test_ledger_levels_are_canonical_values(self):
        brief = {"manual_prediction": "GDP surprise"}
        preds, ledger_levels = SP.build_predictions_spec(self.by_id, brief, "bullish")
        for v in ledger_levels:
            self.assertIn(round(v, 4), self.level_vals)
        # ledger levels are distinct
        self.assertEqual(len(ledger_levels),
                         len({round(v, 4) for v in ledger_levels}))


class TestRRFormatting(unittest.TestCase):
    def test_zero_risk_excluded(self):
        out, m1, m2 = SP._fmt_rr(100, 100, 105, 110)
        self.assertEqual(out, "No valid R:R - excluded")

    def test_below_one_x(self):
        out, _, _ = SP._fmt_rr(100, 95, 102, None)  # risk 5, reward 2 -> 0.4x
        self.assertIn("below 1.0x", out)

    def test_normal_multiples(self):
        out, m1, m2 = SP._fmt_rr(100, 95, 110, 115)  # risk 5; T1 2.0x; T2 3.0x
        self.assertIn("T1 2.0x", out)
        self.assertIn("T2 3.0x", out)


class TestClaimSourcingGate(unittest.TestCase):
    def test_weak_thesis_claim_blocked(self):
        for status in ("unverified", "stale", "unavailable"):
            with self.assertRaises(SystemExit) as cm:
                SP._claims([{"claim": "big", "status": status, "used_in_thesis": True}])
            self.assertEqual(cm.exception.code, 2)

    def test_invalid_status_rejected(self):
        with self.assertRaises(SystemExit) as cm:
            SP._claims([{"claim": "x", "status": "rumor", "used_in_thesis": False}])
        self.assertEqual(cm.exception.code, 2)

    def test_strong_thesis_claim_allowed(self):
        out = SP._claims([{"claim": "x", "status": "multiple-source",
                           "used_in_thesis": True, "source": "CNBC"}])
        self.assertTrue(out[0]["used_in_thesis"])
        self.assertEqual(out[0]["status"], "multiple-source")

    def test_weak_claim_ok_if_not_in_thesis(self):
        out = SP._claims([{"claim": "x", "status": "single-source",
                           "used_in_thesis": False}])
        self.assertFalse(out[0]["used_in_thesis"])


class TestFreeProSplit(unittest.TestCase):
    def _clean_free(self):
        return {
            "cards": [["Last price", "100"]],
            "chart": {"support": [96.1], "resistance": [100]},
            "bullets_html": "<ul><li>clean thesis text</li></ul>",
            "scenarios_html": "<table></table>",
            "teaser": "Pro adds the ladder and R:R and invalidation (this is the pitch).",
            "disclaimer": "not advice",
        }

    def test_clean_free_passes(self):
        SP._assert_free_split(self._clean_free())  # no raise

    def test_pro_vocab_leak_blocked(self):
        for word in ("r:r", "invalidation", "ladder", "source audit", "outcome ledger"):
            free = self._clean_free()
            free["bullets_html"] = f"<ul><li>{word} stuff</li></ul>"
            with self.assertRaises(SystemExit) as cm:
                SP._assert_free_split(free)
            self.assertEqual(cm.exception.code, 2, f"{word!r} should have leaked")

    def test_teaser_may_name_pro_features(self):
        # the teaser is the lead-magnet pitch and is exempt from the vocab scan
        free = self._clean_free()
        free["teaser"] = "Pro adds R:R, the ladder, invalidation levels and the source audit."
        SP._assert_free_split(free)  # no raise

    def test_free_chart_more_than_3_levels_blocked(self):
        free = self._clean_free()
        free["chart"] = {"support": [1, 2], "resistance": [3, 4]}
        with self.assertRaises(SystemExit):
            SP._assert_free_split(free)

    def test_free_chart_with_pivots_blocked(self):
        free = self._clean_free()
        free["chart"]["pivots"] = {"PP": 100}
        with self.assertRaises(SystemExit):
            SP._assert_free_split(free)


class TestDisclaimers(unittest.TestCase):
    def test_free_and_pro_disclaimers_present(self):
        self.assertIn("Not personal financial advice", SP.DISCLAIMER_FREE)
        self.assertIn("never places trades", SP.DISCLAIMER_PRO)


if __name__ == "__main__":
    unittest.main(verbosity=2)
