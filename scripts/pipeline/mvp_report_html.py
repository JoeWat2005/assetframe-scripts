"""AssetFrame Pro/Snapshot HTML twins + inline SVG charts (extracted from mvp_report). Imports the shared leaf only."""
import base64, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp
from mvp_report_const import (BRAND, TAGLINE, LOGO, FREE_CHART_NOTE, PIVOT_CHART_NOTE, LADDER_LEGEND,
    ladder_geometry, _ladder_dp, _pct_from, _section_body, _glossary_rows, _report_quality_rows, _fundamentals_rows)

FG_ZONES_CSS = [(0, 25, "#cf222e"), (25, 45, "#bc4c00"), (45, 55, "#9a6700"),
                (55, 75, "#4d8a2a"), (75, 100, "#1a7f37")]


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
