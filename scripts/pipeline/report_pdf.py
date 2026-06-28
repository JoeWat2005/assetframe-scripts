"""Advisor report chart + section rendering library (fpdf2 native, small PDFs, core fonts).

Imported by mvp_report (chart_svg / read_series / prep_chart / Report / render_section_html). The
standalone single-report CLI (main / build_html_twin) was removed — mvp_report is the generator now.

Chart cfg contract (consumed by prep_chart / chart_svg):
{
  "title", "subtitle", "datetime", "out_pdf",
  "brand_name": "...",            # optional: header band + footer (falls back to "brand")
  "logo_path": "assets/logo.png", # optional: PNG drawn ~6mm tall in the header band
  "accent_color": "#0B3D6E",      # optional: headings/rules/exec border/gauge fill;
                                  # action/risk chips KEEP their semantic colors
  "exec": [["k","v"],...], "action": "Wait", "risk": "High", "confidence": 50,
  "charts": [
    {"csv": path, "label": str, "height": px (300 ~= 80mm),
     "smas": [20,50], "support": [..], "resistance": [..],
     "pivots": {"PP":..}, "bands": [{"lo","hi","color","label"}],
     "rsi": true, "rsi_tag": "hourly", "xfmt": "auto|date|datetime"}
  ],
  "chart_svg_out": path,   # standalone SVG of the first chart (for Drive)
  "sections": [{"heading","html"},...]   # html: ul/li/table/b/font; class up/dn -> colors
}
CSV rows: date[,time],open,high,low,close[,volume]; header optional, any order.

Small-PDF rationale: PDF core fonts (Helvetica) embed nothing, so a full report stays
small enough to upload to Google Drive through the MCP connector as base64.
"""
import base64, csv, html as html_mod, json, re, sys
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

GREEN, RED, BLUE, PURPLE, ORANGE, GRAY, LGRAY, DARK, AMBER = \
    (26, 127, 55), (207, 34, 46), (9, 105, 218), (130, 80, 223), (188, 76, 0), \
    (87, 96, 106), (216, 222, 228), (36, 41, 47), (154, 103, 0)
RISK_COLORS = {"low": GREEN, "medium": AMBER, "high": ORANGE, "very high": RED}
ACTION_COLORS = {"buy": GREEN, "sell": RED, "hold": BLUE, "wait": AMBER,
                 "monitor": GRAY, "no-trade": RED}
SMA_COLORS = [BLUE, PURPLE, ORANGE]

CHAR_MAP = {"→": "->", "←": "<-", "↑": "^", "↓": "v", "≈": "~",
            "≤": "<=", "≥": ">=", "−": "-", "×": "x", "✓": "OK",
            "✔": "OK", "✖": "x", "≊": "~", "…": "...", "✕": "x",
            "–": "-", "—": "-", " ": " "}

WARN = []  # lookback/render warnings, printed at the end of the run


def S(text):
    """Sanitize to cp1252 (core-font charset)."""
    for k, v in CHAR_MAP.items():
        text = text.replace(k, v)
    return text.encode("cp1252", errors="replace").decode("cp1252")


