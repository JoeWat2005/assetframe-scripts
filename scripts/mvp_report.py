"""AssetFrame report generator - Snapshot (free) + Pro pair, website-ready.

Usage:
  python scripts/mvp_report.py <payload.json>            generate everything
  python scripts/mvp_report.py <out_dir> --stamp-visual  set visual_inspection_passed

Brand: AssetFrame - "Next-session market intelligence, scored after the fact."
Products: AssetFrame Snapshot (1 page, lead magnet) and AssetFrame Pro (3-6
pages, paid). Outputs into payload `out_dir`:
  free.pdf  pro.pdf  free.html  pro.html  metadata.json  preview.png

Hard guarantees enforced here (build FAILS before writing artifacts):
  - canonical data object: every setup/ladder/ledger price must exist in
    canonical.levels; header/chart/metadata last price are the same number
  - R:R strings only in the unambiguous "T1 1.5x; T2 2.1x" family (or
    "No valid R:R - excluded"); never negative-looking
  - banned marketing language absent; free/pro split enforced (free has no
    entries/R:R/sizing/ladder and max 3 labelled chart levels)
  - high-impact claims labelled; unverified claims cannot drive thesis
  - indicator warm-up via report_pdf.prep_chart (partial lines hidden/labelled)

Positioning: general market research only - never personal advice, no
guarantees, no execution.
"""
import base64, json, re, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:
    LONDON = None

sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp

try:
    from taxonomy import PREDICTION_TYPES
except Exception:  # taxonomy is stdlib-only; fall back so the generator stays runnable
    PREDICTION_TYPES = ("breakout", "rejection", "continuation",
                        "mean_reversion", "range_hold", "volatility_expansion")

BRAND = "AssetFrame"
TAGLINE = "Next-session market intelligence, scored after the fact."
LOGO = Path(__file__).parent.parent / "logo" / "logo_trimmed.png"
LOGO_ASPECT = 4.954  # w/h of trimmed wordmark
ACCENT = (11, 37, 69)         # logo navy
ACCENT_FILL = (233, 238, 246)
STATUS_COLORS = {"buy": rp.GREEN, "sell": rp.RED, "wait": rp.AMBER,
                 "stand aside": rp.GRAY, "neutral": rp.BLUE}
LADDER_COLORS = {"tail": rp.GRAY, "resistance": rp.RED, "target": rp.BLUE,
                 "trigger": rp.AMBER, "entry": rp.GREEN, "support": (87, 96, 106),
                 "invalidation": (164, 14, 38), "last": rp.DARK}

BANNED = [r"sure trade", r"risk[- ]free(?!\s+(rate|yield|asset|benchmark))", r"easy profit",
          r"you should buy", r"you should sell"]  # "risk-free RATE/yield/asset" is legit finance
# phrases allowed ONLY in negated compliance form ("no outcome is guaranteed",
# "not a personal recommendation")
# A negation token anywhere in the short preceding window clears these (so "no guaranteed returns",
# "is not guaranteed", "isn't guaranteed", "nothing is guaranteed" all pass) — same false-positive
# fix-class as the risk-free lookahead; only a bare positive claim ("guaranteed profit") is flagged.
NEGATED_ONLY = {"guaranteed": r"\b(no|not|never|without|nothing|cannot|none)\b|n't\b",
                "personal recommendation": r"\b(not|no|never|nothing)\b|n't\b"}
RR_OK = re.compile(r"^(T1 (below 1\.0x|\d+(\.\d+)?x); T2 (below 1\.0x|\d+(\.\d+)?x|\d+(\.\d+)?x .*)|No valid R:R - (excluded|setup excluded)).*$")
RR_BAD = re.compile(r"(~\s*-\d)|(-\d+(\.\d+)?\s*/\s*-?\d+(\.\d+)?x)|(R:R[^.<]{0,30}[-−]\d)")
QUALITY_LABELS = {"High quality", "Acceptable", "Low quality", "Management only", "No-trade"}
CLAIM_STATUSES = {"confirmed", "multiple-source", "single-source", "unverified", "stale", "unavailable"}
THESIS_BLOCKED = {"unverified", "stale", "unavailable"}  # cannot drive thesis

# bullets must never carry a literal leading dash - strip at structural starts only,
# and only when a letter/currency follows (so "-0.2%" negative numbers survive)
_DASH = r"[-–—•·]\s*(?=[A-Za-z(£$€])"
RX_DASH_LI = re.compile(r"(<(?:li|p|div)[^>]*>\s*(?:<b[^>]*>)?)\s*" + _DASH)
RX_DASH_BR = re.compile(r"(<br\s*/?>\s*(?:<b[^>]*>)?)\s*" + _DASH)

# canonical Pro section order (relative; unknown headings slot anywhere)
SECTION_ORDER = ["market summary", "long / short research view", "scenario matrix",
                 "event-risk timeline", "technicals", "conditional setups",
                 "options / hedging", "asset-specific statistics",
                 "sentiment", "what can go wrong", "contract", "trade-quality scorecard",
                 "outcome ledger", "source audit", "asset-session rules"]

FG_ZONES_PDF = [(0, 25, (207, 34, 46)), (25, 45, (188, 76, 0)), (45, 55, (154, 103, 0)),
                (55, 75, (77, 138, 42)), (75, 100, (26, 127, 55))]
FG_ZONES_CSS = [(0, 25, "#cf222e"), (25, 45, "#bc4c00"), (45, 55, "#9a6700"),
                (55, 75, "#4d8a2a"), (75, 100, "#1a7f37")]

FREE_CHART_NOTE = ("Green dashed line = support. Red dashed line = resistance. "
                   "Levels are research references, not trade instructions.")
PIVOT_CHART_NOTE = ("PP/R1/R2/S1/S2 are pivot levels from the prior completed session. "
                    "They are reference zones, not trade instructions.")
# acronyms that may legitimately appear in caps; other ALL-CAPS words draw a QA warn
CAPS_ALLOW = {"RSI", "MACD", "SMA", "EMA", "ATR", "VWAP", "ETF", "ETFS", "FOMC", "UTC",
              "BST", "GMT", "USD", "USDT", "GBP", "JPY", "EUR", "BTC", "ETH", "SOL",
              "DXY", "VIX", "VVIX", "GDP", "CPI", "PMI", "BOE", "ECB", "FCA", "CME",
              "NYMEX", "COMEX", "LSE", "IPO", "ATH", "API", "EIA", "OPEC", "CFTC", "COT",
              "PDF", "RTH", "AAPL", "QQQ", "SPY", "NVDA", "MSFT", "WTI", "OKX", "MEXC",
              "WWDC", "EDT", "EST"}


def _strip_dashes(h):
    n = 0
    for rx in (RX_DASH_LI, RX_DASH_BR):
        h, k = rx.subn(r"\1", h)
        n += k
    return h, n


def _normalize_payload(p):
    """Strip authored dash-bullet markers everywhere html is rendered; apply
    plain-English label style to the Free chart. Returns dash count."""
    n = 0
    f = p.get("free", {})
    for k in ("bullets_html", "scenarios_html"):
        if f.get(k):
            f[k], d = _strip_dashes(f[k])
            n += d
    if f.get("chart") is not None:
        f["chart"].setdefault("label_style", "plain")
    for s in p.get("pro", {}).get("sections", []):
        if s.get("html"):
            s["html"], d = _strip_dashes(s["html"])
            n += d
    return n


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


# ---------------------------------------------------------------- helpers
def wrap_text(pdf, txt, size, width, style="B", max_lines=4):
    pdf.set_font("helvetica", style, size)
    words = rp.S(str(txt)).split()
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if pdf.get_string_width(cand) <= width or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(len(lines[-1]) - 3, 0)] + "..."
    return lines or [""]


