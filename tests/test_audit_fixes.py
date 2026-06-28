"""Regression tests for the 2026-06-27 comprehensive-audit fixes:
  - neutral/mixed briefs no longer register a directional (bearish) prediction set
  - pending/ predictions are abandoned once too old to ever grade (no infinite retry/refetch)
  - the daily-cadence due-date is the run's UTC date (DST-correct for US-zone assets)
  - an over-cautious 'reject' with no concrete blocker is downgraded to publish
  - set_config validators: RUN_TIMEOUT capped under the systemd ceiling; TWELVEDATA_RATE settable
Run:  python -m pytest tests/test_audit_fixes.py -q
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scaffold_payload as SP
import calendar_rules as C
import engine_ops as E
import run_daily as RD

UTC = timezone.utc


# --------------------------------------------- neutral/mixed -> no directional predictions
def _by_id():
    return {k: {"value": v} for k, v in {
        "pp": 100.0, "tail_lo": 95.0, "tail_hi": 105.0, "r1": 103.0, "r2": 107.0,
        "swing_lo": 92.0, "anchor": 100.0}.items()}


def _ptypes(direction):
    preds, _lv = SP.build_predictions_spec(_by_id(), {}, direction)
    return {p["id"]: p for p in preds}


def test_neutral_drops_directional_predictions():
    p = _ptypes("neutral")
    assert "P1" not in p and "P3" not in p          # no settle-vs-PP / R1-touch directional bet
    assert "P2" in p and "P4" in p                  # symmetric range + floor predictions kept
    p2 = _ptypes("mixed")
    assert "P1" not in p2 and "P3" not in p2         # 'mixed' is non-directional too (was silently bearish)


def test_bearish_still_emits_directional_with_expect_false():
    p = _ptypes("bearish")
    assert p["P1"]["type"] == "close_above" and p["P1"]["expect"] is False   # settles BELOW pp
    assert p["P3"]["expect"] is False                                        # R1 NOT touched


def test_bullish_emits_directional_with_expect_true():
    p = _ptypes("bullish")
    assert p["P1"]["expect"] is True and p["P3"]["expect"] is True


# --------------------------------------------- pending/ abandonment (no infinite retry)
def _pred(rid, wend):
    return json.dumps({"report_id": rid, "window_end_utc": wend,
                       "window_start_utc": wend, "predictions": []})


def test_old_pending_is_abandoned(monkeypatch, tmp_path):
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    monkeypatch.setenv("ASSETFRAME_SANDBOX", "1")
    pend = tmp_path / "pending"; pend.mkdir()
    # window closed 40 days ago -> the feed can't cover it; must be dropped, never re-scored.
    (pend / "AF-20260501-DEAD.json").write_text(_pred("AF-20260501-DEAD", "2026-05-01 21:00"))
    monkeypatch.setattr(RD, "_run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not try to score an abandoned pending prediction")))
    out = RD.score_step(datetime(2026, 6, 27, 4, 0, tzinfo=UTC))
    assert not (pend / "AF-20260501-DEAD.json").exists()
    assert any("abandoned" in s.get("reason", "") for s in out["skipped"])


# --------------------------------------------- DST-correct due-ness (US-zone assets)
def _ny_equity():
    return {"asset_class": "equity", "timezone": "America/New_York", "cadence": "daily", "enabled": True}


def test_us_equity_due_on_winter_monday_0400_utc():
    # 2026-12-07 04:00 UTC is Monday; in EST that is 23:00 SUNDAY locally. The old local-date logic
    # resolved Sunday -> "weekend" -> NO Monday report. UTC-date logic correctly says due.
    due, _why = C.is_due(_ny_equity(), datetime(2026, 12, 7, 4, 0, tzinfo=UTC), holidays={})
    assert due is True


def test_us_equity_not_due_on_winter_saturday_0400_utc():
    # 2026-12-12 04:00 UTC is Saturday; old logic saw EST Friday -> spurious Saturday report.
    due, _why = C.is_due(_ny_equity(), datetime(2026, 12, 12, 4, 0, tzinfo=UTC), holidays={})
    assert due is False


def test_target_date_is_run_utc_date():
    assert C._target_date(_ny_equity(), datetime(2026, 12, 7, 4, 0, tzinfo=UTC)) == \
        datetime(2026, 12, 7, 4, 0, tzinfo=UTC).date()


# --------------------------------------------- blocker-backed reject guard
def test_reject_is_backed():
    assert RD._reject_is_backed({"publish_blockers": ["fabricated level"]}) is True
    assert RD._reject_is_backed({"issues": [{"severity": "blocker", "problem": "x"}]}) is True
    assert RD._reject_is_backed({"decision": "reject", "summary": "feels weak"}) is False
    assert RD._reject_is_backed({"issues": [{"severity": "minor"}]}) is False


# --------------------------------------------- set_config validators
def test_run_timeout_capped_under_systemd_ceiling():
    v = E._CONFIG_VALUE_VALIDATORS["ASSETFRAME_RUN_TIMEOUT"]
    assert v("7200") is True
    assert v("9000") is False        # would risk SIGKILL mid-publish under TimeoutStartSec=10800
    assert v("5400") is True


def test_twelvedata_rate_is_settable_and_validated():
    assert "TWELVEDATA_RATE_PER_MIN" in E._SETTABLE_CONFIG_KEYS
    v = E._CONFIG_VALUE_VALIDATORS["TWELVEDATA_RATE_PER_MIN"]
    assert v("55") and v("0")
    assert not v("-1") and not v("abc")


# --------------------------------------------- Phase-2 CARE: sub-1 level keying at render precision
def test_sub1_levels_not_merged_in_ledger_levels():
    # 0.123443 and 0.123448 COLLAPSE at a fixed 4dp (both -> 0.1234) but are distinct at the render
    # precision (_dp -> 5dp for sub-1). They must both survive — else a real level + setup is dropped.
    by = {k: {"value": v} for k, v in {
        "pp": 0.123443, "r1": 0.123448, "r2": 0.12400,
        "tail_lo": 0.12000, "tail_hi": 0.13000, "swing_lo": 0.11900, "anchor": 0.123443}.items()}
    _preds, ledger_levels = SP.build_predictions_spec(by, {}, "bullish")
    assert 0.123443 in ledger_levels and 0.123448 in ledger_levels


# --------------------------------------------- Phase-2 CARE: calibration excludes freehand-era rows
def test_calibrate_excludes_blank_conf_version_when_version_requested(tmp_path):
    import csv as _csv
    import calibrate as CAL
    p = tmp_path / "ledger.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["conf_version", "conf_raw", "hits", "misses", "horizon"])
        w.writerow(["2", "70", "3", "1", "next_session"])     # current engine -> kept
        w.writerow(["", "55", "2", "2", "next_session"])       # blank (freehand era) -> excluded at v=2
    pts_v2 = CAL.load_points(str(p), conf_version=2)
    assert len(pts_v2) == 1 and pts_v2[0][0] == 70.0
    assert len(CAL.load_points(str(p), conf_version=None)) == 2   # no filter -> both


# --------------------------------------------- _fmt_rr: a missing target reads "n/a", not "below 1.0x"
def test_fmt_rr_missing_target_reads_na():
    s, _m1, m2 = SP._fmt_rr(100.0, 95.0, 110.0, None)     # t2 absent (e.g. long setup with no R1)
    assert "T2 n/a" in s and "below 1.0x" not in s and m2 is None
    s2, _, _ = SP._fmt_rr(100.0, 95.0, 102.0, None)        # t1 reward < risk -> below 1.0x; t2 -> n/a
    assert "T1 below 1.0x" in s2 and "T2 n/a" in s2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok {name}")
            except TypeError:
                print(f"skip {name} (needs fixtures)")
    print("done")
