"""Tests for market_context.py (the daily intermarket 'weather' pack) + its wiring into the brief.

No network: intraday.fetch_chart is monkeypatched to return canned daily rows.

Run:  python -m pytest tests/test_market_context.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_context as MC
import brief_writer as BW


def _rows(prev, last):
    return ({"provider": "fake"}, [{"ts": 1, "c": prev}, {"ts": 2, "c": last}])


def _fake_fetch(moves):
    # moves: {yahoo_symbol: (prev_close, last_close)}; unknown symbols -> raise (omitted)
    def fetch(symbol, interval, rng, **kw):
        if symbol in moves:
            return _rows(*moves[symbol])
        raise RuntimeError("no data")
    return fetch


def test_build_weather_risk_on(monkeypatch):
    monkeypatch.setattr(MC.intraday, "fetch_chart", _fake_fetch({
        "ES=F": (100, 101.0),        # +1.0% futures
        "^VIX": (20, 18.0),          # -10% vol
        "^N225": (100, 101.5),       # +1.5% Asia
        "^HSI": (100, 101.0),
        "DX-Y.NYB": (100, 100.2),
        "CL=F": (100, 99.0),
    }))
    w = MC.build_market_weather()
    assert w["risk_tone"] == "risk-on"
    assert "S&P 500 futures" in w["overnight_recap"]
    assert "Nikkei 225" in w["overnight_recap"]      # overnight/Asia present
    assert "US Dollar Index" in w["macro_drivers"]
    assert w["overnight_recap"]["S&P 500 futures"]["change_pct"] == 1.0


def test_build_weather_risk_off(monkeypatch):
    monkeypatch.setattr(MC.intraday, "fetch_chart", _fake_fetch({
        "ES=F": (100, 98.5),         # -1.5%
        "^VIX": (18, 21.0),          # +16.7% vol spike
        "^N225": (100, 98.0),        # -2% Asia
    }))
    assert MC.build_market_weather()["risk_tone"] == "risk-off"


def test_failed_fetches_omitted_never_raises(monkeypatch):
    monkeypatch.setattr(MC.intraday, "fetch_chart",
                        _fake_fetch({"^VIX": (20, 20.0)}))   # only VIX resolves
    w = MC.build_market_weather()                            # must not raise
    assert "VIX (volatility)" in w["overnight_recap"]
    assert w["macro_drivers"] == {}                          # none of the macro symbols resolved


def test_brief_embeds_weather_and_calendar_directive():
    msg = BW.build_user_message("BTC", {}, {}, None, None, None,
                                market_weather={"risk_tone": "risk-off"})
    assert "market_weather" in msg and "risk-off" in msg
    assert "calendar" in msg.lower()                         # the macro-calendar web_search directive


def test_sandbox_suppresses_weather():
    os.environ["ASSETFRAME_SANDBOX"] = "1"
    try:
        assert BW._load_market_weather() == {}               # no look-ahead in a backtest
    finally:
        os.environ.pop("ASSETFRAME_SANDBOX", None)