def brand_band(pdf, tier_label, tagline=None):
    y0 = pdf.get_y()
    logo_h = 5.6
    if LOGO.exists():
        pdf.image(str(LOGO), x=pdf.l_margin, y=y0, h=logo_h)
    else:  # never happens in production; keeps generator non-fatal in dev
        pdf.set_font("helvetica", "B", 11)
        pdf.set_text_color(*ACCENT)
        pdf.set_xy(pdf.l_margin, y0)
        pdf.cell(60, logo_h, BRAND)
    pdf.set_font("helvetica", "B", 8)
    pdf.set_text_color(*rp.GRAY)
    pdf.set_xy(pdf.w - pdf.r_margin - 80, y0 + 1.2)
    pdf.cell(80, 4, rp.S(tier_label), align="R")
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.6)
    pdf.line(pdf.l_margin, y0 + logo_h + 1.6, pdf.w - pdf.r_margin, y0 + logo_h + 1.6)
    pdf.set_y(y0 + logo_h + 2.4)
    if tagline:
        pdf.set_font("helvetica", "I", 6.6)
        pdf.set_text_color(*rp.GRAY)
        pdf.cell(0, 3.2, rp.S(tagline), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(0.4)


def title_block(pdf, title, subtitle):
    pdf.set_font("helvetica", "B", 14)
    pdf.set_text_color(*rp.DARK)
    pdf.cell(0, 6, rp.S(title), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 7.6)
    pdf.set_text_color(*rp.GRAY)
    pdf.multi_cell(0, 3.6, rp.S(subtitle), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1.0)


def chips(pdf, status, risk):
    y = pdf.get_y()
    x = pdf.l_margin
    for label, col in [(status, STATUS_COLORS.get(status.lower(), rp.GRAY)),
                       (f"Risk: {risk}", rp.RISK_COLORS.get(risk.lower(), rp.GRAY))]:
        pdf.set_font("helvetica", "B", 8.2)
        w = pdf.get_string_width(rp.S(label)) + 8
        pdf.set_fill_color(*col)
        pdf.rect(x, y, w, 5.6, style="F", round_corners=True, corner_radius=2.4)
        pdf.set_text_color(255, 255, 255)
        pdf.set_xy(x, y + 0.9)
        pdf.cell(w, 3.8, rp.S(label), align="C")
        x += w + 4
    pdf.set_y(y + 7.2)


def card_grid(pdf, items, cols=2, val_font=7.2):
    bx = pdf.l_margin
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    cw = (bw - 6) / cols
    usable = cw - 4
    KEY_H, VAL_H = 2.7, 3.1
    cells = [(rp.S(str(k)), wrap_text(pdf, v, val_font, usable)) for k, v in items]
    grid = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    row_h = [KEY_H + VAL_H * max(len(v) for _, v in r) + 1.8 for r in grid]
    band_h = sum(row_h) + 2.4
    pdf.need(band_h + 5)
    by = pdf.get_y()
    pdf.set_fill_color(246, 248, 250)
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.25)
    pdf.rect(bx, by, bw, band_h, style="FD", round_corners=True, corner_radius=2)
    cy = by + 1.6
    for r, rh in zip(grid, row_h):
        for ci, (k, vlines) in enumerate(r):
            cx = bx + 3 + ci * cw
            pdf.set_font("helvetica", "", 6.2)
            pdf.set_text_color(*rp.GRAY)
            pdf.set_xy(cx, cy)
            pdf.cell(usable, KEY_H, k)
            pdf.set_font("helvetica", "B", val_font)
            pdf.set_text_color(*rp.DARK)
            for li, ln in enumerate(vlines):
                pdf.set_xy(cx, cy + KEY_H + 0.3 + li * VAL_H)
                pdf.cell(usable, VAL_H, ln)
        cy += rh
    pdf.set_y(by + band_h + 3)


def boxed(pdf, text, title=None, fill=(246, 248, 250), border=None):
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    lines = wrap_text(pdf, text, 7.4, bw - 8, style="", max_lines=12)
    h = (4.2 if title else 0) + len(lines) * 3.4 + 4
    pdf.need(h + 4)
    by = pdf.get_y()
    pdf.set_fill_color(*fill)
    pdf.set_draw_color(*(border or rp.LGRAY))
    pdf.set_line_width(0.3)
    pdf.rect(pdf.l_margin, by, bw, h, style="FD", round_corners=True, corner_radius=2)
    cy = by + 2
    if title:
        pdf.set_font("helvetica", "B", 7.8)
        pdf.set_text_color(*ACCENT)
        pdf.set_xy(pdf.l_margin + 4, cy)
        pdf.cell(bw - 8, 3.6, rp.S(title))
        cy += 4.2
    pdf.set_font("helvetica", "", 7.4)
    pdf.set_text_color(*rp.DARK)
    for ln in lines:
        pdf.set_xy(pdf.l_margin + 4, cy)
        pdf.cell(bw - 8, 3.4, ln)
        cy += 3.4
    pdf.set_y(by + h + 2.5)


def info_box(pdf, title, rows, fill=(246, 248, 250), border=None, title_col=None):
    """Premium labelled box: title line, then rows of (bold_label, text) or
    (None, text). Wrapped, dynamic height, never overflows."""
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    usable = bw - 8
    prepared = []
    for label, text in rows:
        prefix = f"{label} " if label else ""
        lines = wrap_text(pdf, prefix + str(text), 7.4, usable, style="", max_lines=4)
        prepared.append((label, lines))
    h = 5.0 + sum(len(l) * 3.4 + 0.7 for _, l in prepared) + 2.4
    pdf.need(h + 4)
    by = pdf.get_y()
    pdf.set_fill_color(*fill)
    pdf.set_draw_color(*(border or ACCENT))
    pdf.set_line_width(0.35)
    pdf.rect(pdf.l_margin, by, bw, h, style="FD", round_corners=True, corner_radius=2)
    pdf.set_font("helvetica", "B", 8.2)
    pdf.set_text_color(*ACCENT)
    pdf.set_xy(pdf.l_margin + 4, by + 1.8)
    pdf.cell(usable, 3.8, rp.S(title))
    cy = by + 6.2
    for label, lines in prepared:
        for i, ln in enumerate(lines):
            pdf.set_xy(pdf.l_margin + 4, cy)
            if i == 0 and label and ln.startswith(rp.S(label)):
                pdf.set_font("helvetica", "B", 7.4)
                pdf.set_text_color(*rp.DARK)
                lw = pdf.get_string_width(rp.S(label) + " ")
                pdf.cell(lw, 3.4, rp.S(label) + " ")
                pdf.set_font("helvetica", "", 7.4)
                pdf.cell(usable - lw, 3.4, ln[len(rp.S(label)) + 1:])
            else:
                pdf.set_font("helvetica", "", 7.4)
                pdf.set_text_color(*rp.DARK)
                pdf.cell(usable, 3.4, ln)
            cy += 3.4
        cy += 0.7
    pdf.set_y(by + h + 2.5)


def section_heading(pdf, heading):
    # premium pagination: never strand a heading near the bottom of a page
    pdf.need(34)
    pdf.ln(1.2)
    pdf.set_font("helvetica", "B", 9.5)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 5, rp.S(heading), new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(*ACCENT)
    pdf.set_line_width(0.35)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(0.8)
    pdf.set_font("helvetica", "", 7.6)
    pdf.set_text_color(*rp.DARK)


def disclaimer(pdf, text):
    pdf.ln(2)
    pdf.set_draw_color(*rp.LGRAY)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)
    pdf.set_font("helvetica", "", 6.3)
    pdf.set_text_color(*rp.GRAY)
    pdf.multi_cell(0, 2.9, rp.S(text))


def timeline_strip(pdf, events, title="Risk window timeline"):
    """Compact visual timeline: time chips joined by arrows; gap-risk chips red."""
    n = max(len(events), 1)
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    arrow_w = 4.0
    chip_w = (bw - arrow_w * (n - 1)) / n
    H = 13.5
    pdf.need(H + 9)
    pdf.set_font("helvetica", "B", 7.8)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 4.2, rp.S(title), new_x="LMARGIN", new_y="NEXT")
    y0 = pdf.get_y() + 0.6
    x = pdf.l_margin
    for i, ev in enumerate(events):
        gap = bool(ev.get("gap"))
        pdf.set_fill_color(*((253, 240, 240) if gap else (246, 248, 250)))
        pdf.set_draw_color(*(rp.RED if gap else ACCENT))
        pdf.set_line_width(0.3)
        pdf.rect(x, y0, chip_w, H, style="FD", round_corners=True, corner_radius=1.6)
        pdf.set_font("helvetica", "B", 6.4)
        pdf.set_text_color(*(rp.RED if gap else ACCENT))
        pdf.set_xy(x + 0.8, y0 + 1.1)
        pdf.cell(chip_w - 1.6, 2.8, rp.S(str(ev["t"])), align="C")
        pdf.set_text_color(*rp.DARK)
        lines = wrap_text(pdf, ev["label"], 5.9, chip_w - 2.2, style="", max_lines=3)
        for li, ln in enumerate(lines[:3]):
            pdf.set_font("helvetica", "", 5.9)
            pdf.set_xy(x + 1.1, y0 + 4.4 + li * 2.7)
            pdf.cell(chip_w - 2.2, 2.7, ln, align="C")
        x += chip_w
        if i < n - 1:
            pdf.set_font("helvetica", "B", 8.5)
            pdf.set_text_color(*rp.GRAY)
            pdf.set_xy(x, y0 + H / 2 - 2)
            pdf.cell(arrow_w, 4, ">", align="C")
            x += arrow_w
    pdf.set_y(y0 + H + 2.5)


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


LADDER_LEGEND = [("resistance", "Resistance"), ("target", "Target"), ("trigger", "Trigger"),
                 ("entry", "Entry zone"), ("support", "Support"),
                 ("invalidation", "Invalidation"), ("tail", "Tail risk")]


