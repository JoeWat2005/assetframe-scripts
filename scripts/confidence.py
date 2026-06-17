"""Deterministic, auditable confidence engine for AssetFrame (Confidence V2).

The analyst EXPLAINS confidence; this module GENERATES it. Same inputs -> same
score, every time. Replaces the freehand "47/80 trimmed to 53/100" scorecard.

Confidence blends four parts, then applies hard caps, then a calibration map:

    raw = 50*market + 30*ledger + 20*catalyst        (each component in 0..1)
        + social_adjustment                          (subtract-only, -10..0)
    capped   = min(raw, <hard caps>)
    published = calibrate(capped)                     (isotonic map; identity early)

  * Market   (analysis + setup): trend, momentum, structure/entry confluence,
             R:R, volatility regime, measured data quality.
  * Ledger   (ledger_context.json): realised hit rate for this prediction type /
             instrument / asset class, shrunk toward 0.5 by sample size.
  * Catalyst (brief + research_pack): claim support + source quality + catalysts.
  * Social   (social_pack, OPTIONAL): crowding / hype / contrarian -> may only
             REDUCE the score, never raise it. Absent -> 0.

Hard caps (take the min): stale data 40 · degraded data 50 ·
single-source/unverified high-impact thesis 55 · hype-driven thesis 55 ·
ledger shows a strong historical failure pattern 55 · cold indicators 60 ·
high-impact catalyst INSIDE the prediction window 60 · engine errors 65.

Every output carries `components` (for the Pro scorecard) and `caps_applied`,
so the published number is fully explainable. Pure stdlib.
"""
from taxonomy import confidence_band

CONF_VERSION = 2                       # bumped from the freehand era (v1) so calibration can filter
WEIGHTS = {"market": 50, "ledger": 30, "catalyst": 20}   # tunable; calibration is ground truth

