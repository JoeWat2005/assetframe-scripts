"""AssetFrame Pro/Snapshot PDF renderers (fpdf2), extracted from mvp_report. Imports the shared leaf only."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp
from mvp_report_const import (BRAND, TAGLINE, LOGO, FREE_CHART_NOTE, PIVOT_CHART_NOTE, LADDER_LEGEND,
    ladder_geometry, _ladder_dp, _pct_from, _section_body, _glossary_rows, _report_quality_rows, _fundamentals_rows)

ACCENT = (11, 37, 69)         # logo navy
ACCENT_FILL = (233, 238, 246)
STATUS_COLORS = {"buy": rp.GREEN, "sell": rp.RED, "wait": rp.AMBER,
                 "stand aside": rp.GRAY, "neutral": rp.BLUE}
LADDER_COLORS = {"tail": rp.GRAY, "resistance": rp.RED, "target": rp.BLUE,
                 "trigger": rp.AMBER, "entry": rp.GREEN, "support": (87, 96, 106),
                 "invalidation": (164, 14, 38), "last": rp.DARK}

FG_ZONES_PDF = [(0, 25, (207, 34, 46)), (25, 45, (188, 76, 0)), (45, 55, (154, 103, 0)),
                (55, 75, (77, 138, 42)), (75, 100, (26, 127, 55))]


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
