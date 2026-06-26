"""market_context.py — the daily "market weather" pack.

A SHARED intermarket + overnight-session snapshot, built once per run and fed to EVERY brief, so each
call is anchored to the macro backdrop instead of reading its own chart in isolation — exactly what a
pre-open morning note does ("what are Asian markets / the dollar / yields saying about today?").

It fetches a fixed set of context instruments through the SAME provider chain intraday.py uses
(Yahoo-first, keyless), computes each one's last close + daily % change, and derives:
  * a one-line RISK TONE (risk-on / risk-off / mixed) from US futures + VIX + the Asian session,
  * an OVERNIGHT RECAP (Asia + US futures = the tone heading into the European/US session),
  * the MACRO DRIVERS (dollar, 10Y yield, oil).

Written to data/market_weather.json. The brief writer reads it (brief_writer._load_market_weather)
and weighs the backdrop into the thesis; the day's scheduled CATALYST calendar is gathered by the
brief's own web_search (no calendar API needed). LIVE only — a backdated/sandbox run must not fetch
CURRENT context (look-ahead), so run_daily skips building it and the brief loader returns {}.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from _paths import ROOT
import intraday

# (yahoo symbol, human label, group). Groups drive the recap framing.
CONTEXT_TICKERS = [
    ("ES=F",     "S&P 500 futures",    "risk"),       # US risk appetite into the open
    ("NQ=F",     "Nasdaq 100 futures", "risk"),
    ("^VIX",     "VIX (volatility)",   "risk"),        # fear gauge
    ("^N225",    "Nikkei 225",         "overnight"),   # Asian session tone
    ("^HSI",     "Hang Seng",          "overnight"),
    ("DX-Y.NYB", "US Dollar Index",    "macro"),       # drives FX, gold, commodities
    ("^TNX",     "US 10Y yield",       "macro"),       # drives equities, gold, risk
    ("CL=F",     "WTI crude",          "macro"),       # growth / inflation barometer
]
OUT = ROOT / "data" / "market_weather.json"


def _daily_change(symbol):
    """Last close + prior close + % change from a short daily series. None on any failure (a missing
    instrument is omitted, never fatal)."""
    try:
        _meta, rows = intraday.fetch_chart(symbol, "1d", "5d")
    except Exception:
        return None
    closes = [r.get("c") for r in (rows or []) if r.get("c") is not None]
    if len(closes) < 2:
        return None
    last, prev = closes[-1], closes[-2]
    chg = ((last - prev) / prev * 100) if prev else 0.0
    return {"last": round(last, 4), "prev_close": round(prev, 4), "change_pct": round(chg, 2)}


def _risk_tone(items):
    """One-line risk read from US futures + VIX + the Asian average (best-effort heuristic)."""
    es = items.get("S&P 500 futures", {}).get("change_pct")
    vix = items.get("VIX (volatility)", {}).get("change_pct")
    asia = [items[k]["change_pct"] for k in ("Nikkei 225", "Hang Seng") if k in items]
    asia_avg = (sum(asia) / len(asia)) if asia else None
    votes = 0
    if es is not None:
        votes += 1 if es > 0.15 else (-1 if es < -0.15 else 0)
    if asia_avg is not None:
        votes += 1 if asia_avg > 0.2 else (-1 if asia_avg < -0.2 else 0)
    if vix is not None:
        votes += 1 if vix < -2 else (-1 if vix > 2 else 0)
    return "risk-on" if votes >= 1 else ("risk-off" if votes <= -1 else "mixed/neutral")


def build_market_weather(now=None):
    """Fetch the context instruments + assemble the weather pack. Best-effort; never raises."""
    now = now or datetime.now(timezone.utc)
    items = {}
    for sym, label, group in CONTEXT_TICKERS:
        d = _daily_change(sym)
        if d:
            items[label] = {**d, "group": group, "symbol": sym}
    return {
        "as_of_utc": now.strftime("%Y-%m-%d %H:%M"),
        "risk_tone": _risk_tone(items),
        # Asia + US futures = the tone heading into the session (the "overnight recap").
        "overnight_recap": {k: v for k, v in items.items() if v["group"] in ("overnight", "risk")},
        # dollar / yields / oil = the cross-asset drivers.
        "macro_drivers": {k: v for k, v in items.items() if v["group"] == "macro"},
        "note": ("Daily % change vs prior close (^TNX is the CBOE 10Y yield index, i.e. 42.5 = "
                 "4.25%). Tilt the call with this backdrop — risk-on/off, the dollar, yields, vol, "
                 "the Asian session. Then research TODAY's scheduled high-impact macro events (the "
                 "calendar) via web_search and weigh them as catalysts/risks."),
    }


def main(argv=None):
    weather = build_market_weather()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(weather, ensure_ascii=False, indent=1), encoding="utf-8")
    n = len(weather.get("overnight_recap", {})) + len(weather.get("macro_drivers", {}))
    print(json.dumps({"ok": True, "out": str(OUT), "instruments": n,
                      "risk_tone": weather.get("risk_tone")}))


if __name__ == "__main__":
    main()
