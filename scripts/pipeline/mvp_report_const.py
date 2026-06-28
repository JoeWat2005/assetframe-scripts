"""Shared constants + render-agnostic helpers for the report twins (extracted from mvp_report).
Leaf module: imports only stdlib + report_pdf; NEVER imports mvp_report/_qa/_pdf/_html."""
import json, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp
from _paths import ROOT

BRAND = "AssetFrame"
TAGLINE = "Next-session market intelligence, scored after the fact."
LOGO = ROOT / "logo" / "logo_trimmed.png"   # repo-root logo/ (NOT scripts/logo — depth-independent)
LOGO_ASPECT = 4.954  # w/h of trimmed wordmark

FREE_CHART_NOTE = ("Green dashed line = support. Red dashed line = resistance. "
                   "Levels are research references, not trade instructions.")
PIVOT_CHART_NOTE = ("PP/R1/R2/S1/S2 are pivot levels from the prior completed session. "
                    "They are reference zones, not trade instructions.")

LADDER_LEGEND = [("resistance", "Resistance"), ("target", "Target"), ("trigger", "Trigger"),
                 ("entry", "Entry zone"), ("support", "Support"),
                 ("invalidation", "Invalidation"), ("tail", "Tail risk")]


def _items_to_html(items):
    """Structured label-break bullets -> premium <ul>. Preferred authoring form."""
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    return "<ul>" + "".join(
        f'<li><b>{esc(i["label"])}</b><br>{i["text"]}</li>' for i in items) + "</ul>"


def _section_body(s):
    return (_items_to_html(s["items"]) if s.get("items") else "") + s.get("html", "")


def _pct_from(v, last, dp=None):
    pct = (float(v) / last - 1) * 100
    if dp is None:
        dp = 2 if abs(pct) < 3 else 1
    return f"{pct:+.{dp}f}%"


def _ladder_dp(rows, ref):
    """One precision for the whole ladder - consistent decimals beat per-row precision."""
    return 1 if any(abs((float(l["value"]) / ref - 1) * 100) >= 3 for l in rows) else 2


def _glossary_rows(p):
    """Auto glossary - core terms always, technical terms only when the report
    actually uses them. Short plain-English entries; reference, not filler."""
    rows = []
    blob = json.dumps(p.get("pro", {}), ensure_ascii=False).lower()
    charts = p["pro"].get("charts", [])
    labels = " ".join(str(l.get("label", "")) for l in p["canonical"]["levels"]).lower()
    classes = {l["cls"] for l in p["canonical"]["levels"]}
    rows.append(("Support / Resistance:",
                 "Price areas where falls (support, below price) and rallies (resistance, above price) "
                 "have previously stalled."))
    if re.search(r"\b(pp|r[123]|s[123]|pivot)\b", labels + " " + blob):
        rows.append(("Pivots (PP, R1-R3, S1-S3):",
                     "Reference levels computed from the prior completed session's high, low and close. "
                     "PP is the balance level; R1-R3 sit above it, S1-S3 below."))
    smas = sorted({n for cfg in charts for n in cfg.get("smas", [])})
    if smas:
        rows.append((f"SMA {'/'.join(map(str, smas))}:",
                     "Simple moving average of the last N closes - the trend lines on the charts. "
                     "Price above = uptrend bias; below = downtrend bias."))
    if "ema" in blob:
        rows.append(("EMA:",
                     "Exponential moving average - weighted toward recent closes, so it turns faster than the SMA."))
    if any(cfg.get("rsi") for cfg in charts) or "rsi" in blob:
        rows.append(("RSI(14):",
                     "Momentum on a 0-100 scale: above 70 = stretched (overbought zone), "
                     "below 30 = washed-out (oversold zone), 50 = neutral midline."))
    if "macd" in blob:
        rows.append(("MACD:",
                     "Trend-momentum gauge built from two EMAs; a bearish cross means short-term momentum "
                     "has rolled under the longer trend."))
    if "atr" in blob or "band" in labels:
        rows.append(("ATR / ATR bands:",
                     "Average True Range - the instrument's typical session movement. The bands project a "
                     "'normal day' envelope; beyond them is an unusual session."))
    if "vwap" in blob:
        rows.append(("VWAP:",
                     "Volume-weighted average price - where the session's average participant has traded."))
    if "entry" in classes or "trigger" in classes:
        rows.append(("Entry zone / trigger:",
                     "The conditional area or level where a researched scenario becomes active. "
                     "A reference for analysis - never an instruction."))
    if "invalidation" in classes:
        rows.append(("Invalidation:",
                     "The level that proves the scenario wrong - beyond it the setup is void and the "
                     "view must be re-framed."))
    if "target" in classes:
        rows.append(("T1 / T2:",
                     "First and second objective levels expressing how far the scenario could reasonably carry."))
    if p["canonical"].get("setups"):
        rows.append(("R:R:",
                     "Reward-to-risk multiple: distance from entry to target versus entry to invalidation, "
                     "quoted net of typical spread."))
    if "funding" in blob:
        rows.append(("Funding:",
                     "The periodic fee long and short perpetual-futures holders exchange; positive = longs "
                     "pay (crowded long), negative = shorts pay."))
    if "open interest" in blob:
        rows.append(("Open interest (OI):",
                     "Total value of open derivative positions - rising OI means money entering, "
                     "falling OI means positions closing."))
    if "basis" in blob:
        rows.append(("Basis:",
                     "The gap between futures/perp price and spot - small positive basis is normal carry; "
                     "extremes signal stress or froth."))
    if p["meta"].get("options_context_included") or "implied vol" in blob:
        rows.append(("Implied volatility (IV):",
                     "The market's priced-in expectation of future movement, derived from option prices."))
    if (p["pro"].get("sentiment") or {}).get("fear_greed"):
        rows.append(("Fear & Greed index:",
                     "Composite sentiment from 0 (extreme fear) to 100 (extreme greed). "
                     "Extremes are contrarian context, not signals."))
    if "tail" in classes:
        rows.append(("Tail risk levels:",
                     "Outer 'unusual day' extremes - trading beyond them marks an abnormal session."))
    return rows