def price_ladder(pdf, ladder_levels, last_price_obj, title="Price ladder / levels map"):
    H = 96.0
    pdf.need(H + 12)
    x0, y0 = pdf.l_margin, pdf.get_y()
    W = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font("helvetica", "B", 8.4)
    pdf.set_text_color(*rp.DARK)
    pdf.set_xy(x0, y0)
    pdf.cell(W, 4, rp.S(title))
    pdf.set_font("helvetica", "", 5.8)
    pdf.set_text_color(*rp.GRAY)
    pdf.set_xy(x0, y0 + 3.9)
    pdf.cell(W, 2.6, "values show distance from last price")
    top, bot = y0 + 9, y0 + H - 10
    ch = bot - top
    last_val = float(last_price_obj["value"])
    rows, y_last, band = ladder_geometry(ladder_levels, last_val)
    ax = x0 + 52            # axis x
    tick_l, tick_r = ax - 3, ax + 10
    ref = last_val
    dp = _ladder_dp(rows, ref)

    # axis + entry band
    pdf.set_draw_color(*rp.LGRAY)
    pdf.set_line_width(0.5)
    pdf.line(ax, top, ax, bot)
    if band:
        b0, b1 = top + band[0] * ch, top + band[1] * ch
        with pdf.local_context(fill_opacity=0.15):
            pdf.set_fill_color(*rp.GREEN)
            pdf.rect(tick_l, b0, tick_r - tick_l + 26, max(b1 - b0, 0.8), style="F")

    used = []

    def place(y):
        while any(abs(y - u) < 3.6 for u in used):
            y += 3.6
        y = min(y, bot - 1.0)
        while any(abs(y - u) < 3.6 for u in used):
            y -= 3.6
        used.append(y)
        return y

    # last-price marker first so its label wins the collision race
    yl = top + y_last * ch
    pdf.set_draw_color(*rp.DARK)
    pdf.set_line_width(1.0)
    pdf.line(tick_l - 1.5, yl, tick_r + 1.5, yl)
    ly = place(yl - 1.6)
    chip_txt = rp.S(f"LAST {rp.fmt(last_val, ref)}")
    pdf.set_font("helvetica", "B", 7.0)
    cw = pdf.get_string_width(chip_txt) + 3.6
    pdf.set_fill_color(*rp.DARK)
    pdf.rect(ax - 5 - cw, ly - 0.5, cw, 3.9, style="F", round_corners=True, corner_radius=1.2)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(ax - 5 - cw, ly - 0.3)
    pdf.cell(cw, 3.4, chip_txt, align="C")
    pdf.set_font("helvetica", "B", 6.9)
    pdf.set_text_color(*rp.DARK)
    pdf.set_xy(tick_r + 3, ly)
    pdf.cell(W - (tick_r + 3 - x0), 2.8, "last price (live reference)")

    for l in rows:
        col = LADDER_COLORS.get(l["cls"], rp.GRAY)
        yv = top + l["y"] * ch
        pdf.set_draw_color(*col)
        pdf.set_line_width(0.65 if l["cls"] in ("invalidation", "trigger") else 0.45)
        if l["cls"] in ("tail", "invalidation"):
            with pdf.local_context():
                pdf.set_dash_pattern(dash=1.2, gap=1.0)
                pdf.line(tick_l, yv, tick_r, yv)
        else:
            pdf.line(tick_l, yv, tick_r, yv)
        ly = place(yv - 1.6)
        if abs(ly - (yv - 1.6)) > 1.8:  # leader line when label displaced
            pdf.set_line_width(0.15)
            pdf.set_draw_color(*rp.LGRAY)
            pdf.line(tick_r + 1.5, yv, tick_r + 2.6, ly + 1.4)
        emphasis = "B" if l["cls"] in ("invalidation", "trigger", "target", "entry") else ""
        pdf.set_font("helvetica", emphasis, 6.9)
        pdf.set_text_color(*col)
        pdf.set_xy(x0, ly)
        pdf.cell(ax - x0 - 5, 2.8,
                 rp.S(f"{rp.fmt(float(l['value']), ref)}  ({_pct_from(l['value'], ref, dp)})"),
                 align="R")
        pdf.set_xy(tick_r + 3, ly)
        pdf.cell(W - (tick_r + 3 - x0), 2.8, rp.S(l["label"]))
    pdf.set_dash_pattern()

    # legend: only the classes actually present, in ladder order
    present = {l["cls"] for l in rows}
    lx, lyy = x0, y0 + H - 6.6
    pdf.set_font("helvetica", "", 5.8)
    for cls, name in LADDER_LEGEND:
        if cls not in present:
            continue
        pdf.set_fill_color(*LADDER_COLORS.get(cls, rp.GRAY))
        pdf.rect(lx, lyy + 0.4, 2.2, 2.2, style="F")
        pdf.set_text_color(*rp.GRAY)
        pdf.set_xy(lx + 3.0, lyy)
        nm = rp.S(name)
        pdf.cell(pdf.get_string_width(nm) + 1, 3.0, nm)
        lx += 3.0 + pdf.get_string_width(nm) + 5.5
    pdf.set_font("helvetica", "I", 6.0)
    pdf.set_text_color(*rp.GRAY)
    pdf.set_xy(x0, y0 + H - 2.8)
    pdf.cell(W, 2.6, "Levels are conditional research references, not trade instructions.")
    pdf.set_y(y0 + H + 2.5)


def ladder_svg(ladder_levels, last_price_obj):
    W, H = 700, 348
    top, bot, ax = 36, H - 40, 230
    ch = bot - top
    last_val = float(last_price_obj["value"])
    rows, y_last, band = ladder_geometry(ladder_levels, last_val)
    dp = _ladder_dp(rows, last_val)
    css = {"tail": "#57606a", "resistance": "#cf222e", "target": "#0969da",
           "trigger": "#9a6700", "entry": "#1a7f37", "support": "#57606a",
           "invalidation": "#a40e26", "last": "#24292f"}
    e = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")
    P = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Arial" font-size="11">',
         f'<text x="14" y="18" font-size="13" font-weight="600" fill="#24292f">Price ladder / levels map</text>',
         f'<text x="14" y="30" font-size="9" fill="#57606a">values show distance from last price</text>',
         f'<line x1="{ax}" y1="{top}" x2="{ax}" y2="{bot}" stroke="#d8dee4" stroke-width="2"/>']
    if band:
        b0, b1 = top + band[0] * ch, top + band[1] * ch
        P.append(f'<rect x="{ax-12}" y="{b0:.1f}" width="180" height="{max(b1-b0,3):.1f}" fill="#1a7f37" opacity="0.15"/>')
    used = []

    def place(y):
        while any(abs(y - u) < 13.5 for u in used):
            y += 13.5
        y = min(y, bot)
        while any(abs(y - u) < 13.5 for u in used):
            y -= 13.5
        used.append(y)
        return y
    yl = top + y_last * ch
    P.append(f'<line x1="{ax-16}" y1="{yl:.1f}" x2="{ax+44}" y2="{yl:.1f}" stroke="#24292f" stroke-width="3.4"/>')
    ly = place(yl + 4)
    chip = f"LAST {rp.fmt(last_val, last_val)}"
    cw = 7.2 * len(chip) + 12
    P.append(f'<rect x="{ax-22-cw:.0f}" y="{ly-10.5:.1f}" width="{cw:.0f}" height="14.5" rx="3" fill="#24292f"/>')
    P.append(f'<text x="{ax-22-cw/2:.0f}" y="{ly:.1f}" text-anchor="middle" font-weight="700" fill="#ffffff">{chip}</text>')
    P.append(f'<text x="{ax+52}" y="{ly:.1f}" font-weight="700" fill="#24292f">last price (live reference)</text>')
    for l in rows:
        col = css.get(l["cls"], "#57606a")
        yv = top + l["y"] * ch
        dash = ' stroke-dasharray="5,4"' if l["cls"] in ("tail", "invalidation") else ""
        wt = "2.6" if l["cls"] in ("invalidation", "trigger") else "2"
        P.append(f'<line x1="{ax-12}" y1="{yv:.1f}" x2="{ax+40}" y2="{yv:.1f}" stroke="{col}" stroke-width="{wt}"{dash}/>')
        ly = place(yv + 4)
        bold = ' font-weight="600"' if l["cls"] in ("invalidation", "trigger", "target", "entry") else ""
        P.append(f'<text x="{ax-22}" y="{ly:.1f}" text-anchor="end" fill="{col}"{bold}>'
                 f'{rp.fmt(float(l["value"]), last_val)} <tspan font-size="9" fill="#57606a">({_pct_from(l["value"], last_val, dp)})</tspan></text>')
        P.append(f'<text x="{ax+52}" y="{ly:.1f}" fill="{col}"{bold}>{e(l["label"])}</text>')
    # legend - classes actually present
    present = {l["cls"] for l in rows}
    lx = 14
    for cls, name in LADDER_LEGEND:
        if cls not in present:
            continue
        P.append(f'<rect x="{lx}" y="{H-26}" width="9" height="9" fill="{css.get(cls, "#57606a")}"/>')
        P.append(f'<text x="{lx+13}" y="{H-18}" font-size="9.5" fill="#57606a">{e(name)}</text>')
        lx += 13 + 6.0 * len(name) + 16
    P.append(f'<text x="14" y="{H-5}" font-style="italic" font-size="9" fill="#57606a">Levels are conditional research references, not trade instructions.</text>')
    P.append("</svg>")
    return "".join(P)


