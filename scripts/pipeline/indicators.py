"""Pure technical-indicator + level math, extracted from intraday.py.

No I/O and no engine state — lists/dicts in, numbers out — so this is unit-testable in isolation and
reusable (intraday re-exports these; market_context uses them). The math is byte-for-byte identical
to the original inline block (golden-file tested)."""


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def rsi14(closes):
    n = 14
    if len(closes) <= n:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    ag = sum(max(d, 0) for d in deltas[:n]) / n
    al = sum(max(-d, 0) for d in deltas[:n]) / n
    for d in deltas[n:]:
        ag = (ag * 13 + max(d, 0)) / 14
        al = (al * 13 + max(-d, 0)) / 14
    return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 1)


def atr14(rows):
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:14]) / 14
    for t in trs[14:]:
        a = (a * 13 + t) / 14
    return a


def macd(closes):
    e12, e26 = ema_series(closes, 12), ema_series(closes, 26)
    if not e26:
        return None
    line = [a - b for a, b in zip(e12[-len(e26):], e26)]
    sig = ema_series(line, 9)
    if not sig:
        return None
    hist = [a - b for a, b in zip(line[-len(sig):], sig)]
    return {"macd": round(line[-1], 6), "signal": round(sig[-1], 6), "hist": round(hist[-1], 6),
            "hist_prev": round(hist[-2], 6) if len(hist) > 1 else None,
            "cross": ("bullish" if line[-1] > sig[-1] else "bearish")}


def classify_long_term(closes, s50, s200, lookback=20):
    """Daily regime: Uptrend / Downtrend / Range from price vs SMA200, SMA50/200 cross, slope."""
    if s200 is None or s50 is None:
        return "Insufficient data"
    last = closes[-1]
    slope_up = len(closes) > lookback and closes[-1] > closes[-1 - lookback]
    votes = int(last > s200) + int(s50 > s200) + int(slope_up)
    if votes >= 2 and last > s200:
        return "Uptrend"
    if votes <= 1 and last < s200:
        return "Downtrend"
    return "Range"


def classify_intraday_trend(hc, s20, s50, e9, e21):
    votes = 0
    votes += int(s50 is not None and hc[-1] > s50)
    votes += int(s20 is not None and hc[-1] > s20)
    votes += int(e9 is not None and e21 is not None and e9 > e21)
    votes += int(len(hc) > 24 and hc[-1] > hc[-25])
    if votes >= 3:
        return "Uptrend"
    if votes <= 1:
        return "Downtrend"
    return "Range"


def alignment_verdict(lt, it):
    if lt == it and lt in ("Uptrend", "Downtrend"):
        return "aligned-" + ("up" if lt == "Uptrend" else "down")
    if lt == "Range":
        return "mixed (long-term range)"
    if it == "Range":
        return "mixed (intraday range)"
    return "counter-trend (intraday against long-term)"


def level_stats(sessions_sorted, atr_d, max_n=120):
    """Empirical band-containment and pivot-touch rates over completed sessions.
    Approximation: uses the CURRENT ATR for all historical bands (documented)."""
    comp = sessions_sorted[:-1]  # exclude in-progress session
    pairs = list(zip(comp, comp[1:]))[-max_n:]
    if not pairs or not atr_d:
        return None
    inner = outer = pp_t = r1_t = s1_t = 0
    ranges = []
    for prev, cur in pairs:
        pp = (prev["h"] + prev["l"] + prev["c"]) / 3
        r1, s1 = 2 * pp - prev["l"], 2 * pp - prev["h"]
        move = abs(cur["c"] - prev["c"])  # net session move (prior close ~ session open in 24h markets)
        inner += move <= 0.5 * atr_d
        outer += move <= 1.0 * atr_d
        pp_t += cur["l"] <= pp <= cur["h"]
        r1_t += cur["h"] >= r1
        s1_t += cur["l"] <= s1
        ranges.append(cur["h"] - cur["l"])
    n = len(pairs)
    ranges.sort()
    return {
        "sessions_evaluated": n,
        "close_inside_inner_band_pct": round(100 * inner / n, 1),
        "close_inside_outer_band_pct": round(100 * outer / n, 1),
        "touched_PP_pct": round(100 * pp_t / n, 1),
        "touched_R1_pct": round(100 * r1_t / n, 1),
        "touched_S1_pct": round(100 * s1_t / n, 1),
        "median_session_range": round(ranges[n // 2], 6),
        "note": "containment = |close - prior close| vs current ATR(14) bands (approximation); touches use session H/L vs prior-session pivots",
    }


def swings(rows, k=2):
    """Confirmed swing highs/lows (fractal, k bars either side)."""
    hi, lo = [], []
    for i in range(k, len(rows) - k):
        win = rows[i - k:i + k + 1]
        if rows[i]["h"] == max(r["h"] for r in win):
            hi.append({"t": rows[i]["ts"], "p": rows[i]["h"]})
        if rows[i]["l"] == min(r["l"] for r in win):
            lo.append({"t": rows[i]["ts"], "p": rows[i]["l"]})
    return hi[-5:], lo[-5:]


def compute_pivots_bands(prior_hlc, anchor_close, atr_daily):
    """Shared floor-pivots + ATR day-band math for both the live and anchored paths.

    prior_hlc    dict/mapping with "h","l","c" of the session the pivots derive from
                 (live path: prior completed session; anchored path: chosen session).
    anchor_close band anchor: the live path passes TODAY'S session open; the anchored
                 paths pass the chosen completed session's close. None => no bands.
    atr_daily    daily ATR(14); None/0 => no bands.

    Returns (pivots_dict | None, bands_dict | None), UNROUNDED — callers round/tag
    exactly as before. Math is identical to the original inline block so swapping the
    live path to this helper is byte-for-byte (see the golden-file tests).
    """
    pivots = None
    if prior_hlc:
        pp = (prior_hlc["h"] + prior_hlc["l"] + prior_hlc["c"]) / 3
        rng = prior_hlc["h"] - prior_hlc["l"]
        pivots = {"PP": pp, "R1": 2 * pp - prior_hlc["l"], "S1": 2 * pp - prior_hlc["h"],
                  "R2": pp + rng, "S2": pp - rng,
                  "R3": prior_hlc["h"] + 2 * (pp - prior_hlc["l"]),
                  "S3": prior_hlc["l"] - 2 * (prior_hlc["h"] - pp)}
    bands = None
    if atr_daily and anchor_close is not None:
        bands = {"open": anchor_close,
                 "inner_hi": anchor_close + 0.5 * atr_daily,
                 "inner_lo": anchor_close - 0.5 * atr_daily,
                 "outer_hi": anchor_close + 1.0 * atr_daily,
                 "outer_lo": anchor_close - 1.0 * atr_daily}
    return pivots, bands