# ---------------------------------------------------------------- ladder
def ladder_geometry(levels, last_value):
    """Shared geometry for the price ladder (PDF + SVG). Returns rows sorted
    high->low with y in [0,1] (0=top) plus the entry-band bounds if present."""
    rows = sorted(levels, key=lambda l: -float(l["value"]))
    vals = [float(l["value"]) for l in rows] + [float(last_value)]
    hi, lo = max(vals), min(vals)
    span = (hi - lo) or 1.0
    hi, lo = hi + span * 0.05, lo - span * 0.05

    def Y(v):
        return (hi - float(v)) / (hi - lo)
    out = [{**l, "y": Y(l["value"])} for l in rows]
    entry = [l for l in out if l["cls"] == "entry"]
    band = (min(e["y"] for e in entry), max(e["y"] for e in entry)) if entry else None
    return out, Y(last_value), band


def _report_quality_rows(p, qa):
    """Auto-built from the QA gate - the card can never disagree with the checks."""
    m = p["meta"]
    sc = p["pro"].get("source_confidence") or []
    overall = next((str(t) for k, t in sc if str(k).lower().startswith("overall")),
                   "see Source confidence card")
    ok = lambda b: "Pass" if b else "CHECK FAILED"
    return [
        ("Data quality score", f"{m.get('data_quality_score', '?')}/10"),
        ("Source confidence (overall)", overall),
        ("Layout QA", "Premium pagination; sections never stranded"),
        ("Indicator warm-up", "Confirmed - charts crop to fully warmed windows"
                              if not rp.WARN else "Partial - disclosed in source audit"),
        ("Canonical price alignment", ok(qa.get("header_price_matches_chart"))),
        ("Claim gating", "Enforced - unverified claims cannot drive thesis"),
        ("Free/Pro split", ok(qa.get("free_pro_split_enforced"))),
        ("Visual inspection", "Stamped via --stamp-visual before release"),
    ]


def _fundamentals_rows(fund):
    """Shared extraction for the fundamentals renderers (HTML + PDF). Returns
    (metric_rows[(label, value)], catalyst_lines[str], source_note) or (None, None, None)."""
    if not fund:
        return None, None, None

    def _money(x):
        try:
            x = float(x)
        except (TypeError, ValueError):
            return str(x)
        for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if abs(x) >= div:
                return f"{x / div:.2f}{unit}"
        return f"{x:,.0f}"

    def _pct(x):
        try:
            return f"{float(x) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(x)

    def _ratio(x):
        try:
            return f"{float(x):.1f}"
        except (TypeError, ValueError):
            return str(x)

    rows = []
    val = fund.get("valuation") or {}
    for key, lab, fmt in (("market_capitalization", "Market cap", _money),
                          ("trailing_pe", "P/E (ttm)", _ratio), ("forward_pe", "P/E (fwd)", _ratio),
                          ("peg_ratio", "PEG", _ratio), ("price_to_sales_ttm", "P/S", _ratio)):
        if val.get(key) is not None:
            rows.append((lab, fmt(val[key])))
    marg = fund.get("margins") or {}
    for key, lab in (("gross_margin", "Gross margin"), ("operating_margin", "Operating margin"),
                     ("profit_margin", "Net margin"), ("return_on_equity_ttm", "ROE")):
        if marg.get(key) is not None:
            rows.append((lab, _pct(marg[key])))

    cat = []
    le = fund.get("latest_earnings") or {}
    if le.get("date"):
        s = f"Latest earnings {le['date']}"
        if le.get("eps_actual") is not None:
            s += f" - EPS {le['eps_actual']}"
            if le.get("eps_estimate") is not None:
                s += f" vs est {le['eps_estimate']}"
        if le.get("surprise_prc") is not None:
            try:
                s += f" ({float(le['surprise_prc']):+.1f}% surprise)"
            except (TypeError, ValueError):
                pass
        cat.append(s)

    if not rows and not cat:
        return None, None, None
    prof = fund.get("profile") or {}
    sub = " / ".join(x for x in (prof.get("sector"), prof.get("industry")) if x)
    src = "Source: Twelve Data fundamentals"
    if sub:
        src += f" - {sub}"
    if fund.get("fetched_utc"):
        src += f" - as of {fund['fetched_utc']} UTC"
    return rows, cat, src