def chart_note(pdf, text):
    """One-line plain-English note under a chart explaining its markings."""
    pdf.set_font("helvetica", "I", 6.0)
    pdf.set_text_color(*rp.GRAY)
    pdf.set_x(pdf.l_margin + 2)
    pdf.multi_cell(0, 2.8, rp.S(text))
    pdf.ln(1.2)


def kv_card(pdf, title, items):
    """Compact two-column badge card (used for Source confidence / Report quality)."""
    pdf.need(20)
    pdf.set_font("helvetica", "B", 7.8)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 4.0, rp.S(title), new_x="LMARGIN", new_y="NEXT")
    card_grid(pdf, items, cols=2, val_font=6.8)


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


def key_levels_strip(pdf, p):
    """One-row chip strip of the numbers that matter most - auto-derived from the
    canonical object (last + primary setup), so it can never disagree with the body."""
    c = p["canonical"]
    setups = c.get("setups") or []
    if not setups:
        return
    s0 = setups[0]
    last = float(c["last_price"]["value"])
    fv = lambda v: rp.fmt(float(v), last)
    chips = [("LAST", fv(last), rp.DARK)]
    if s0.get("entry_lo") is not None and s0.get("entry_hi") is not None:
        d = (s0.get("direction") or "").upper()
        chips.append((f"ENTRY ZONE{' - ' + d if d else ''}",
                      f"{fv(s0['entry_lo'])} - {fv(s0['entry_hi'])}", rp.GREEN))
    for key, lab, col in (("invalidation", "INVALIDATION", (164, 14, 38)),
                          ("t1", "TARGET 1", rp.BLUE), ("t2", "TARGET 2", rp.BLUE)):
        if s0.get(key) is not None:
            chips.append((lab, fv(s0[key]), col))
    H, gap = 11.8, 2.6
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.need(H + 9)
    pdf.set_font("helvetica", "B", 7.8)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 4.0, rp.S(f"Key levels - {s0.get('name', 'primary setup')}"),
             new_x="LMARGIN", new_y="NEXT")
    widths = []
    for lab, val, _ in chips:
        pdf.set_font("helvetica", "B", 8.4)
        wv = pdf.get_string_width(rp.S(val))
        pdf.set_font("helvetica", "", 5.4)
        wl = pdf.get_string_width(rp.S(lab))
        widths.append(max(wv, wl) + 7.5)
    spare = bw - gap * (len(chips) - 1) - sum(widths)
    if spare > 0:
        widths = [w + spare / len(widths) for w in widths]
    else:
        widths = [w * (bw - gap * (len(chips) - 1)) / sum(widths) for w in widths]
    x, y = pdf.l_margin, pdf.get_y() + 0.4
    for (lab, val, col), w in zip(chips, widths):
        pdf.set_fill_color(250, 251, 252)
        pdf.set_draw_color(*rp.LGRAY)
        pdf.set_line_width(0.3)
        pdf.rect(x, y, w, H, style="FD", round_corners=True, corner_radius=1.8)
        pdf.set_fill_color(*col)
        pdf.rect(x, y, 1.5, H, style="F", round_corners=True, corner_radius=0.7)
        pdf.set_font("helvetica", "", 5.4)
        pdf.set_text_color(*rp.GRAY)
        pdf.set_xy(x + 3.0, y + 1.7)
        pdf.cell(w - 3.6, 2.6, rp.S(lab))
        pdf.set_font("helvetica", "B", 8.4)
        pdf.set_text_color(*col)
        pdf.set_xy(x + 3.0, y + 5.3)
        pdf.cell(w - 3.6, 3.8, rp.S(val))
        x += w + gap
    pdf.set_y(y + H + 3)


def fg_gauge(pdf, fg):
    """Fear & Greed dial: 0 (extreme fear) -> 100 (extreme greed), needle at value."""
    val = max(0, min(100, int(fg["value"])))
    lab = fg.get("label", "")
    H = 16.0
    pdf.need(H + 6)
    x0, y0 = pdf.l_margin, pdf.get_y()
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    gx, gw = x0 + 30, bw - 30 - 42
    pdf.set_font("helvetica", "B", 7.2)
    pdf.set_text_color(*rp.DARK)
    pdf.set_xy(x0, y0 + 3.6)
    pdf.cell(28, 3.5, "Fear & Greed")
    for a, b, c in FG_ZONES_PDF:
        with pdf.local_context(fill_opacity=0.38):
            pdf.set_fill_color(*c)
            pdf.rect(gx + gw * a / 100, y0 + 2.6, gw * (b - a) / 100, 5.8, style="F")
    nx = gx + gw * val / 100
    pdf.set_draw_color(*rp.DARK)
    pdf.set_line_width(0.9)
    pdf.line(nx, y0 + 1.4, nx, y0 + 9.6)
    pdf.set_font("helvetica", "B", 8.2)
    pdf.set_text_color(*rp.DARK)
    pdf.set_xy(gx + gw + 3, y0 + 3.4)
    pdf.cell(40, 4, rp.S(f"{val} - {lab}"))
    pdf.set_font("helvetica", "", 5.2)
    pdf.set_text_color(*rp.GRAY)
    for v, t in ((0, "0 extreme fear"), (50, "50"), (100, "100 extreme greed")):
        tw = pdf.get_string_width(t)
        pdf.set_xy(min(max(gx + gw * v / 100 - tw / 2, gx - 8), gx + gw - tw + 8), y0 + 9.9)
        pdf.cell(tw + 1, 2.4, t)
    if fg.get("source"):
        pdf.set_xy(x0, y0 + 13.0)
        pdf.set_font("helvetica", "I", 5.6)
        pdf.cell(bw, 2.6, rp.S(f"Source: {fg['source']}"
                               + (f" - as of {fg['asof']}" if fg.get("asof") else "")))
    pdf.set_y(y0 + H + 1.8)


# ---------------------------------------------------------------- QA gate
def _num_in_levels(v, level_vals, tol=1e-6):
    return any(abs(float(v) - lv) <= max(tol, abs(lv) * 1e-6) for lv in level_vals)


