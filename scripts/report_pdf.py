"""Advisor report PDF generator — fpdf2 native rendering (small PDFs, ~20-40KB, core fonts).

Usage: python scripts/report_pdf.py <payload.json>

Payload contract (unchanged from the Edge version):
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


def main():
    p = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))

    def respath(s):
        q = Path(s)
        return q if q.is_absolute() else (Path.cwd() / q)

    accent = hex_rgb(p["accent_color"]) if p.get("accent_color") else None
    brand_name = p.get("brand_name")
    logo = respath(p["logo_path"]) if p.get("logo_path") else None

    pdf = Report(orientation="P", unit="mm", format="A4")
    pdf.core_fonts_encoding = "windows-1252"
    pdf.set_margins(13, 12, 13)
    _brand = p.get("brand_name") or p.get("brand") or "AssetFrame"
    _yr = ("".join(c for c in str(p.get("report_id", "")) if c.isdigit())[:4]) or "2026"
    footer_bits = [_brand, p.get("report_id"),
                   "General market research - not personal advice",
                   f"© {_yr} {_brand}. All rights reserved."]
    pdf.meta_footer = S(" \xb7 ".join(b for b in footer_bits if b))
    pdf.alias_nb_pages()
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.add_page()

    # brand band (rendered only when branding fields are present)
    if brand_name or (logo and logo.exists()):
        by0 = pdf.get_y()
        if logo and logo.exists():
            try:
                pdf.image(str(logo), x=pdf.l_margin, y=by0, h=6)
            except Exception:
                pass  # a bad logo file must never block report generation
        if brand_name:
            pdf.set_font("helvetica", "B", 9.5)
            pdf.set_text_color(*(accent or DARK))
            pdf.set_xy(pdf.l_margin, by0 + 1)
            pdf.cell(0, 4, S(brand_name), align="R")
        pdf.set_draw_color(*(accent or LGRAY))
        pdf.set_line_width(0.5)
        pdf.line(pdf.l_margin, by0 + 7.2, pdf.w - pdf.r_margin, by0 + 7.2)
        pdf.set_y(by0 + 9)

    # header
    pdf.set_font("helvetica", "B", 15)
    pdf.set_text_color(*DARK)
    pdf.multi_cell(0, 6.5, S(p["title"]), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 8)
    pdf.set_text_color(*GRAY)
    pdf.multi_cell(0, 4, S(f'{p.get("subtitle", "")}  -  {p.get("datetime", "")}'), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    # chips
    y = pdf.get_y()
    x = pdf.l_margin
    for label, col in [(p.get("action", ""), ACTION_COLORS.get(p.get("action", "").lower(), GRAY)),
                       (f'Risk: {p.get("risk", "")}', RISK_COLORS.get(p.get("risk", "").lower(), GRAY))]:
        w = pdf.get_string_width(S(label)) + 8
        pdf.set_fill_color(*col)
        pdf.rect(x, y, w, 6, style="F", round_corners=True, corner_radius=2.6)
        pdf.set_font("helvetica", "B", 8.5)
        pdf.set_text_color(255, 255, 255)
        pdf.set_xy(x, y + 1)
        pdf.cell(w, 4, S(label), align="C")
        x += w + 4
    pdf.set_y(y + 8)
    # exec band - 2-column grid with wrapped values and dynamic row heights so long
    # fields (setups, ranges, key risks) never overflow into neighbouring cells.
    items = p.get("exec", [])
    cols = max(1, int(p.get("exec_cols", 2)))
    bx = pdf.l_margin
    bw = pdf.w - pdf.l_margin - pdf.r_margin
    cw = (bw - 6) / cols
    usable = cw - 4
    KEY_F, VAL_F, KEY_H, VAL_H = 6.2, 7.2, 2.7, 3.1

    def wrap_to(txt, size, width, max_lines=4):
        pdf.set_font("helvetica", "B", size)
        words = S(str(txt)).split()
        lines, cur = [], ""
        for w_ in words:
            cand = (cur + " " + w_).strip()
            if pdf.get_string_width(cand) <= width or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w_
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:max(len(lines[-1]) - 3, 0)] + "..."
        return lines or [""]

    cells = [(S(str(k)), wrap_to(v, VAL_F, usable)) for k, v in items]
    grid_rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    row_hts = [KEY_H + VAL_H * max(len(vl) for _, vl in r) + 1.8 for r in grid_rows]
    band_h = sum(row_hts) + 2.4
    pdf.need(band_h + 5)
    by = pdf.get_y()
    pdf.set_fill_color(246, 248, 250)
    pdf.set_draw_color(*(accent or LGRAY))
    pdf.set_line_width(0.25)
    pdf.rect(bx, by, bw, band_h, style="FD", round_corners=True, corner_radius=2)
    cy = by + 1.6
    for r, rh in zip(grid_rows, row_hts):
        for ci, (k, vlines) in enumerate(r):
            cx = bx + 3 + ci * cw
            pdf.set_font("helvetica", "", KEY_F)
            pdf.set_text_color(*GRAY)
            pdf.set_xy(cx, cy)
            pdf.cell(usable, KEY_H, k)
            pdf.set_font("helvetica", "B", VAL_F)
            pdf.set_text_color(*DARK)
            for li, ln in enumerate(vlines):
                pdf.set_xy(cx, cy + KEY_H + 0.3 + li * VAL_H)
                pdf.cell(usable, VAL_H, ln)
        cy += rh
    pdf.set_y(by + band_h + 3)

    # charts
    first_svg = None
    for cfg in p.get("charts", []):
        rows = read_series(respath(cfg["csv"]))
        if first_svg is None:
            first_svg = (rows, cfg)
        pdf.chart(rows, cfg)
        if cfg.get("rsi"):
            pdf.rsi_panel(rows, cfg)
    pdf.gauge(int(p.get("confidence", 0)), accent)

    # sections
    for s in p.get("sections", []):
        pdf.need(16)
        pdf.ln(1.5)
        pdf.set_font("helvetica", "B", 9.5)
        pdf.set_text_color(*(accent or DARK))
        pdf.cell(0, 5, S(s["heading"]), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*(accent or LGRAY))
        pdf.set_line_width(0.35)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(0.8)
        pdf.set_font("helvetica", "", 7.6)
        pdf.set_text_color(*DARK)
        render_section_html(pdf, s["html"])
        pdf.ln(0.5)

    # disclaimer
    pdf.ln(2)
    pdf.set_draw_color(*LGRAY)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(1)
    pdf.set_font("helvetica", "", 6.3)
    pdf.set_text_color(*GRAY)
    pdf.multi_cell(0, 2.9, S("Disclaimer: AI-generated market analysis and decision support - not regulated "
                             "financial advice, not a personal recommendation. Data may be delayed or incomplete; "
                             "markets are uncertain; you can lose money. Verify figures before acting; consider an "
                             "FCA-authorised adviser. This system never places trades."))

    out_pdf = respath(p["out_pdf"])
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_pdf))

    if p.get("chart_svg_out") and first_svg:
        out_svg = respath(p["chart_svg_out"])
        out_svg.parent.mkdir(parents=True, exist_ok=True)
        out_svg.write_text(chart_svg(*first_svg), encoding="utf-8")

    # HTML twin: same report as a single self-contained text file (uploadable to Drive,
    # opens in any browser, prints to PDF in one click).
    html_path = out_pdf.with_suffix(".html")
    html_path.write_text(build_html_twin(p, respath), encoding="utf-8")

    print(f"PDF: {out_pdf} ({out_pdf.stat().st_size} bytes)")
    print(f"HTML twin: {html_path} ({html_path.stat().st_size} bytes)")
    if WARN:
        print("LOOKBACK WARNINGS: " + "; ".join(dict.fromkeys(WARN)))
    else:
        print("LOOKBACK: all chart SMAs/RSI fully warmed across their display windows")


def build_html_twin(p, respath):
    risk_css = {"low": "#1a7f37", "medium": "#9a6700", "high": "#bc4c00", "very high": "#cf222e"}
    act_css = {"buy": "#1a7f37", "sell": "#cf222e", "hold": "#0969da", "wait": "#9a6700",
               "monitor": "#57606a", "no-trade": "#cf222e"}
    risk_col = risk_css.get(p.get("risk", "").lower(), "#57606a")
    act_col = act_css.get(p.get("action", "").lower(), "#57606a")
    e = html_mod.escape
    acc = p.get("accent_color")
    h2_rule = acc or "#d8dee4"
    h2_color = acc or "#24292f"
    gauge_fill = acc or "#24292f"
    brand_name = p.get("brand_name")
    logo_b64 = ""
    if p.get("logo_path"):
        lp = respath(p["logo_path"])
        if lp.exists():
            logo_b64 = base64.b64encode(lp.read_bytes()).decode()
    brand_html = ""
    if brand_name or logo_b64:
        img = f'<img src="data:image/png;base64,{logo_b64}" style="height:22px" alt="">' if logo_b64 else ""
        brand_html = f'<div class="brandbar">{img}<span>{e(brand_name or "")}</span></div>'
    charts_html = ""
    for cfg in p.get("charts", []):
        charts_html += chart_svg(read_series(respath(cfg["csv"])), cfg)
        if cfg.get("rsi"):
            rows_f = read_series(respath(cfg["csv"]))
            i0 = crop_index(rows_f, cfg.get("display_days"))
            rows = rows_f[i0:]
            line = rsi_line([r["c"] for r in rows_f])[i0:]
            pts = " ".join(f"{56 + 634 * i / max(len(rows) - 1, 1):.1f},{14 + 68 * (1 - v / 100):.1f}"
                           for i, v in enumerate(line) if v is not None)
            charts_html += (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 100" font-family="Arial" font-size="10">'
                            f'<text x="56" y="10" font-size="11" font-weight="600" fill="#24292f">RSI(14) {e(cfg.get("rsi_tag", ""))}</text>'
                            + "".join(f'<line x1="56" y1="{14 + 68 * (1 - l / 100):.1f}" x2="690" y2="{14 + 68 * (1 - l / 100):.1f}" stroke="{c}" stroke-width="0.7" stroke-dasharray="4,3"/>'
                                      f'<text x="50" y="{17 + 68 * (1 - l / 100):.1f}" text-anchor="end" fill="#57606a">{l}</text>'
                                      for l, c in ((70, "#cf222e"), (50, "#d8dee4"), (30, "#1a7f37")))
                            + f'<polyline points="{pts}" fill="none" stroke="#bc4c00" stroke-width="1.4"/></svg>')
    conf = int(p.get("confidence", 0))
    gauge = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 700 44" font-family="Arial" font-size="10">'
             '<text x="82" y="24" text-anchor="end" font-weight="600" fill="#24292f">Confidence</text>'
             + "".join(f'<rect x="{90 + 540 * a / 100:.1f}" y="12" width="{540 * (b - a) / 100:.1f}" height="14" fill="{c}" opacity="0.35" rx="2"/>'
                       for a, b, c in [(0, 20, "#cf222e"), (20, 40, "#bc4c00"), (40, 60, "#9a6700"),
                                       (60, 75, "#4d8a2a"), (75, 90, "#1a7f37"), (90, 100, "#116329")])
             + f'<rect x="90" y="12" width="{540 * conf / 100:.1f}" height="14" fill="{gauge_fill}" opacity="0.85" rx="2"/>'
             f'<text x="{96 + 540 * conf / 100:.1f}" y="24" font-weight="700" fill="#24292f">{conf}/100</text></svg>')
    exec_rows = "".join(f'<div class="kv"><span class="k">{e(str(k))}</span><span class="v">{e(str(v))}</span></div>'
                        for k, v in p.get("exec", []))
    sections = "".join(f'<section><h2>{e(s["heading"])}</h2>{s["html"]}</section>' for s in p.get("sections", []))
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{e(p["title"])}</title><style>
@page {{ size: A4; margin: 13mm; }} * {{ box-sizing: border-box; }}
body {{ font-family: Arial, sans-serif; color: #24292f; font-size: 10.2px; line-height: 1.45; margin: 0 auto; max-width: 760px; padding: 12px; }}
h1 {{ font-size: 20px; margin: 0; }} .sub {{ color: #57606a; font-size: 11px; margin: 2px 0 8px; }}
.chips span {{ display: inline-block; padding: 3px 12px; border-radius: 12px; color: #fff; font-weight: 700; font-size: 11px; margin-right: 8px; }}
.band {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 5px 22px; background: #f6f8fa; border: 1px solid {h2_rule}; border-radius: 8px; padding: 9px 13px; margin: 9px 0; }}
.brandbar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid {h2_rule}; padding-bottom: 4px; margin-bottom: 6px; font-weight: 700; font-size: 13px; color: {h2_color}; }}
.kv {{ display: flex; justify-content: space-between; gap: 6px; }} .kv .k {{ color: #57606a; }} .kv .v {{ font-weight: 600; text-align: right; }}
section {{ margin: 8px 0; break-inside: avoid-page; }}
section h2 {{ font-size: 12.5px; color: {h2_color}; border-bottom: 1.5px solid {h2_rule}; padding-bottom: 2px; margin: 0 0 4px; }}
ul {{ margin: 2px 0 2px 16px; padding: 0; }} li {{ margin: 1.5px 0; }}
table {{ border-collapse: collapse; width: 100%; margin: 3px 0; }} th, td {{ border: 1px solid #d8dee4; padding: 3px 7px; text-align: left; vertical-align: top; }} th {{ background: #f6f8fa; }}
.disc {{ border-top: 1.5px solid #d8dee4; margin-top: 11px; padding-top: 6px; font-size: 8.6px; color: #57606a; }}
b.up {{ color: #1a7f37; }} b.dn {{ color: #cf222e; }}
@media screen and (max-width: 640px) {{ body {{ font-size: 14px; padding: 16px; max-width: 100%; }} table {{ display: block; overflow-x: auto; }} }}
</style></head><body>
{brand_html}<h1>{e(p["title"])}</h1><div class="sub">{e(p.get("subtitle", ""))} &middot; {e(p.get("datetime", ""))}</div>
<div class="chips"><span style="background:{act_col}">{e(p.get("action", ""))}</span><span style="background:{risk_col}">Risk: {e(p.get("risk", ""))}</span></div>
<div class="band">{exec_rows}</div>
{charts_html}{gauge}
{sections}
<div class="disc"><b>Disclaimer:</b> AI-generated market analysis and decision support — not regulated financial advice, not a personal recommendation. Data may be delayed or incomplete; markets are uncertain; you can lose money. Verify figures before acting; consider an FCA-authorised adviser. This system never places trades.</div>
</body></html>"""


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


if __name__ == "__main__":
    main()