def hex_rgb(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def read_series(path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) >= 5 and r[0][:2].isdigit():
                rows.append({"d": r[0], "o": float(r[1]), "h": float(r[2]),
                             "l": float(r[3]), "c": float(r[4])})
    rows.sort(key=lambda x: x["d"])
    return rows


def sma_line(closes, n):
    return [sum(closes[i - n + 1:i + 1]) / n if i >= n - 1 else None for i in range(len(closes))]


def rsi_line(closes):
    n = 14
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    ag = sum(max(closes[i] - closes[i - 1], 0) for i in range(1, n + 1)) / n
    al = sum(max(closes[i - 1] - closes[i], 0) for i in range(1, n + 1)) / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag, al = (ag * 13 + max(d, 0)) / 14, (al * 13 + max(-d, 0)) / 14
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def fmt(v, ref):
    # Decimal half-up: float .1f rounds half-even on binary values (1707.55 ->
    # "1707.5"), which silently disagrees with human-rounded authored text
    a = abs(ref)
    dp = 4 if a < 50 else (2 if a < 500 else 1)
    q = Decimal(str(v)).quantize(Decimal("1." + "0" * dp), rounding=ROUND_HALF_UP)
    return f"{q:,f}"


def fmt_level(v, ref, plain=False):
    """Plain-English chart labels round large prices to whole numbers
    ("Support 1,646"); small refs (FX) keep fmt precision."""
    if plain and abs(ref) >= 500:
        q = Decimal(str(v)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return f"{q:,f}"
    return fmt(v, ref)


def parse_dt_str(d):
    return (datetime.strptime(d[:16], "%Y-%m-%d %H:%M") if len(d) > 10
            else datetime.strptime(d[:10], "%Y-%m-%d"))


def crop_index(rows, display_days):
    """Index of the first bar inside the display window (bars before it are warm-up)."""
    if not display_days or not rows:
        return 0
    cutoff = parse_dt_str(rows[-1]["d"]) - timedelta(days=float(display_days))
    for i, r in enumerate(rows):
        if parse_dt_str(r["d"]) >= cutoff:
            return i
    return 0


def prep_chart(rows, cfg):
    """Compute indicators on the FULL series, then crop everything to the display
    window. Returns (disp_rows, sma_segs, rsi_seg, notes) where sma_segs is a list of
    (n, cropped_line, status) with status ok|partial|missing. A cold SMA is never
    silently drawn as if it were warm: partial segments are drawn only where valid
    and disclosed; missing ones are dropped from the plot and disclosed."""
    idx = crop_index(rows, cfg.get("display_days"))
    disp = rows[idx:]
    closes_full = [r["c"] for r in rows]
    segs = []
    for n in cfg.get("smas", []):
        seg = sma_line(closes_full, n)[idx:]
        if all(v is None for v in seg):
            status = "missing"
        elif seg[0] is None:
            status = "partial"
        else:
            status = "ok"
        segs.append((n, seg, status))
    rsi_seg = rsi_line(closes_full)[idx:] if cfg.get("rsi") else None
    if rsi_seg is not None and rsi_seg and rsi_seg[0] is None:
        WARN.append(f"RSI14 partial in display window ({cfg.get('csv', '?')})")
    notes = [f"SMA{n} {'unavailable' if st == 'missing' else 'starts late'} - insufficient lookback"
             for n, _, st in segs if st != "ok"]
    for note in notes:
        WARN.append(f"{note} ({cfg.get('csv', '?')})")
    return disp, segs, rsi_seg, notes


def xlabel(d, mode):
    if mode == "datetime" or (mode == "auto" and len(d) > 10):
        return d[5:13].replace("-", "/")
    return d[5:10]


class Report(FPDF):
    meta_footer = ""

    def footer(self):
        if not self.meta_footer:
            return
        self.set_y(-9)
        self.set_font("helvetica", "", 6.2)
        self.set_text_color(*GRAY)
        self.set_draw_color(*LGRAY)
        self.set_line_width(0.2)
        self.line(self.l_margin, self.get_y() - 1, self.w - self.r_margin, self.get_y() - 1)
        self.cell(0, 3, self.meta_footer)
        self.set_x(self.w - self.r_margin - 22)
        self.cell(22, 3, f"Page {self.page_no()}/{{nb}}", align="R")

    def need(self, h):
        if self.get_y() + h > self.page_break_trigger:
            self.add_page()

    def chart(self, rows, cfg):
        H = int(cfg.get("height", 300)) * 80 / 300  # px -> mm (300px ~ 80mm)
        self.need(H + 9)
        x0, y0 = self.l_margin, self.get_y()
        W = self.w - self.l_margin - self.r_margin
        PL, PR, PT, PB = 14, 2, 10, 5  # PT reserves a full title row + legend row
        cw, ch = W - PL - PR, H - PT - PB
        used_l, used_r = [], []  # label-collision registries per side

        def place(used, y, cap=None):
            while any(abs(y - u) < 2.7 for u in used):
                y += 2.7
            if cap is not None and y > cap:  # never collide with the x-axis label row
                y = cap
                while any(abs(y - u) < 2.7 for u in used):
                    y -= 2.7
            used.append(y)
            return y
        # indicators on the FULL series, display cropped to the configured window
        rows, segs, _, notes = prep_chart(rows, cfg)
        closes = [r["c"] for r in rows]
        bar_lo, bar_hi = min(r["l"] for r in rows), max(r["h"] for r in rows)
        bspan = (bar_hi - bar_lo) or 1
        # levels far beyond the traded range would crush the candles - keep only
        # those within 35% of the bar span outside it (others remain in tables)
        levels = [p for p in (list(cfg.get("support", [])) + list(cfg.get("resistance", [])) +
                              [b["lo"] for b in cfg.get("bands", [])] +
                              [b["hi"] for b in cfg.get("bands", [])] +
                              list((cfg.get("pivots") or {}).values()))
                  if bar_lo - 0.35 * bspan <= p <= bar_hi + 0.35 * bspan]
        lo = min(bar_lo, *(levels or [closes[-1]]))
        hi = max(bar_hi, *(levels or [closes[-1]]))
        span = (hi - lo) or 1
        lo, hi = lo - span * 0.04, hi + span * 0.04

        def X(i):
            return x0 + PL + cw * i / max(len(rows) - 1, 1)

        def Y(p):
            return y0 + PT + ch * (1 - (p - lo) / (hi - lo))

        self.set_font("helvetica", "B", 8.2)
        self.set_text_color(*DARK)
        self.set_xy(x0 + PL, y0)
        self.cell(cw, 4, S(cfg.get("label", "")))
        # bands (shaded)
        for b in cfg.get("bands", []):
            col = hex_rgb(b.get("color", "#9a6700"))
            y1, y2 = Y(b["hi"]), Y(b["lo"])
            with self.local_context(fill_opacity=0.13):
                self.set_fill_color(*col)
                self.rect(x0 + PL, y1, cw, max(y2 - y1, 0.4), style="F")
            self.set_font("helvetica", "B", 6)
            self.set_text_color(*col)
            self.set_xy(x0 + PL + 1, place(used_l, y1 + 0.4, cap=y0 + PT + ch - 2.8))
            self.cell(cw - 2, 2.6, S(b.get("label", "")))
        # grid + y labels
        self.set_line_width(0.12)
        self.set_draw_color(*LGRAY)
        self.set_font("helvetica", "", 6.5)
        self.set_text_color(*GRAY)
        for k in range(5):
            p = lo + (hi - lo) * k / 4
            self.line(x0 + PL, Y(p), x0 + PL + cw, Y(p))
            self.set_xy(x0, Y(p) - 1.3)
            self.cell(PL - 1.5, 2.6, fmt(p, closes[-1]), align="R")
        for i in range(0, len(rows), max(len(rows) // 4, 1)):
            self.set_xy(X(i) - 8, y0 + PT + ch + 0.8)
            self.cell(16, 2.6, xlabel(rows[i]["d"], cfg.get("xfmt", "auto")), align="C")
        # candles
        bw = max(min(cw / len(rows) * 0.65, 1.6), 0.25)
        self.set_line_width(0.15)
        for i, r in enumerate(rows):
            x, up = X(i), r["c"] >= r["o"]
            col = GREEN if up else RED
            self.set_draw_color(*col)
            self.set_fill_color(*col)
            self.line(x, Y(r["h"]), x, Y(r["l"]))
            yo, yc = Y(r["o"]), Y(r["c"])
            self.rect(x - bw / 2, min(yo, yc), bw, max(abs(yc - yo), 0.25), style="F")
        # SMAs - computed on the full series, drawn only where warmed/valid
        legend = []
        self.set_line_width(0.35)
        for j, (n, seg, status) in enumerate(segs):
            col = SMA_COLORS[j % len(SMA_COLORS)]
            if status == "missing":
                legend.append((f"SMA{n} n/a", GRAY))
                continue
            self.set_draw_color(*col)
            pts = [(X(i), Y(v)) for i, v in enumerate(seg) if v is not None]
            for a, b in zip(pts, pts[1:]):
                self.line(a[0], a[1], b[0], b[1])
            legend.append((f"SMA{n}" + ("*" if status == "partial" else ""), col))
        # S/R dashed
        plain = cfg.get("label_style") == "plain"
        lw_lab = 32 if plain else 24
        self.set_line_width(0.25)
        self.set_font("helvetica", "B", 6.5)
        for p, col, tag in [(p, GREEN, "Support" if plain else "S") for p in cfg.get("support", [])] + \
                           [(p, RED, "Resistance" if plain else "R") for p in cfg.get("resistance", [])]:
            if lo < p < hi:
                self.set_draw_color(*col)
                self.set_text_color(*col)
                with self.local_context():
                    self.set_dash_pattern(dash=1.4, gap=1.1)
                    self.line(x0 + PL, Y(p), x0 + PL + cw, Y(p))
                self.set_xy(x0 + PL + cw - lw_lab, place(used_r, Y(p) - 2.7, cap=y0 + PT + ch - 2.8))
                self.cell(lw_lab - 1, 2.6, f"{tag} {fmt_level(p, closes[-1], plain)}", align="R")
        # pivots dotted gray
        self.set_font("helvetica", "", 6)
        for name, p in (cfg.get("pivots") or {}).items():
            if lo < p < hi:
                self.set_draw_color(*GRAY)
                self.set_text_color(*GRAY)
                self.set_line_width(0.3 if name == "PP" else 0.18)
                with self.local_context():
                    self.set_dash_pattern(dash=0.5, gap=0.9)
                    self.line(x0 + PL, Y(p), x0 + PL + cw, Y(p))
                self.set_xy(x0 + PL + 0.5, place(used_l, Y(p) - 2.5, cap=y0 + PT + ch - 2.8))
                self.cell(24, 2.4, f"{name} {fmt(p, closes[-1])}")
        # legend on its own row under the title (never over the plot title)
        self.set_font("helvetica", "", 6.5)
        widths = [self.get_string_width(n) + 5.5 for n, _ in legend]
        lx = x0 + PL + cw - 26 - sum(widths)
        cx = lx
        for (n, col), wd in zip(legend, widths):
            self.set_fill_color(*col)
            self.rect(cx, y0 + 5.4, 3, 0.9, style="F")
            self.set_text_color(*DARK)
            self.set_xy(cx + 3.5, y0 + 4.4)
            self.cell(wd - 3.5, 2.6, n)
            cx += wd
        self.set_text_color(*GRAY)
        self.set_xy(x0 + PL + cw - 24, y0 + 4.4)
        self.cell(24, 2.6, f"last {fmt(closes[-1], closes[-1])}", align="R")
        # disclose any cold-start indicator directly on the chart
        if notes:
            self.set_font("helvetica", "I", 5.8)
            self.set_text_color(*GRAY)
            self.set_xy(x0 + PL, y0 + 4.4)
            self.cell(max(lx - x0 - PL - 2, 10), 2.6, S("; ".join(notes)))
        self.set_dash_pattern()
        self.set_y(y0 + H + 2)

    def rsi_panel(self, rows, cfg):
        tag = cfg.get("rsi_tag", "")
        H = 24
        self.need(H + 4)
        x0, y0 = self.l_margin, self.get_y()
        W = self.w - self.l_margin - self.r_margin
        PL, PR, PT, PB = 14, 2, 4, 4
        cw, ch = W - PL - PR, H - PT - PB
        # RSI computed on the full series, cropped to the display window (warm start)
        idx = crop_index(rows, cfg.get("display_days"))
        line = rsi_line([r["c"] for r in rows])[idx:]
        rows = rows[idx:]

        def X(i):
            return x0 + PL + cw * i / max(len(rows) - 1, 1)

        def Y(v):
            return y0 + PT + ch * (1 - v / 100)

        self.set_font("helvetica", "B", 8)
        self.set_text_color(*DARK)
        self.set_xy(x0 + PL, y0)
        self.cell(cw, 4, S(f"RSI(14) {tag}"))
        self.set_font("helvetica", "", 6.5)
        for lvl, col in ((70, RED), (50, LGRAY), (30, GREEN)):
            self.set_draw_color(*col)
            self.set_line_width(0.15)
            with self.local_context():
                self.set_dash_pattern(dash=1.1, gap=0.9)
                self.line(x0 + PL, Y(lvl), x0 + PL + cw, Y(lvl))
            self.set_text_color(*GRAY)
            self.set_xy(x0, Y(lvl) - 1.3)
            self.cell(PL - 1.5, 2.6, str(lvl), align="R")
        self.set_draw_color(*ORANGE)
        self.set_line_width(0.35)
        pts = [(X(i), Y(v)) for i, v in enumerate(line) if v is not None]
        for a, b in zip(pts, pts[1:]):
            self.line(a[0], a[1], b[0], b[1])
        last = next((v for v in reversed(line) if v is not None), None)
        if last is not None:
            self.set_font("helvetica", "B", 7)
            self.set_text_color(*ORANGE)
            self.set_xy(x0 + PL + cw - 10, Y(last) - 3)
            self.cell(10, 2.6, f"{last:.0f}", align="R")
        self.set_dash_pattern()
        self.set_y(y0 + H + 1)

    def gauge(self, conf, accent=None):
        self.need(14)
        x0, y0 = self.l_margin, self.get_y()
        gx, gw = x0 + 26, self.w - self.l_margin - self.r_margin - 30
        self.set_font("helvetica", "B", 8)
        self.set_text_color(*DARK)
        self.set_xy(x0, y0 + 1)
        self.cell(24, 4, "Confidence", align="R")
        for a, b, col in [(0, 20, RED), (20, 40, ORANGE), (40, 60, AMBER),
                          (60, 75, (77, 138, 42)), (75, 90, GREEN), (90, 100, (17, 99, 41))]:
            with self.local_context(fill_opacity=0.35):
                self.set_fill_color(*col)
                self.rect(gx + gw * a / 100, y0 + 1, gw * (b - a) / 100, 4, style="F", round_corners=True, corner_radius=0.5)
        with self.local_context(fill_opacity=0.85):
            self.set_fill_color(*(accent or DARK))
            self.rect(gx, y0 + 1, gw * conf / 100, 4, style="F", round_corners=True, corner_radius=0.5)
        self.set_xy(gx + gw * conf / 100 + 1.5, y0 + 1)
        self.cell(16, 4, f"{conf}/100")
        self.set_font("helvetica", "", 5.5)
        self.set_text_color(150, 153, 159)
        for t in (0, 20, 40, 60, 75, 90, 100):
            self.set_xy(gx + gw * t / 100 - 3, y0 + 5.6)
            self.cell(6, 2.4, str(t), align="C")
        self.set_y(y0 + 10)


INLINE_TOKEN = re.compile(r'(<b class="up">|<b class="dn">|<b>|</b>|<br\s*/?>)')


def plain(s):
    return html_mod.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def render_inline(pdf, htext, h=3.5, size=7.6, indent=2.5):
    """Render text with <b>, <b class='up'/'dn'> runs; everything else stripped.
    Wraps at word boundaries only - never inside a number or word, even when a
    style run starts near the right margin. `indent` is the hanging indent that
    wrapped lines (and <br> breaks) return to."""
    htext = htext.replace("<code>", "<b>").replace("</code>", "</b>")
    bold, color = False, DARK
    maxx = pdf.w - pdf.r_margin
    # write() wraps internally at (w - r_margin - c_margin); zero c_margin here so
    # its boundary equals ours exactly and the guard below is authoritative
    saved_cm, pdf.c_margin = pdf.c_margin, 0
    for tok in INLINE_TOKEN.split(htext):
        if tok == "<b>":
            bold = True
        elif tok == '<b class="up">':
            bold, color = True, GREEN
        elif tok == '<b class="dn">':
            bold, color = True, RED
        elif tok == "</b>":
            bold, color = False, DARK
        elif tok and re.fullmatch(r"<br\s*/?>", tok):
            pdf.ln(h)
            pdf.set_x(pdf.l_margin + indent)
        elif tok:
            txt = html_mod.unescape(re.sub(r"<[^>]+>", "", tok))
            if not txt:
                continue
            pdf.set_font("helvetica", "B" if bold else "", size)
            pdf.set_text_color(*color)
            for w_ in re.split(r"(\s+)", S(txt)):
                if not w_:
                    continue
                wd = pdf.get_string_width(w_)
                if w_.isspace():
                    if pdf.get_x() + wd <= maxx - 0.3:
                        pdf.write(h, w_)
                    continue
                if pdf.get_x() + wd > maxx - 0.3:
                    pdf.ln(h)
                    pdf.set_x(pdf.l_margin + indent)
                pdf.write(h, w_)
    pdf.c_margin = saved_cm
    pdf.set_text_color(*DARK)


NUM_CELL = re.compile(
    r"^[~+\-]?[$£€]?[\d,]+(\.\d+)?\s*(%|x|bp|bps|pts?)?$|^n/a$|^-$", re.I)


def render_section_html(pdf, html_src, bullet_color=None):
    """Deterministic mini-renderer: <ul><li> bullets and <table> via fpdf2 native tables.
    Bullets render as coloured glyphs with a hanging indent (never a bare '-').
    Tables are normalised: bold+filled header row, zebra body rows, numeric
    columns right-aligned so figures line up, fully-<b> cells carried as bold."""
    pos = 0
    for m in re.finditer(r"<(ul|table)[^>]*>(.*?)</\1>", html_src, flags=re.S):
        lead = html_src[pos:m.start()].strip()
        if plain(lead):
            pdf.set_x(pdf.l_margin)
            render_inline(pdf, lead)
            pdf.ln(4.2)
        if m.group(1) == "ul":
            for li in re.findall(r"<li[^>]*>(.*?)</li>", m.group(2), flags=re.S):
                pdf.set_x(pdf.l_margin + 1.2)
                pdf.set_font("helvetica", "B", 7.6)
                pdf.set_text_color(*(bullet_color or DARK))
                pdf.write(3.5, S("• "))
                pdf.set_x(pdf.l_margin + 5.0)
                render_inline(pdf, li, indent=5.0)
                pdf.ln(4.4)
        else:
            rows, rows_raw = [], []
            header = False
            for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(2), flags=re.S):
                cells = re.findall(r"<(th|td)[^>]*>(.*?)</\1>", tr, flags=re.S)
                if cells and cells[0][0] == "th":
                    header = True
                rows.append([plain(c[1]) for c in cells])
                rows_raw.append([c[1] for c in cells])
            if rows:
                pdf.set_font("helvetica", "", 6.9)
                pdf.set_text_color(*DARK)
                pdf.set_draw_color(*LGRAY)
                pdf.set_fill_color(246, 248, 250)
                pdf.set_line_width(0.2)
                # width-accurate columns: floor each column at its longest word's
                # MEASURED width (so "Primary"/"Bullish" can never split mid-word),
                # then distribute the remaining width by content length
                ncols = max(len(r) for r in rows)
                avail = pdf.w - pdf.l_margin - pdf.r_margin
                pdf.set_font("helvetica", "B", 6.9)  # headers are bold = widest
                minw, flex = [], []
                for i in range(ncols):
                    words = [w_ for r in rows if i < len(r) for w_ in r[i].split()]
                    longest = max((pdf.get_string_width(S(w_)) for w_ in words),
                                  default=3)
                    minw.append(min(longest, 0.4 * avail) + 3.0)  # + cell padding
                    flex.append(min(max((len(r[i]) if i < len(r) else 0)
                                        for r in rows), 60))
                spare = avail - sum(minw)
                if spare > 0:
                    ftot = sum(flex) or 1
                    col_mm = [m + spare * f / ftot for m, f in zip(minw, flex)]
                else:  # pathological: fall back to pure minimums (may wrap hard)
                    col_mm = minw
                pdf.set_font("helvetica", "", 6.9)
                col_w = tuple(round(w_, 2) for w_ in col_mm)
                body = rows[1:] if header and len(rows) > 1 else rows
                aligns = []
                for i in range(ncols):
                    vals = [r[i].strip() for r in body if i < len(r) and r[i].strip()]
                    num = sum(1 for v in vals if NUM_CELL.match(v))
                    aligns.append("RIGHT" if vals and num / len(vals) >= 0.7 else "LEFT")
                with pdf.table(first_row_as_headings=header, line_height=3.4,
                               text_align=tuple(aligns), borders_layout="ALL",
                               col_widths=col_w, padding=(0.9, 1.1, 0.9, 1.1),
                               v_align="TOP",
                               headings_style=FontFace(emphasis="BOLD",
                                                       fill_color=(233, 238, 246)),
                               cell_fill_color=(250, 251, 252),
                               cell_fill_mode="ROWS") as table:
                    for ri, r in enumerate(rows):
                        row = table.row()
                        for ci, c in enumerate(r):
                            raw = rows_raw[ri][ci] if ci < len(rows_raw[ri]) else ""
                            emph = (not (header and ri == 0)
                                    and re.fullmatch(r"\s*<b[^>]*>.*?</b>\s*", raw, flags=re.S))
                            row.cell(S(c), style=FontFace(emphasis="BOLD") if emph else None)
                pdf.ln(1)
        pos = m.end()
    tail = html_src[pos:].strip()
    if plain(tail):
        pdf.set_x(pdf.l_margin)
        render_inline(pdf, tail)
        pdf.ln(4.2)


def chart_svg(rows, cfg):
    """Standalone SVG of a chart (for Drive exports/charts/)."""
    W, H = 700, int(cfg.get("height", 300))
    PL, PR, PT, PB = 56, 10, 24, 22
    cw, ch = W - PL - PR, H - PT - PB
    rows, segs, _, notes = prep_chart(rows, cfg)
    closes = [r["c"] for r in rows]
    bar_lo, bar_hi = min(r["l"] for r in rows), max(r["h"] for r in rows)
    bspan = (bar_hi - bar_lo) or 1
    levels = [p for p in (list(cfg.get("support", [])) + list(cfg.get("resistance", [])) +
                          [b["lo"] for b in cfg.get("bands", [])] +
                          [b["hi"] for b in cfg.get("bands", [])] +
                          list((cfg.get("pivots") or {}).values()))
              if bar_lo - 0.35 * bspan <= p <= bar_hi + 0.35 * bspan]
    lo = min(bar_lo, *(levels or [closes[-1]]))
    hi = max(bar_hi, *(levels or [closes[-1]]))
    span = (hi - lo) or 1
    lo, hi = lo - span * 0.04, hi + span * 0.04

    def X(i):
        return PL + cw * i / max(len(rows) - 1, 1)

    def Y(p):
        return PT + ch * (1 - (p - lo) / (hi - lo))

    e = html_mod.escape
    P = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="Arial" font-size="10" '
         f'role="img" aria-label="{e(cfg.get("label", "Price chart"))}">',
         f'<text x="{PL}" y="14" font-size="12" font-weight="600" fill="#24292f">{e(cfg.get("label", ""))}</text>']
    if notes:
        P.append(f'<text x="{W - PR}" y="14" text-anchor="end" font-size="8.5" '
                 f'font-style="italic" fill="#57606a">{e("; ".join(notes))}</text>')
    for b in cfg.get("bands", []):
        y1, y2 = Y(b["hi"]), Y(b["lo"])
        P.append(f'<rect x="{PL}" y="{y1:.1f}" width="{cw}" height="{max(y2-y1,1):.1f}" fill="{b.get("color","#9a6700")}" opacity="0.13"/>')
        P.append(f'<text x="{PL+4}" y="{y1+10:.1f}" fill="{b.get("color","#9a6700")}" font-size="8.5" font-weight="600">{e(b.get("label",""))}</text>')
    for k in range(5):
        pv = lo + (hi - lo) * k / 4
        P.append(f'<line x1="{PL}" y1="{Y(pv):.1f}" x2="{W-PR}" y2="{Y(pv):.1f}" stroke="#d8dee4" stroke-width="0.6"/>')
        P.append(f'<text x="{PL-6}" y="{Y(pv)+3:.1f}" text-anchor="end" fill="#57606a">{fmt(pv, closes[-1])}</text>')
    for i in range(0, len(rows), max(len(rows) // 4, 1)):
        P.append(f'<text x="{X(i):.1f}" y="{H-6}" text-anchor="middle" fill="#57606a">{xlabel(rows[i]["d"], cfg.get("xfmt","auto"))}</text>')
    bw = max(min(cw / len(rows) * 0.65, 7), 1.0)
    for i, r in enumerate(rows):
        x, up = X(i), r["c"] >= r["o"]
        col = "#1a7f37" if up else "#cf222e"
        P.append(f'<line x1="{x:.1f}" y1="{Y(r["h"]):.1f}" x2="{x:.1f}" y2="{Y(r["l"]):.1f}" stroke="{col}" stroke-width="0.8"/>')
        yo, yc = Y(r["o"]), Y(r["c"])
        P.append(f'<rect x="{x-bw/2:.1f}" y="{min(yo,yc):.1f}" width="{bw:.1f}" height="{max(abs(yc-yo),0.8):.1f}" fill="{col}"/>')
    for j, (n, seg, status) in enumerate(segs):
        col = ["#0969da", "#8250df", "#bc4c00"][j % 3]
        if status == "missing":
            continue
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(seg) if v is not None)
        if pts:
            P.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.4"/>')
    plain = cfg.get("label_style") == "plain"
    for pv, col, tag in [(pv, "#1a7f37", "Support" if plain else "S") for pv in cfg.get("support", [])] + \
                        [(pv, "#cf222e", "Resistance" if plain else "R") for pv in cfg.get("resistance", [])]:
        if lo < pv < hi:
            P.append(f'<line x1="{PL}" y1="{Y(pv):.1f}" x2="{W-PR}" y2="{Y(pv):.1f}" stroke="{col}" stroke-width="1" stroke-dasharray="5,4"/>')
            P.append(f'<text x="{W-PR-2}" y="{Y(pv)-3:.1f}" text-anchor="end" fill="{col}" font-weight="600">{tag} {fmt_level(pv, closes[-1], plain)}</text>')
    for name, pv in (cfg.get("pivots") or {}).items():
        if lo < pv < hi:
            wd = "1.2" if name == "PP" else "0.8"
            P.append(f'<line x1="{PL}" y1="{Y(pv):.1f}" x2="{W-PR}" y2="{Y(pv):.1f}" stroke="#57606a" stroke-width="{wd}" stroke-dasharray="2,3"/>')
            P.append(f'<text x="{PL+2}" y="{Y(pv)-2:.1f}" fill="#57606a" font-size="8.5">{name} {fmt(pv, closes[-1])}</text>')
    P.append("</svg>")
    return "".join(P)