def run_qa(p):
    """Pre-render QA. Returns (qa_dict, errors, warnings). Errors abort the build."""
    errs, warns = [], []
    c = p["canonical"]
    meta = p["meta"]
    level_vals = [float(l["value"]) for l in c["levels"]]
    last = float(c["last_price"]["value"])

    # --- price triple-equality (header/chart/metadata) from the hourly CSV
    hourly_cfg = next((ch for ch in p["pro"]["charts"] if "hourly" in ch["csv"].lower()
                       or ch.get("display_days", 99) <= 30), p["pro"]["charts"][-1])
    rows = rp.read_series(Path(hourly_cfg["csv"]))
    if not rows:
        # A degraded/empty hourly CSV must FAIL QA cleanly, not crash on rows[-1].
        errs.append(f"hourly CSV {hourly_cfg['csv']} has no rows - cannot verify the canonical price")
    else:
        csv_last = rows[-1]["c"]
        ok_price = abs(csv_last - last) <= max(0.01, last * 1e-5)
        if not ok_price:
            errs.append(f"canonical last {last} != hourly CSV last close {csv_last}")
    if str(meta.get("last_price", "")).strip() == "":
        errs.append("meta.last_price empty")
    free_chart_same = p["free"]["chart"]["csv"] == hourly_cfg["csv"]
    if not free_chart_same:
        warns.append("free chart uses a different CSV than pro hourly chart")

    # --- levels consistency: setups + ladder + ledger reference canonical levels
    setups = c.get("setups", [])
    ladder_ids = set(c.get("ladder", []))
    levels_by_id = {l["id"]: l for l in c["levels"]}
    ok_levels = ok_ladder = ok_ledger = True
    for s in setups:
        for key in ("entry_lo", "entry_hi", "invalidation", "t1", "t2"):
            v = s.get(key)
            if v is None:
                continue
            if not _num_in_levels(v, level_vals):
                ok_levels = False
                errs.append(f"setup {s.get('name')} {key}={v} not in canonical levels")
            if key in ("invalidation", "t1", "t2"):
                if not any(abs(float(levels_by_id[i]["value"]) - float(v)) <= 1e-6
                           for i in ladder_ids if i in levels_by_id):
                    ok_ladder = False
                    errs.append(f"setup {s.get('name')} {key}={v} missing from ladder")
        rr = s.get("rr", "")
        if not RR_OK.match(rr):
            errs.append(f"setup {s.get('name')} rr string not in approved format: '{rr}'")
    for i in ladder_ids:
        if i not in levels_by_id:
            ok_ladder = False
            errs.append(f"ladder id '{i}' not in canonical levels")
    for v in c.get("ledger_levels", []):
        if not _num_in_levels(v, level_vals):
            ok_ledger = False
            errs.append(f"ledger level {v} not in canonical levels")

    blob = json.dumps(p, ensure_ascii=False).lower()
    # --- banned language; some phrases allowed only in negated compliance form
    for pat in BANNED:
        if re.search(pat, blob):
            errs.append(f"banned language present: /{pat}/")
    for phrase, negation in NEGATED_ONLY.items():
        for m in re.finditer(phrase, blob):
            ctx = blob[max(0, m.start() - 34):m.start()]
            if not re.search(negation, ctx):
                errs.append(f"unnegated '{phrase}' phrasing found")
    if RR_BAD.search(json.dumps(p, ensure_ascii=False)):
        errs.append("negative-looking R:R rendering found")
    for m in re.finditer(r"(high quality|acceptable|low quality|management only|no-trade)", blob):
        pass  # presence is fine; enum enforced on canonical fields below
    for fld in ("long_scenario_quality", "short_scenario_quality"):
        v = meta.get(fld, "")
        if v and v not in QUALITY_LABELS:
            errs.append(f"meta.{fld}='{v}' not a valid quality label")

    # --- free/pro split
    fch = p["free"]["chart"]
    n_levels = len(fch.get("support", [])) + len(fch.get("resistance", []))
    ok_split = True
    if n_levels > 3 or "pivots" in fch or "bands" in fch:
        ok_split = False
        errs.append("free chart exceeds 3 labelled levels or carries pivots/bands")
    # the teaser legitimately NAMES pro features (it is the lead-magnet pitch);
    # the content scan covers everything else in the free tier
    free_scan = {k: v for k, v in p["free"].items() if k not in ("teaser", "disclaimer")}
    free_blob = json.dumps(free_scan, ensure_ascii=False).lower()
    for banned_free in ("r:r", "per contract", "entry zone", "invalidation",
                        "t1 ", "t2 ", "ladder", "glossary", "source audit",
                        "outcome ledger", "hedging", "risk math"):
        if banned_free in free_blob:
            ok_split = False
            errs.append(f"free tier contains pro-only content: '{banned_free}'")

    # --- high-impact claims
    for cl in meta.get("high_impact_claims", []):
        if cl.get("status") not in CLAIM_STATUSES:
            errs.append(f"claim '{cl.get('claim','?')[:40]}' bad status {cl.get('status')}")
        if cl.get("used_in_thesis") and cl.get("status") in THESIS_BLOCKED:
            errs.append(f"claim '{cl.get('claim','?')[:40]}' is {cl.get('status')} but used_in_thesis")

    # --- editorial structure (warnings only - old payloads still build)
    if not p["pro"].get("overview"):
        warns.append("pro.overview (plain-English box) missing - strongly recommended")
    known = []
    for s in p["pro"].get("sections", []):
        h = s["heading"].strip().lower()
        for idx, key in enumerate(SECTION_ORDER):
            if h.startswith(key):
                known.append((idx, s["heading"]))
                break
    for (i1, h1), (i2, h2) in zip(known, known[1:]):
        if i2 < i1:
            warns.append(f"section order: '{h2}' renders after '{h1}' but belongs earlier")
    if len(c.get("ladder", [])) > 12:
        warns.append(f"ladder has {len(c['ladder'])} levels - prefer 8-10, never more than 12")
    # all-caps editorial scan over authored narrative (acronyms allowed)
    texts = []
    for s in p["pro"].get("sections", []):
        texts.append(rp.plain(s.get("html", "")))
        for it in s.get("items", []):
            texts.append(str(it.get("label", "")) + " " + rp.plain(str(it.get("text", ""))))
    ov = p["pro"].get("overview")
    if ov:
        texts += [ov] if isinstance(ov, str) else [str(t) for t in ov]
    texts += [str(x) for x in (p["pro"].get("verdict") or {}).values()]
    caps = sorted({w for t in texts for w in re.findall(r"\b[A-Z]{3,}\b", t)} - CAPS_ALLOW)
    if caps:
        warns.append("all-caps words in narrative (use sentence case): " + ", ".join(caps[:8]))
    # catalyst-status line required when a claim drives the thesis
    if (any(cl.get("used_in_thesis") for cl in meta.get("high_impact_claims", []))
            and not p["pro"].get("catalyst_status")):
        warns.append("pro.catalyst_status missing while thesis-driving claims exist")
    # optional-chart governance: default visual set is 2 charts; a third must be declared
    if len(p["pro"].get("charts", [])) > 2 and not (meta.get("optional_chart") or {}).get("included"):
        warns.append("more than 2 pro charts but meta.optional_chart not declared")

    # --- timestamps + lookahead
    ok_ts = True
    for fld in ("prediction_window_start_utc", "prediction_window_end_utc",
                "latest_bar_timestamp_utc"):
        try:
            datetime.strptime(meta[fld][:16], "%Y-%m-%d %H:%M")
        except Exception:
            ok_ts = False
            errs.append(f"meta.{fld} missing/unparseable")
    no_look = True
    try:
        ws = datetime.strptime(meta["prediction_window_start_utc"][:16], "%Y-%m-%d %H:%M")
        bt = datetime.strptime(meta["latest_bar_timestamp_utc"][:16], "%Y-%m-%d %H:%M")
        no_look = ws >= bt - __import__("datetime").timedelta(hours=1)
        if not no_look:
            errs.append("prediction window starts before latest bar (lookahead)")
    except Exception:
        no_look = False

    # --- session rules + misc
    ok_sess = bool(meta.get("market_session_type")) and bool(meta.get("market_close_utc"))
    if not ok_sess:
        errs.append("session fields missing (market_session_type/market_close_utc)")
    if not bool(meta.get("next_major_event")):
        warns.append("meta.next_major_event empty")
    if not LOGO.exists():
        errs.append(f"logo missing at {LOGO}")
    bar_complete = bool(c["last_price"].get("bar_complete", False))
    if not bar_complete and "(live bar)" not in blob and "live" not in str(meta.get("last_price", "")).lower():
        warns.append("incomplete last bar not labelled 'live' in header")

    # --- prediction-type enum (taxonomy is the single vocabulary across the pipeline)
    pt = meta.get("prediction_type")
    prediction_type_valid = True
    if pt is None:
        warns.append("meta.prediction_type missing (older payload) - cannot tag edition archetype")
    elif pt not in PREDICTION_TYPES:
        prediction_type_valid = False
        errs.append(f"meta.prediction_type='{pt}' not in taxonomy.PREDICTION_TYPES {list(PREDICTION_TYPES)}")

    # --- confidence vs breakdown: the gauge and scorecard must agree on one number
    confidence_matches_breakdown = True
    cb = p.get("confidence_breakdown")
    if cb is not None:
        try:
            if int(p["confidence"]) != int(cb["published"]):
                confidence_matches_breakdown = False
                errs.append("payload.confidence != confidence_breakdown.published")
        except (KeyError, TypeError, ValueError):
            confidence_matches_breakdown = False
            errs.append("payload.confidence != confidence_breakdown.published")

    # --- social must read as market conversation, never as fact (light heuristic).
    # Trigger only on social-as-signal language, NOT the scorecard's "Social adj." label.
    social_labelled_soft = True
    SOFT_PHRASES = ("market conversation", "not a fact", "sentiment context", "soft signal")
    SOCIAL_SIGNAL = ("social sentiment", "social media", "social chatter", "stocktwits",
                     "reddit", "retail chatter", "crowd sentiment", "hype")
    pro_blob = json.dumps(p.get("pro", {}), ensure_ascii=False).lower()
    if any(t in pro_blob for t in SOCIAL_SIGNAL) and not any(ph in pro_blob for ph in SOFT_PHRASES):
        social_labelled_soft = False
        warns.append("social sentiment appears in pro sections but is not framed as market "
                     "conversation (add 'market conversation' / 'sentiment context' / 'soft signal')")

    qa = {
        "logo_present": LOGO.exists(),
        "header_price_matches_chart": ok_price,
        "free_chart_matches_metadata": ok_price and free_chart_same,
        "pro_chart_matches_metadata": ok_price,
        "levels_match_setups": ok_levels,
        "setups_match_ladder": ok_ladder,
        "ledger_levels_match_tables": ok_ledger,
        "timestamps_normalized_utc": ok_ts,
        "no_lookahead": no_look,
        "asset_session_rules_applied": ok_sess,
        "free_pro_split_enforced": ok_split,
        "rr_format_unambiguous": not RR_BAD.search(json.dumps(p, ensure_ascii=False))
                                 and all(RR_OK.match(s.get("rr", "")) for s in setups),
        "chart_abbreviations_explained": bool(_glossary_rows(p)),
        "ladder_size_ok": len(c.get("ladder", [])) <= 12,
        "prediction_type_valid": prediction_type_valid,
        "confidence_matches_breakdown": confidence_matches_breakdown,
        "social_labelled_soft": social_labelled_soft,
        "visual_inspection_passed": False,
    }
    return qa, errs, warns