_CLAIM_STATUS_SCORE = {
    "multiple-source": 1.0, "multi-source": 1.0, "confirmed": 1.0, "official": 1.0,
    "single-source": 0.5, "unverified": 0.25, "stale": 0.3, "unavailable": 0.2,
}
_WEAK_STATUSES = ("single-source", "unverified", "stale")


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _num(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


# --- measured data quality (replaces the hand-set data_quality_score) -------

def compute_dq(analysis, claims=None, options_included=False):
    """0..10 measured data-quality score from the engine analysis + claim sourcing."""
    score = 7
    fr = analysis.get("freshness") or {}
    if analysis.get("degraded"):
        score -= 3
    if fr.get("stale"):
        score -= 2
    age = _num(fr.get("age_minutes"))
    if age is not None and age > 180:
        score -= 1
    warm = (analysis.get("windows") or {}).get("sma_warm_at_display_start") or {}
    if warm and not all(warm.values()):
        score -= 1
    if analysis.get("errors"):
        score -= 2
    if options_included:
        score += 1
    if claims:
        unsupported = sum(1 for c in claims
                          if (c.get("status") or "").lower() in _WEAK_STATUSES + ("unavailable",))
        if unsupported >= 2:
            score -= 1
    return max(0, min(10, score))


# --- market component -------------------------------------------------------

def _default_levels(analysis):
    vals = []
    for v in (analysis.get("pivots_classic") or {}).values():
        if _num(v) is not None:
            vals.append(v)
    for k, v in (analysis.get("atr_day_bands") or {}).items():
        if k != "open" and _num(v) is not None:
            vals.append(v)
    h = analysis.get("hourly") or {}
    for sw in (h.get("swing_highs") or []) + (h.get("swing_lows") or []):
        if _num(sw.get("p")) is not None:
            vals.append(sw["p"])
    return vals


def _trend_score(analysis):
    trend = analysis.get("trend") or {}
    align = (trend.get("alignment") or "").lower()
    lt = (trend.get("long_term_daily") or "").lower()
    if "mixed" in align:
        return 0.4
    if "range" in align:
        return 0.55
    if "uptrend" in lt or "downtrend" in lt:
        return 0.85
    return 0.5


def _momentum_score(analysis, setup):
    side = (setup or {}).get("direction")
    if side not in ("long", "short"):
        return 0.5
    h = analysis.get("hourly") or {}
    d = analysis.get("daily") or {}
    macd = h.get("macd") or {}
    pts = []
    rsi_h = _num(h.get("rsi14"))
    if rsi_h is not None:
        pts.append(_clamp((rsi_h - 40) / 30) if side == "long" else _clamp((60 - rsi_h) / 30))
    cross = macd.get("cross")
    if cross in ("bullish", "bearish"):
        agree = (cross == "bullish") == (side == "long")
        pts.append(0.8 if agree else 0.3)
    hist, hist_prev = _num(macd.get("hist")), _num(macd.get("hist_prev"))
    if hist is not None and hist_prev is not None:
        pts.append(0.65 if abs(hist) > abs(hist_prev) else 0.45)
    rsi_d = _num(d.get("rsi14"))
    if rsi_d is not None:
        pts.append(_clamp((rsi_d - 40) / 30) if side == "long" else _clamp((60 - rsi_d) / 30))
    return sum(pts) / len(pts) if pts else 0.5


def _structure_score(analysis, setup, levels):
    pts = []
    levels = levels if levels is not None else _default_levels(analysis)
    if setup and levels:
        elo, ehi = _num(setup.get("entry_lo")), _num(setup.get("entry_hi"))
        if elo is not None and ehi is not None:
            mid = (elo + ehi) / 2
            atr = _num((analysis.get("daily") or {}).get("atr14")) or 0
            tol = max(abs(ehi - elo), atr * 0.25, mid * 0.001)
            near = sum(1 for v in levels if abs(v - mid) <= tol)
            pts.append(_clamp(near / 3.0))
    inner = _num((analysis.get("stats_last_sessions") or {}).get("close_inside_inner_band_pct"))
    if inner is not None:
        pts.append(_clamp(inner / 100.0))
    return sum(pts) / len(pts) if pts else 0.5


def _rr_score(setup):
    if not setup:
        return 0.5
    entry_lo, entry_hi = _num(setup.get("entry_lo")), _num(setup.get("entry_hi"))
    inval, t1 = _num(setup.get("invalidation")), _num(setup.get("t1"))
    if None in (entry_lo, entry_hi, inval, t1):
        return 0.5
    entry = (entry_lo + entry_hi) / 2
    risk = abs(entry - inval)
    if risk == 0:
        return 0.5
    rr1 = abs(t1 - entry) / risk
    if rr1 >= 2.0:
        return 1.0
    if rr1 >= 1.5:
        return 0.8
    if rr1 >= 1.0:
        return 0.55
    return 0.3


def _vol_score(analysis):
    """Asset-relative volatility normality: current ATR vs the instrument's own
    median session range. ~1x = normal (reliable structure); expanding = lower."""
    d = analysis.get("daily") or {}
    stats = analysis.get("stats_last_sessions") or {}
    atr, med = _num(d.get("atr14")), _num(stats.get("median_session_range"))
    if atr is not None and med and med > 0:
        ratio = atr / med
        return _clamp(1.15 - 0.5 * abs(ratio - 1.0))
    rv = _num(d.get("realized_vol_20d_pct"))
    if rv is not None:
        return _clamp(1.1 - rv / 80.0)
    return 0.5


def market_confidence(analysis, setup, levels=None, options_included=False):
    subs = {
        "trend": _trend_score(analysis),
        "momentum": _momentum_score(analysis, setup),
        "structure": _structure_score(analysis, setup, levels),
        "rr": _rr_score(setup),
        "volatility": _vol_score(analysis),
        "data_quality": compute_dq(analysis, options_included=options_included) / 10.0,
    }
    w = {"trend": 0.22, "momentum": 0.18, "structure": 0.20,
         "rr": 0.16, "volatility": 0.10, "data_quality": 0.14}
    return sum(subs[k] * w[k] for k in subs), subs


# --- ledger component -------------------------------------------------------

def ledger_confidence(ledger_context, pred_type=None):
    """Bayesian blend of realised hit rates (prediction-type / instrument /
    asset-class) with a 0.5 prior pseudo-count, so it shrinks to neutral when
    there is little history. Returns (score 0..1, detail)."""
    if not ledger_context:
        return 0.5, {"reason": "no ledger context (neutral prior)"}
    pthr = ledger_context.get("prediction_type_hit_rates") or {}
    ptc = ledger_context.get("prediction_type_counts") or {}
    candidates = []
    if pred_type and _num(pthr.get(pred_type)) is not None:
        candidates.append(("prediction_type", pthr[pred_type], ptc.get(pred_type, 0)))
    if _num(ledger_context.get("instrument_hit_rate")) is not None:
        candidates.append(("instrument", ledger_context["instrument_hit_rate"],
                           ledger_context.get("historical_prediction_count", 0)))
    if _num(ledger_context.get("asset_class_hit_rate")) is not None:
        candidates.append(("asset_class", ledger_context["asset_class_hit_rate"],
                           ledger_context.get("asset_class_count", 0)))
    if not candidates:
        return 0.5, {"reason": "no rates (neutral prior)"}
    num, den, detail = 0.5, 1.0, []   # prior: pseudo-count 1 at 0.5
    for basis, rate, cnt in candidates:
        r01 = rate / 100.0 if rate > 1 else rate
        cnt = cnt or 0
        num += r01 * cnt
        den += cnt
        detail.append({"basis": basis, "rate": rate, "n": cnt})
    return _clamp(num / den), {"blend": detail}


# --- catalyst component -----------------------------------------------------

def catalyst_confidence(brief, research_pack=None):
    """Thesis support from sourced catalysts/claims. Absence of catalysts is
    neutral (a clean technical call needn't have news), not low."""
    if not brief:
        return 0.5, {"reason": "no brief"}
    pts, detail = [], {}
    claims = brief.get("claims") or []
    if claims:
        supported = []
        for c in claims:
            status = (c.get("status") or "").lower()
            base = _CLAIM_STATUS_SCORE.get(status, 0.5)
            # research_pack present: only DOWNGRADE weakly-sourced thesis claims that
            # aren't traceable to a pack item. A multiple-source/confirmed/official claim
            # already cleared the status gate - never penalise it for a fuzzy string
            # mismatch, or adding the pack would paradoxically LOWER confidence.
            if (research_pack is not None and c.get("used_in_thesis")
                    and status in _WEAK_STATUSES and not _claim_traced(c, research_pack)):
                base = min(base, 0.25)
            supported.append(base)
        pts.append(sum(supported) / len(supported))
        detail["claim_support"] = round(pts[-1], 2)
    gaps = ((brief.get("news_context") or {}).get("source_gaps")
            or brief.get("source_gaps") or [])
    if gaps:
        pts.append(_clamp(1.0 - 0.15 * len(gaps)))
        detail["source_gaps"] = len(gaps)
    return (_clamp(sum(pts) / len(pts)) if pts else 0.5), detail


def _claim_traced(claim, research_pack):
    srcs = []
    for item in (research_pack.get("items") or research_pack.get("sources") or []):
        u = item.get("url") or item.get("source") or ""
        if u:
            srcs.append(u.lower())
    cs = (claim.get("source") or "").lower()
    if not cs:
        return False
    return any(s in cs or cs in s for s in srcs) if srcs else False


# --- social adjustment (subtract-only, optional) ----------------------------

def social_adjustment(social_pack):
    if not social_pack:
        return 0.0, {"reason": "no social data"}
    agg = social_pack.get("aggregate") or {}
    pen, notes = 0.0, []
    hype = (agg.get("hype_risk") or "").lower()
    if hype == "high":
        pen -= 5; notes.append("high hype risk")
    elif hype == "medium":
        pen -= 2
    crowd = (agg.get("crowding_risk") or "").lower()
    if crowd == "high":
        pen -= 3; notes.append("high crowding risk")
    elif crowd == "medium":
        pen -= 1
    if agg.get("contrarian_warning"):
        pen -= 2; notes.append("contrarian warning")
    return max(-10.0, pen), {"penalty": pen, "notes": notes}


# --- caps -------------------------------------------------------------------

def _has_unsupported_thesis(brief):
    for c in ((brief or {}).get("claims") or []):
        if c.get("used_in_thesis") and (c.get("status") or "").lower() in _WEAK_STATUSES:
            return True
    return False


def _hype_thesis(brief, social_pack):
    if not social_pack:
        return False
    if (social_pack.get("aggregate") or {}).get("hype_risk", "").lower() != "high":
        return False
    return bool(((brief or {}).get("social_context") or {}).get("drives_thesis"))


def _ledger_failure(ledger_context, pred_type):
    if not ledger_context or not pred_type:
        return False
    rate = (ledger_context.get("prediction_type_hit_rates") or {}).get(pred_type)
    cnt = (ledger_context.get("prediction_type_counts") or {}).get(pred_type, 0)
    if _num(rate) is None or cnt < 5:
        return False
    return (rate / 100.0 if rate > 1 else rate) < 0.4


def _in_window_event(brief):
    """A scheduled high-impact catalyst INSIDE the prediction window widens the
    outcome distribution no matter how well-sourced it is - you cannot be highly
    confident across a binary event. Gated on in_window AND gap_risk so a routine
    in-window item (e.g. an unconfirmed minor print) doesn't trip the cap; an event
    that is merely well-sourced but OUT of window (the next session's risk) does not."""
    for c in ((brief or {}).get("catalysts") or []):
        if c.get("in_window") and c.get("gap_risk"):
            return True
    return False


# --- calibration map --------------------------------------------------------

def _apply_calibration(score, calib):
    """Piecewise-linear interpolation through the isotonic knots written by
    calibrate.py. No map (or <2 knots) -> identity."""
    if not calib:
        return score
    knots = calib.get("knots") or []
    if len(knots) < 2:
        return score
    xs = [k[0] for k in knots]
    ys = [k[1] for k in knots]
    if score <= xs[0]:
        return ys[0]
    if score >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if score <= xs[i]:
            x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
            return y0 + (y1 - y0) * ((score - x0) / (x1 - x0)) if x1 > x0 else y0
    return score


# --- public entry point -----------------------------------------------------

def compute_confidence(analysis, setup, brief=None, research_pack=None,
                       social_pack=None, ledger_context=None, calib=None,
                       options_included=False, levels=None):
    pred_type = ((brief or {}).get("primary_prediction") or {}).get("type")
    m, m_sub = market_confidence(analysis, setup, levels=levels, options_included=options_included)
    l, l_sub = ledger_confidence(ledger_context, pred_type)
    c, c_sub = catalyst_confidence(brief, research_pack)
    s_adj, s_sub = social_adjustment(social_pack)

    raw = WEIGHTS["market"] * m + WEIGHTS["ledger"] * l + WEIGHTS["catalyst"] * c
    raw = _clamp(raw + s_adj, 0, 100)

    cap, caps = 100, []
    fr = analysis.get("freshness") or {}
    if analysis.get("degraded"):
        cap = min(cap, 50); caps.append("degraded_data->50")
    if fr.get("stale"):
        cap = min(cap, 40); caps.append("stale_data->40")
    warm = (analysis.get("windows") or {}).get("sma_warm_at_display_start") or {}
    if warm and not all(warm.values()):
        cap = min(cap, 60); caps.append("cold_indicators->60")
    if analysis.get("errors"):
        cap = min(cap, 65); caps.append("engine_errors->65")
    if _has_unsupported_thesis(brief):
        cap = min(cap, 55); caps.append("single_source_thesis->55")
    if _hype_thesis(brief, social_pack):
        cap = min(cap, 55); caps.append("hype_driven_thesis->55")
    if _ledger_failure(ledger_context, pred_type):
        cap = min(cap, 55); caps.append("ledger_failure_pattern->55")
    if _in_window_event(brief):
        cap = min(cap, 60); caps.append("in_window_event->60")

    capped = min(raw, cap)
    published = int(round(_clamp(_apply_calibration(capped, calib), 0, 100)))

    components = [
        {"name": "Market", "weight": WEIGHTS["market"], "score": round(m, 3), "detail": m_sub},
        {"name": "Ledger", "weight": WEIGHTS["ledger"], "score": round(l, 3), "detail": l_sub},
        {"name": "Catalyst", "weight": WEIGHTS["catalyst"], "score": round(c, 3), "detail": c_sub},
        {"name": "Social adj.", "weight": 0, "score": round(s_adj, 1), "detail": s_sub},
    ]
    return {
        "market": round(m, 3), "ledger": round(l, 3), "catalyst": round(c, 3),
        "social_adj": round(s_adj, 1), "raw": round(raw, 1), "capped": round(capped, 1),
        "published": published, "band": confidence_band(published),
        "caps_applied": caps, "components": components,
        "calibrated": bool(calib), "conf_version": CONF_VERSION,
    }


if __name__ == "__main__":
    import json
    from pathlib import Path
    a = json.loads(Path("data/analysis/AAPL_analysis.json").read_text(encoding="utf-8-sig"))
    setup = {"direction": "long", "entry_lo": 287.38, "entry_hi": 288.12,
             "invalidation": 285.11, "t1": 292.63, "t2": 295.64}
    brief = {
        "primary_prediction": {"type": "range_hold"},
        "claims": [{"claim": "WWDC Siri AI, no ship date", "status": "multiple-source",
                    "used_in_thesis": True, "source": "CNBC + NPR"}],
        "news_context": {"source_gaps": ["options IV", "short interest"]},
    }
    print("--- no ledger/social/calib (Phase 1 baseline) ---")
    print(json.dumps(compute_confidence(a, setup, brief), indent=1))
    print("--- with ledger context + high social hype ---")
    lc = {"historical_prediction_count": 14, "instrument_hit_rate": 71,
          "asset_class_hit_rate": 58, "asset_class_count": 40,
          "prediction_type_hit_rates": {"range_hold": 65}, "prediction_type_counts": {"range_hold": 9}}
    sp = {"aggregate": {"hype_risk": "high", "crowding_risk": "medium",
                        "contrarian_warning": "crowded long"}}
    print(json.dumps(compute_confidence(a, setup, brief, ledger_context=lc, social_pack=sp), indent=1))
