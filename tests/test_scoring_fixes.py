"""Tests for the live-scoring fixes: crypto daily window closes at a fixed 21:00 UTC (so it grades
the next morning before the per-ticker file is overwritten), and the score-time candle refresh only
re-fetches assets that actually have a pending CLOSED window.

Run:  python -m pytest tests/test_scoring_fixes.py -q
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sessions
import run_daily as RD

UTC = timezone.utc


# --------------------------------------------------------------- crypto window

def test_crypto_daily_window_closes_2100_utc():
    # A Saturday 04:00 report must close the SAME day 21:00 UTC (well before the next 04:00 run),
    # not now+24h (which would close after the next run and never grade).
    w = sessions.get_cadence_window("crypto_24_7", "daily", now=datetime(2026, 6, 27, 4, 0, tzinfo=UTC))
    assert w["window_end_utc"] == "2026-06-27 21:00"
    assert w["window_start_utc"] == "2026-06-27 04:00"
    assert w["scored_cadence"] == "daily"


def test_crypto_window_rolls_to_next_day_when_past_close():
    # A report generated AFTER 21:00 targets the next day's 21:00 (never a near-zero window).
    w = sessions.get_cadence_window("crypto_24_7", "daily", now=datetime(2026, 6, 27, 22, 0, tzinfo=UTC))
    assert w["window_end_utc"] == "2026-06-28 21:00"


def test_noncrypto_daily_window_unchanged():
    # The crypto retarget must not touch equity/fx windows (regression guard).
    w = sessions.get_cadence_window("nyse_equity", "daily",
                                    now=datetime(2026, 6, 26, 4, 0, tzinfo=UTC)) \
        if "nyse_equity" in sessions.PROFILES else None
    if w is not None:
        assert w["scored_cadence"] == "daily"
        assert "21:00 UTC daily close" not in w.get("window_label", "")


# --------------------------------------------------------------- score-time candle refresh

def _asset(tk):
    return {"ticker": tk, "session_profile": "crypto_24_7", "roll_utc": 0,
            "provider_symbols": {"yahoo": f"{tk}-USD"}}


def test_refresh_only_closed_window_assets(monkeypatch, tmp_path):
    now = datetime(2026, 6, 27, 4, 0, tzinfo=UTC)
    # BTC window CLOSED (yesterday 21:00); ETH window still OPEN (later today) -> only BTC refreshed.
    (tmp_path / "BTC_predictions.json").write_text(json.dumps(
        {"report_id": "AF-20260626-BTC", "window_end_utc": "2026-06-26 21:00"}))
    (tmp_path / "ETH_predictions.json").write_text(json.dumps(
        {"report_id": "AF-20260627-ETH", "window_end_utc": "2026-06-27 21:00"}))
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    calls = []
    monkeypatch.setattr(RD, "_run", lambda cmd, timeout=180: (calls.append(cmd), ("", ""))[1] or (True, "", ""))

    refreshed = RD._refresh_candles_for_scoring(now, [_asset("BTC"), _asset("ETH")])
    assert refreshed == ["BTC"]                          # ETH window still open -> not refreshed
    assert len(calls) == 1 and "--name" in calls[0] and "BTC" in calls[0]


def test_refresh_noop_when_no_closed_windows(monkeypatch, tmp_path):
    now = datetime(2026, 6, 27, 4, 0, tzinfo=UTC)
    (tmp_path / "BTC_predictions.json").write_text(json.dumps(
        {"report_id": "AF-20260627-BTC", "window_end_utc": "2026-06-27 21:00"}))   # open
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    monkeypatch.setattr(RD, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh expected")))
    assert RD._refresh_candles_for_scoring(now, [_asset("BTC")]) == []


# --------------------------------------------------------------- pending retry ("score it next run")

def _pred(rid, wend):
    return json.dumps({"report_id": rid, "window_end_utc": wend, "predictions": []})


def test_open_window_preserved_to_pending(monkeypatch, tmp_path):
    # An open-window prediction is copied to pending/ so the next generation's overwrite can't lose it.
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    monkeypatch.setenv("ASSETFRAME_SANDBOX", "1")          # skip the memory-refresh subprocesses
    (tmp_path / "BTC_predictions.json").write_text(_pred("AF-20260627-BTC", "2026-06-27 21:00"))
    monkeypatch.setattr(RD, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no score for open window")))
    out = RD.score_step(datetime(2026, 6, 27, 4, 0, tzinfo=UTC))   # before 21:00 -> open
    assert (tmp_path / "pending" / "AF-20260627-BTC.json").exists()
    assert any("still open" in s.get("reason", "") for s in out["skipped"])


def test_pending_scored_then_deleted(monkeypatch, tmp_path):
    # A preserved prediction whose window has now closed grades on the NEXT run, then pending is cleaned.
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    monkeypatch.setenv("ASSETFRAME_SANDBOX", "1")
    pend = tmp_path / "pending"; pend.mkdir()
    (pend / "AF-20260627-BTC.json").write_text(_pred("AF-20260627-BTC", "2026-06-27 21:00"))  # closed
    monkeypatch.setattr(RD, "_run", lambda cmd, **k: (True, '{"hit_rate_pct": 50.0}', ""))
    out = RD.score_step(datetime(2026, 6, 28, 4, 0, tzinfo=UTC))   # Sunday, window closed
    assert not (pend / "AF-20260627-BTC.json").exists()            # graded -> removed
    assert any(s["report_id"] == "AF-20260627-BTC" for s in out["scored"])


def test_failed_score_preserved_for_retry(monkeypatch, tmp_path):
    # A transient scoring failure must keep the prediction in pending/ to retry next run.
    monkeypatch.setattr(RD, "PRED_DIR", tmp_path)
    monkeypatch.setenv("ASSETFRAME_SANDBOX", "1")
    (tmp_path / "BTC_predictions.json").write_text(_pred("AF-20260627-BTC", "2026-06-27 21:00"))
    monkeypatch.setattr(RD, "_run", lambda cmd, **k: (False, "", "transient fetch error"))
    out = RD.score_step(datetime(2026, 6, 28, 4, 0, tzinfo=UTC))   # closed but scoring fails
    assert (tmp_path / "pending" / "AF-20260627-BTC.json").exists()   # kept for retry
    assert any(e["report_id"] == "AF-20260627-BTC" for e in out["errors"])