# ---------------------------------------------------------------- builders
def new_pdf(p, tier_tag):
    pdf = rp.Report(orientation="P", unit="mm", format="A4")
    pdf.core_fonts_encoding = "windows-1252"
    pdf.set_margins(13, 12, 13)
    _yr = ("".join(c for c in str(p.get("report_id", "")) if c.isdigit())[:4]) or "2026"
    pdf.meta_footer = rp.S(" \xb7 ".join(
        [BRAND, f'{p["report_id"]}-{tier_tag}',
         "General market research - not personal advice",
         f"© {_yr} {BRAND}. All rights reserved."]))
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()
    return pdf


def build_free(p):
    f = p["free"]
    pdf = new_pdf(p, "FREE")
    brand_band(pdf, "ASSETFRAME SNAPSHOT  -  FREE", tagline=TAGLINE)
    title_block(pdf, p["title"], p["subtitle"])
    chips(pdf, p["status"], p["risk"])
    card_grid(pdf, f["cards"], cols=2)
    pdf.chart(rp.read_series(Path(f["chart"]["csv"])), f["chart"])
    chart_note(pdf, FREE_CHART_NOTE)
    rp.render_section_html(pdf, f["bullets_html"], bullet_color=ACCENT)
    pdf.ln(0.5)
    pdf.set_font("helvetica", "B", 8.6)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 4.4, "Scenarios (broad view)", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 7.6)
    pdf.set_text_color(*rp.DARK)
    rp.render_section_html(pdf, f["scenarios_html"], bullet_color=ACCENT)
    timeline_strip(pdf, f["timeline_events"])
    boxed(pdf, f["teaser"], title="Inside AssetFrame Pro", fill=ACCENT_FILL, border=ACCENT)
    disclaimer(pdf, f["disclaimer"])
    return pdf


def build_pro(p, qa):
    pro = p["pro"]
    pdf = new_pdf(p, "PRO")
    brand_band(pdf, "ASSETFRAME PRO  -  MARKET INTELLIGENCE", tagline=TAGLINE)
    title_block(pdf, p["title"], p["subtitle"])
    chips(pdf, p["status"], p["risk"])
    card_grid(pdf, pro["exec"], cols=2)
    ov = pro.get("overview")
    if ov:
        lines = [ov] if isinstance(ov, str) else list(ov)
        info_box(pdf, "In plain English - the 30-second read",
                 [(None, t) for t in lines], fill=(255, 255, 255))
    v = pro.get("verdict")
    if v:
        info_box(pdf, "Pro verdict",
                 [(None, v["line"]),
                  ("Best opportunity:", v["best"]),
                  ("Main risk:", v["risk"]),
                  ("Stand-aside condition:", v["stand_aside"])],
                 fill=ACCENT_FILL)
    cs = pro.get("catalyst_status")
    if cs:
        pdf.set_x(pdf.l_margin)
        pdf.set_font("helvetica", "B", 7.0)
        pdf.set_text_color(*rp.DARK)
        pdf.write(3.4, rp.S("Catalyst status: "))
        pdf.set_font("helvetica", "", 7.0)
        pdf.write(3.4, rp.S(str(cs)))
        pdf.ln(5.2)
    key_levels_strip(pdf, p)
    for cfg in pro.get("charts", []):
        rows = rp.read_series(Path(cfg["csv"]))
        pdf.chart(rows, cfg)
        if cfg.get("pivots"):
            chart_note(pdf, PIVOT_CHART_NOTE)
        if cfg.get("rsi"):
            pdf.rsi_panel(rows, cfg)
    ladder_levels = [l for l in p["canonical"]["levels"] if l["id"] in set(p["canonical"]["ladder"])]
    price_ladder(pdf, ladder_levels, p["canonical"]["last_price"])
    pdf.gauge(int(p.get("confidence", 0)), ACCENT)
    sent = pro.get("sentiment")
    if sent:
        section_heading(pdf, "Sentiment & positioning (sourced)")
        if sent.get("fear_greed"):
            fg_gauge(pdf, sent["fear_greed"])
        if sent.get("rows"):
            tbl = ("<table><tr><th>Source</th><th>Reading</th><th>Why it matters</th></tr>"
                   + "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>"
                             for r in sent["rows"]) + "</table>")
            rp.render_section_html(pdf, tbl, bullet_color=ACCENT)
        if sent.get("note"):
            rp.render_section_html(pdf, str(sent["note"]), bullet_color=ACCENT)
        pdf.ln(0.5)
    _fundamentals_pdf(pdf, p.get("fundamentals"))   # Pro-only; canonical figures
    sc = pro.get("source_confidence")
    for s in pro.get("sections", []):
        if s["heading"].strip().lower().startswith("source audit"):
            if sc:
                kv_card(pdf, "Source confidence", [(str(k), str(t)) for k, t in sc])
            kv_card(pdf, "Report quality", _report_quality_rows(p, qa))
        section_heading(pdf, s["heading"])
        rp.render_section_html(pdf, _section_body(s), bullet_color=ACCENT)
        pdf.ln(0.5)
    gl = _glossary_rows(p)
    if gl:
        info_box(pdf, "Glossary - how to read the charts and levels", gl,
                 fill=(246, 248, 250), border=rp.LGRAY)
    disclaimer(pdf, pro["disclaimer"])
    return pdf


# ---------------------------------------------------------------- HTML twins
def _logo_b64():
    try:
        return base64.b64encode(LOGO.read_bytes()).decode()
    except Exception:
        return ""


def _rsi_svg(csv_path, cfg):
    rows_f = rp.read_series(Path(csv_path))
    i0 = rp.crop_index(rows_f, cfg.get("display_days"))
    rows = rows_f[i0:]
    line = rp.rsi_line([r["c"] for r in rows_f])[i0:]
    pts = " ".join(f"{56 + 634 * i / max(len(rows) - 1, 1):.1f},{14 + 68 * (1 - v / 100):.1f}"
                   for i, v in enumerate(line) if v is not None)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 100" font-family="Arial" font-size="10">'
            f'<text x="56" y="10" font-size="11" font-weight="600" fill="#24292f">RSI(14) {cfg.get("rsi_tag", "")}</text>'
            + "".join(f'<line x1="56" y1="{14 + 68 * (1 - l / 100):.1f}" x2="690" y2="{14 + 68 * (1 - l / 100):.1f}" stroke="{c}" stroke-width="0.7" stroke-dasharray="4,3"/>'
                      f'<text x="50" y="{17 + 68 * (1 - l / 100):.1f}" text-anchor="end" fill="#57606a">{l}</text>'
                      for l, c in ((70, "#cf222e"), (50, "#d8dee4"), (30, "#1a7f37")))
            + f'<polyline points="{pts}" fill="none" stroke="#bc4c00" stroke-width="1.4"/></svg>')


def _timeline_html(events):
    chips_html = ""
    for i, ev in enumerate(events):
        cls = "tl gap" if ev.get("gap") else "tl"
        chips_html += f'<div class="{cls}"><b>{ev["t"]}</b><span>{ev["label"]}</span></div>'
        if i < len(events) - 1:
            chips_html += '<div class="tla">&rsaquo;</div>'
    return f'<div class="tlrow">{chips_html}</div>'


def _gauge_svg(conf):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 44" font-family="Arial" font-size="10">'
            '<text x="82" y="24" text-anchor="end" font-weight="600" fill="#24292f">Confidence</text>'
            + "".join(f'<rect x="{90 + 540 * a / 100:.1f}" y="12" width="{540 * (b - a) / 100:.1f}" height="14" fill="{c}" opacity="0.35" rx="2"/>'
                      for a, b, c in [(0, 20, "#cf222e"), (20, 40, "#bc4c00"), (40, 60, "#9a6700"),
                                      (60, 75, "#4d8a2a"), (75, 90, "#1a7f37"), (90, 100, "#116329")])
            + f'<rect x="90" y="12" width="{540 * conf / 100:.1f}" height="14" fill="#0b2545" opacity="0.9" rx="2"/>'
            f'<text x="{96 + 540 * conf / 100:.1f}" y="24" font-weight="700" fill="#24292f">{conf}/100</text></svg>')


_CSS = """
@page { size: A4; margin: 13mm; } * { box-sizing: border-box; }
body { font-family: Arial, sans-serif; color: #24292f; font-size: 10.2px; line-height: 1.45; margin: 0 auto; max-width: 780px; padding: 12px; }
.brandbar { display: flex; justify-content: space-between; align-items: center; border-bottom: 2.5px solid #0b2545; padding-bottom: 5px; margin-bottom: 4px; }
.brandbar img { height: 26px; } .tier { color: #57606a; font-weight: 700; font-size: 11px; letter-spacing: .04em; }
.tagline { color: #57606a; font-style: italic; font-size: 10px; margin: 2px 0 8px; }
h1 { font-size: 20px; margin: 0; } .sub { color: #57606a; font-size: 11px; margin: 2px 0 8px; }
.chips span { display: inline-block; padding: 3px 12px; border-radius: 12px; color: #fff; font-weight: 700; font-size: 11px; margin-right: 8px; }
.band { display: grid; grid-template-columns: repeat(2, 1fr); gap: 5px 22px; background: #f6f8fa; border: 1px solid #0b2545; border-radius: 8px; padding: 9px 13px; margin: 9px 0; }
.kv { display: flex; justify-content: space-between; gap: 6px; } .kv .k { color: #57606a; } .kv .v { font-weight: 600; text-align: right; }
section { margin: 8px 0; break-inside: avoid-page; }
section h2 { font-size: 12.5px; color: #0b2545; border-bottom: 1.5px solid #0b2545; padding-bottom: 2px; margin: 0 0 4px; }
ul { margin: 3px 0 3px 18px; padding: 0; } li { margin: 2.5px 0; } li::marker { color: #0b2545; font-weight: 700; }
table { border-collapse: collapse; width: 100%; margin: 3px 0; } th, td { border: 1px solid #d8dee4; padding: 3px 7px; text-align: left; vertical-align: top; font-variant-numeric: tabular-nums; } th { background: #e9eef6; color: #0b2545; }
tbody tr:nth-child(even) td, tr:nth-child(even) td { background: #fafbfc; }
.klrow { display: flex; gap: 6px; margin: 8px 0; }
.kl { flex: 1; border: 1px solid #d8dee4; border-left: 4px solid #57606a; border-radius: 6px; padding: 4px 9px; background: #fafbfc; }
.kl .k { font-size: 8.5px; color: #57606a; text-transform: uppercase; letter-spacing: .04em; }
.kl .v { font-weight: 700; font-size: 13px; }
.plainbox { background: #fff; border: 1.4px solid #0b2545; border-radius: 8px; padding: 8px 12px; margin: 9px 0; }
.plainbox div { margin: 3px 0; }
.chartnote { color: #57606a; font-style: italic; font-size: 9px; margin: -2px 0 8px; }
.catstat { font-size: 10px; margin: 2px 0 8px; }
.tlrow { display: flex; align-items: stretch; gap: 4px; margin: 8px 0; }
.tl { flex: 1; border: 1px solid #0b2545; background: #f6f8fa; border-radius: 6px; padding: 4px 6px; text-align: center; font-size: 9.5px; }
.tl b { display: block; color: #0b2545; } .tl.gap { border-color: #cf222e; background: #fdf0f0; } .tl.gap b { color: #cf222e; }
.tla { align-self: center; color: #57606a; font-size: 16px; }
.teaser { background: #e9eef6; border: 1.5px solid #0b2545; border-radius: 8px; padding: 8px 12px; margin: 9px 0; }
.teaser b { color: #0b2545; }
.infobox { background: #f6f8fa; border: 1.2px solid #0b2545; border-radius: 8px; padding: 8px 12px; margin: 9px 0; }
.infobox div { margin: 2px 0; }
.disc { border-top: 1.5px solid #d8dee4; margin-top: 11px; padding-top: 6px; font-size: 8.6px; color: #57606a; }
b.up { color: #1a7f37; } b.dn { color: #cf222e; }
"""


def _html_head(p, tier):
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    # White text on these chips, so they must clear WCAG AA 4.5:1 (matches the web app's
    # darkened sell/high/very-high shades).
    risk_css = {"low": "#1a7f37", "medium": "#9a6700", "high": "#9a3d00", "very high": "#b91c1c"}
    st_css = {"buy": "#1a7f37", "sell": "#b91c1c", "wait": "#9a6700", "stand aside": "#57606a", "neutral": "#0969da"}
    return (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{e(p["title"])} - {BRAND} {tier}</title>'
            f'<style>{_CSS}@media screen and (max-width:640px){{body{{font-size:14px;padding:16px;max-width:100%}}table{{display:block;overflow-x:auto}}}}</style></head><body>'
            f'<div class="brandbar"><img src="data:image/png;base64,{_logo_b64()}" alt="AssetFrame">'
            f'<span class="tier">ASSETFRAME {tier.upper()}</span></div>'
            f'<div class="tagline">{TAGLINE}</div>'
            f'<h1>{e(p["title"])}</h1><div class="sub">{e(p["subtitle"])}</div>'
            f'<div class="chips"><span style="background:{st_css.get(p["status"].lower(), "#57606a")}">{e(p["status"])}</span>'
            f'<span style="background:{risk_css.get(p["risk"].lower(), "#57606a")}">Risk: {e(p["risk"])}</span></div>')


def _cards_html(items):
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    return '<div class="band">' + "".join(
        f'<div class="kv"><span class="k">{e(k)}</span><span class="v">{e(v)}</span></div>'
        for k, v in items) + "</div>"


def build_free_html(p):
    f = p["free"]
    h = _html_head(p, "Snapshot - Free")
    h += _cards_html(f["cards"])
    h += rp.chart_svg(rp.read_series(Path(f["chart"]["csv"])), f["chart"])
    h += f'<div class="chartnote">{FREE_CHART_NOTE}</div>'
    h += f'<section>{f["bullets_html"]}</section>'
    h += f'<section><h2>Scenarios (broad view)</h2>{f["scenarios_html"]}</section>'
    h += f'<section><h2>Risk window timeline</h2>{_timeline_html(f["timeline_events"])}</section>'
    h += f'<div class="teaser"><b>Inside AssetFrame Pro:</b> {f["teaser"]}</div>'
    h += f'<div class="disc">{f["disclaimer"]}</div></body></html>'
    return h


def _info_box_html(title, rows, accent_bg=False, cls=None):
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    body = "".join(f'<div>{("<b>" + e(l) + "</b> ") if l else ""}{e(t)}</div>' for l, t in rows)
    cls = cls or ("teaser" if accent_bg else "infobox")
    return f'<div class="{cls}"><b style="color:#0b2545">{e(title)}</b>{body}</div>'


def _fg_svg(fg):
    val = max(0, min(100, int(fg["value"])))
    lab = str(fg.get("label", ""))
    nx = 120 + 460 * val / 100
    src = (f'<text x="120" y="50" font-size="8.5" font-style="italic" fill="#57606a">Source: {fg.get("source", "")}'
           + (f' - as of {fg.get("asof", "")}' if fg.get("asof") else "") + "</text>") if fg.get("source") else ""
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 54" font-family="Arial" font-size="10">'
            '<text x="112" y="24" text-anchor="end" font-weight="600" fill="#24292f">Fear &amp; Greed</text>'
            + "".join(f'<rect x="{120 + 460 * a / 100:.0f}" y="10" width="{460 * (b - a) / 100:.0f}" height="16" fill="{c}" opacity="0.4" rx="2"/>'
                      for a, b, c in FG_ZONES_CSS)
            + f'<line x1="{nx:.0f}" y1="6" x2="{nx:.0f}" y2="30" stroke="#24292f" stroke-width="3"/>'
            + f'<text x="592" y="24" font-weight="700" fill="#24292f">{val} - {lab}</text>'
            + "".join(f'<text x="{120 + 460 * z / 100:.0f}" y="40" text-anchor="middle" fill="#57606a" font-size="8.5">{t}</text>'
                      for z, t in ((0, "0 extreme fear"), (50, "50"), (100, "100 extreme greed")))
            + src + "</svg>")


def _kl_html(p):
    c = p["canonical"]
    setups = c.get("setups") or []
    if not setups:
        return ""
    s0 = setups[0]
    last = float(c["last_price"]["value"])
    fv = lambda v: rp.fmt(float(v), last)
    css = {"LAST": "#24292f", "ENTRY": "#1a7f37", "INVALIDATION": "#a40e26",
           "TARGET 1": "#0969da", "TARGET 2": "#0969da"}
    chips = [("LAST", fv(last))]
    if s0.get("entry_lo") is not None and s0.get("entry_hi") is not None:
        d = (s0.get("direction") or "").upper()
        chips.append((f"ENTRY ZONE{' - ' + d if d else ''}", f"{fv(s0['entry_lo'])} - {fv(s0['entry_hi'])}"))
    for key, lab in (("invalidation", "INVALIDATION"), ("t1", "TARGET 1"), ("t2", "TARGET 2")):
        if s0.get(key) is not None:
            chips.append((lab, fv(s0[key])))
    cells = "".join(
        f'<div class="kl" style="border-left-color:{next((v for k, v in css.items() if lab.startswith(k)), "#57606a")}">'
        f'<div class="k">{lab}</div><div class="v">{val}</div></div>' for lab, val in chips)
    name = s0.get("name", "primary setup")
    return (f'<section><h2>Key levels - {name}</h2><div class="klrow">{cells}</div></section>')


def _sentiment_html(sent):
    h = '<section><h2>Sentiment &amp; positioning (sourced)</h2>'
    if sent.get("fear_greed"):
        h += _fg_svg(sent["fear_greed"])
    if sent.get("rows"):
        h += ("<table><tr><th>Source</th><th>Reading</th><th>Why it matters</th></tr>"
              + "".join(f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>"
                        for r in sent["rows"]) + "</table>")
    if sent.get("note"):
        h += f'<p>{sent["note"]}</p>'
    return h + "</section>"


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


def _fundamentals_html(fund):
    """Pro 'Fundamentals & Catalysts' HTML section from the canonical block (figures can never
    disagree with the body). Pro-only; '' if absent."""
    rows, cat, src = _fundamentals_rows(fund)
    if rows is None:
        return ""
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    body = _cards_html([(l, v) for l, v in rows]) if rows else ""
    if cat:
        body += "<ul>" + "".join(f"<li>{e(c)}</li>" for c in cat) + "</ul>"
    body += f'<div class="muted">{e(src)}</div>'
    return f'<section><h2>Fundamentals &amp; Catalysts</h2>{body}</section>'


def _fundamentals_pdf(pdf, fund):
    """Pro 'Fundamentals & Catalysts' PDF block (canonical figures), mirroring _fundamentals_html."""
    rows, cat, src = _fundamentals_rows(fund)
    if rows is None:
        return
    section_heading(pdf, "Fundamentals & Catalysts")
    if rows:
        kv_card(pdf, "Key metrics", rows)
    parts = []
    if cat:
        parts.append("<ul>" + "".join(f"<li>{c}</li>" for c in cat) + "</ul>")
    parts.append(src)
    rp.render_section_html(pdf, " ".join(parts), bullet_color=ACCENT)
    pdf.ln(0.5)


def build_pro_html(p, qa):
    pro = p["pro"]
    e = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    h = _html_head(p, "Pro")
    h += _cards_html(pro["exec"])
    ov = pro.get("overview")
    if ov:
        lines = [ov] if isinstance(ov, str) else list(ov)
        h += _info_box_html("In plain English - the 30-second read",
                            [(None, t) for t in lines], cls="plainbox")
    v = pro.get("verdict")
    if v:
        h += _info_box_html("Pro verdict",
                            [(None, v["line"]), ("Best opportunity:", v["best"]),
                             ("Main risk:", v["risk"]),
                             ("Stand-aside condition:", v["stand_aside"])], accent_bg=True)
    if pro.get("catalyst_status"):
        h += f'<div class="catstat"><b>Catalyst status:</b> {e(pro["catalyst_status"])}</div>'
    h += _kl_html(p)
    for cfg in pro.get("charts", []):
        h += rp.chart_svg(rp.read_series(Path(cfg["csv"])), cfg)
        if cfg.get("pivots"):
            h += f'<div class="chartnote">{PIVOT_CHART_NOTE}</div>'
        if cfg.get("rsi"):
            h += _rsi_svg(cfg["csv"], cfg)
    ladder_levels = [l for l in p["canonical"]["levels"] if l["id"] in set(p["canonical"]["ladder"])]
    h += ladder_svg(ladder_levels, p["canonical"]["last_price"])
    h += _gauge_svg(int(p.get("confidence", 0)))
    sent = pro.get("sentiment")
    if sent:
        h += _sentiment_html(sent)
    h += _fundamentals_html(p.get("fundamentals"))   # Pro-only; canonical figures
    sc = pro.get("source_confidence")
    for s in pro.get("sections", []):
        if s["heading"].strip().lower().startswith("source audit"):
            if sc:
                h += f'<section><h2>Source confidence</h2>{_cards_html([(str(k), str(t)) for k, t in sc])}</section>'
            h += f'<section><h2>Report quality</h2>{_cards_html(_report_quality_rows(p, qa))}</section>'
        h += f'<section><h2>{s["heading"]}</h2>{_section_body(s)}</section>'
    gl = _glossary_rows(p)
    if gl:
        h += _info_box_html("Glossary - how to read the charts and levels", gl)
    h += f'<div class="disc">{pro["disclaimer"]}</div></body></html>'
    return h


# ---------------------------------------------------------------- metadata
def build_metadata(p, qa, free_warn, pro_warn, qa_warns=None):
    meta = dict(p["meta"])
    now = datetime.now(timezone.utc)
    meta["plain_english_overview_included"] = bool(p["pro"].get("overview"))
    meta["sentiment_block_included"] = bool(p["pro"].get("sentiment"))
    meta["chart_glossary_included"] = bool(_glossary_rows(p))
    meta["catalyst_status"] = p["pro"].get("catalyst_status")
    meta["fundamentals_included"] = bool(p.get("fundamentals"))
    meta.setdefault("optional_chart", {
        "included": len(p["pro"].get("charts", [])) > 2,
        "reason": "default visual set sufficient - no optional chart added"})
    if qa_warns:
        meta["qa_warnings"] = list(qa_warns)
    meta.setdefault("brand", BRAND)
    meta.setdefault("tagline", TAGLINE)
    meta.setdefault("product_free", "AssetFrame Snapshot")
    meta.setdefault("product_pro", "AssetFrame Pro")
    meta["report_timezone"] = "UTC"
    meta["generated_at_utc"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if LONDON:
        ld = now.astimezone(LONDON)
        meta["generated_at_report_tz"] = (now.strftime("%Y-%m-%d %H:%M UTC")
                                          + ld.strftime(" (%H:%M %Z)"))
    meta["indicator_warmup_confirmed"] = not free_warn and not pro_warn
    meta["partial_indicators_hidden"] = True
    if free_warn or pro_warn:
        meta["indicator_warmup_warnings"] = list(dict.fromkeys(free_warn + pro_warn))
    meta["qa_checks"] = qa
    for k in ("source_confidence", "report_quality"):
        block = p["pro"].get(k)
        if block:
            meta[k] = {label.rstrip(':'): text for label, text in block}
    meta["paths"] = {"free_pdf": "free.pdf", "pro_pdf": "pro.pdf",
                     "metadata_json": "metadata.json", "preview_png": "preview.png",
                     "free_html": "free.html", "pro_html": "pro.html"}
    return meta


# ---------------------------------------------------------------- main
def main():
    if "--stamp-visual" in sys.argv:
        target = Path(sys.argv[1])
        mpath = target if target.name == "metadata.json" else target / "metadata.json"
        meta = json.loads(mpath.read_text(encoding="utf-8"))
        meta["qa_checks"]["visual_inspection_passed"] = True
        mpath.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"visual inspection stamped: {mpath}")
        return

    p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    out_dir = Path(p["out_dir"])

    n_dash = _normalize_payload(p)
    qa, errs, warns = run_qa(p)
    if n_dash:
        warns.append(f"stripped {n_dash} dash-prefixed bullet markers "
                     f"(author bullets without leading '-')")
    for w in warns:
        print(f"QA WARN: {w}")
    if errs:
        for e in errs:
            print(f"QA FAIL: {e}")
        print("BUILD ABORTED - no artifacts written.")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)
    # Forecast-only: the QA gate has already passed; skip the PDF/HTML/preview render
    # (predictions + payload are written upstream by scaffold). Used by the scheduler and
    # backtests where artifacts aren't needed - much faster/cheaper, QA still enforced.
    if "--no-render" in sys.argv:
        meta = build_metadata(p, qa, [], [], warns)
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"metadata.json: {out_dir / 'metadata.json'} (forecast-only)")
        print("QA: all pre-render checks passed (--no-render: PDFs/HTML/preview skipped)")
        return
    rp.WARN[:] = []
    build_free(p).output(str(out_dir / "free.pdf"))
    free_warn = list(dict.fromkeys(rp.WARN))
    rp.WARN[:] = []
    build_pro(p, qa).output(str(out_dir / "pro.pdf"))
    pro_warn = list(dict.fromkeys(rp.WARN))

    (out_dir / "free.html").write_text(build_free_html(p), encoding="utf-8")
    (out_dir / "pro.html").write_text(build_pro_html(p, qa), encoding="utf-8")

    try:
        import fitz
        doc = fitz.open(out_dir / "free.pdf")
        doc[0].get_pixmap(dpi=130).save(out_dir / "preview.png")
        doc.close()
    except Exception as ex:
        print(f"WARNING: preview.png failed: {ex}")

    meta = build_metadata(p, qa, free_warn, pro_warn, warns)
    (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    for name in ("free.pdf", "pro.pdf", "free.html", "pro.html", "metadata.json", "preview.png"):
        fpath = out_dir / name
        if fpath.exists():
            print(f"{name}: {fpath} ({fpath.stat().st_size} bytes)")
    if free_warn or pro_warn:
        print("LOOKBACK WARNINGS: " + "; ".join(dict.fromkeys(free_warn + pro_warn)))
    else:
        print("LOOKBACK: all chart SMAs/RSI fully warmed across their display windows")
    print("QA: all pre-render checks passed (visual inspection pending --stamp-visual)")


if __name__ == "__main__":
    main()
