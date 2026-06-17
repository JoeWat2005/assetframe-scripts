"""Static site generator for AssetFrame - turns reports/ + ledger/ into a
deployable website. Stdlib only (no deps); output is a self-contained site/
folder you can drag straight onto Cloudflare Pages or open locally.

Usage:
  python scripts/build_site.py [--out site] [--reports reports] [--include-dev] [--include-pro]

Builds:
  site/index.html         storefront + report library (free Snapshots)
  site/track-record.html  the public ledger: open calls + scored results + calibration
  site/pricing.html       Free vs Pro + the Subscribe button (Lemon Squeezy checkout)
  site/redeem.html        members area: paste licence key -> access Pro reports
  site/r/<date>/<instr>/  PUBLIC free artifacts only (free.html, free.pdf, preview.png)
  site/functions/pro/     the gating Function (copied from web/functions/)
  site/assets/logo.png    brand mark

Gating: free Snapshots are public; Pro files are NOT copied into the public build.
They are uploaded to private R2 by scripts/publish.py and served by the Cloudflare
Pages Function in web/functions/pro/, which validates a Lemon Squeezy licence key.
Pass --include-pro to also copy Pro files locally for preview (NEVER deploy that build).
Reads site.config.json for the checkout URL and site URL. See LAUNCH.md to go live.
"""
import argparse
import csv
import html
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = "AssetFrame"
TAGLINE = "Next-session market intelligence, scored after the fact."
ACCENT = "#0b2545"
DISCLAIMER = ("General market research and decision support - not regulated financial advice and "
             "not a personal recommendation. Markets are uncertain and you can lose money. "
             "No outcome is guaranteed. Verify figures independently before acting; consider an "
             "FCA-authorised adviser. AssetFrame never places trades.")

STATUS_COLOR = {"buy": "#1a7f37", "sell": "#cf222e", "wait": "#9a6700",
                "stand aside": "#57606a", "neutral": "#0969da", "hold": "#0969da"}
RISK_COLOR = {"low": "#1a7f37", "medium": "#9a6700", "high": "#bc4c00", "very high": "#cf222e"}

E = lambda s: html.escape(str(s), quote=True)


def load_config():
    cfg = {"site_url": "", "checkout_url": "", "pro_price_label": ""}
    p = ROOT / "site.config.json"
    if p.exists():
        try:
            cfg.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8")).items()
                        if not k.startswith("_")})
        except Exception:
            pass
    return cfg


CFG = load_config()
CHECKOUT_URL = (CFG.get("checkout_url") or "").strip()
PRO_PRICE = (CFG.get("pro_price_label") or "").strip()
# Card "Unlock Pro" buttons explain the offer via the pricing page; the pricing
# page itself sends buyers to the live checkout once checkout_url is configured.
BUY_LABEL = f"Subscribe{' ' + PRO_PRICE if PRO_PRICE else ''}"
PUBLIC_FILES = ["free.html", "free.pdf", "preview.png"]
PRO_FILES = ["pro.html", "pro.pdf"]


def status_color(s):
    return STATUS_COLOR.get(str(s).strip().lower(), "#57606a")


def risk_color(s):
    return RISK_COLOR.get(str(s).strip().lower(), "#57606a")


CSS = f"""
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  margin:0;color:#24292f;background:#f6f8fa;line-height:1.5}}
a{{color:{ACCENT};text-decoration:none}} a:hover{{text-decoration:underline}}
.wrap{{max-width:1080px;margin:0 auto;padding:0 20px}}
header.nav{{background:#fff;border-bottom:1px solid #d8dee4;position:sticky;top:0;z-index:10}}
header.nav .wrap{{display:flex;align-items:center;justify-content:space-between;height:58px}}
header.nav img{{height:24px}}
header.nav a.navlink{{margin-left:22px;color:#57606a;font-weight:600;font-size:14px}}
header.nav a.navlink:hover{{color:{ACCENT};text-decoration:none}}
header.nav a.navlink.active{{color:{ACCENT}}}
.hero{{background:{ACCENT};color:#fff;padding:54px 0 46px}}
.hero h1{{margin:0 0 8px;font-size:34px;letter-spacing:-.4px}}
.hero p.tag{{margin:0;font-size:17px;color:#c9d6e8}}
.hero p.sub{{margin:16px 0 0;max-width:680px;color:#aebfd6;font-size:15px}}
.hero .cta{{display:inline-block;margin-top:22px;background:#fff;color:{ACCENT};font-weight:700;
  padding:10px 20px;border-radius:8px;font-size:15px}}
h2.section{{font-size:22px;margin:38px 0 6px;color:{ACCENT}}}
p.lead{{color:#57606a;margin:0 0 18px;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}}
.card{{background:#fff;border:1px solid #d8dee4;border-radius:12px;padding:16px 18px;display:flex;flex-direction:column}}
.card .top{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}}
.card h3{{margin:0;font-size:18px}}
.card .tkr{{color:#57606a;font-size:13px;font-weight:600}}
.card .cls{{color:#8b949e;font-size:12px;margin-top:2px}}
.badges{{display:flex;gap:6px;flex-wrap:wrap;margin:12px 0 10px}}
.badge{{color:#fff;font-weight:700;font-size:11.5px;padding:3px 10px;border-radius:20px;white-space:nowrap}}
.meta{{font-size:12.5px;color:#57606a;margin:2px 0}}
.bias{{font-size:13px;color:#24292f;margin:8px 0 12px}}
.actions{{display:flex;gap:8px;margin-top:auto;flex-wrap:wrap}}
.btn{{font-weight:700;font-size:13px;padding:8px 14px;border-radius:8px;border:1px solid {ACCENT};
  color:{ACCENT};background:#fff;cursor:pointer}}
.btn:hover{{text-decoration:none;background:#eef2f8}}
.btn.primary{{background:{ACCENT};color:#fff}}
.btn.pro{{border-color:#9a6700;color:#9a6700}}
.btn.pro:hover{{background:#fff7e6}}
.btn.sm{{padding:6px 10px;font-size:12px}}
.statband{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:14px 0 26px}}
.stat{{background:#fff;border:1px solid #d8dee4;border-radius:12px;padding:16px}}
.stat .n{{font-size:30px;font-weight:800;color:{ACCENT};line-height:1}}
.stat .l{{font-size:12.5px;color:#57606a;margin-top:6px}}
table.tr{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #d8dee4;border-radius:12px;overflow:hidden;font-size:13.5px}}
table.tr th{{background:#eef2f8;color:{ACCENT};text-align:left;padding:10px 12px;font-size:12.5px}}
table.tr td{{border-top:1px solid #eaeef2;padding:10px 12px;vertical-align:top}}
.pill{{font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px;background:#eaeef2;color:#57606a}}
.pill.pending{{background:#fff4d6;color:#9a6700}}
.pill.hit{{background:#dafbe1;color:#1a7f37}} .pill.miss{{background:#ffebe9;color:#cf222e}}
.note{{background:#eef2f8;border:1px solid #cdd9ea;border-radius:10px;padding:12px 16px;font-size:13px;color:#33415c;margin:18px 0}}
footer{{border-top:1px solid #d8dee4;margin-top:48px;background:#fff}}
footer .wrap{{padding:22px 20px;font-size:11.5px;color:#8b949e}}
"""


def page(title, body, active=""):
    nav = "".join(
        f'<a class="navlink{" active" if active == key else ""}" href="{href}">{label}</a>'
        for key, href, label in [("reports", "index.html", "Reports"),
                                  ("track", "track-record.html", "Track record"),
                                  ("pricing", "pricing.html", "Pricing"),
                                  ("members", "redeem.html", "Members")])
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)} | {BRAND}</title><style>{CSS}</style></head><body>
<header class="nav"><div class="wrap">
<a href="index.html"><img src="assets/logo.png" alt="{BRAND}"></a>
<nav>{nav}</nav></div></header>
{body}
<footer><div class="wrap"><b>{BRAND}</b> &middot; {E(TAGLINE)}<br>{E(DISCLAIMER)}</div></footer>
</body></html>"""


# ---------------------------------------------------------------- data loaders
def load_catalog(reports_dir, include_dev):
    editions = []
    for meta_path in sorted(reports_dir.glob("*/*/metadata.json")):
        rel = meta_path.relative_to(reports_dir)
        if not include_dev and rel.parts[0].startswith("_"):
            continue
        try:
            m = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        d = meta_path.parent
        editions.append({
            "dir": d, "date": rel.parts[0], "slug": rel.parts[1],
            "instrument": m.get("instrument", rel.parts[1]),
            "ticker": m.get("ticker", ""),
            "asset_class": m.get("asset_class", ""),
            "status": m.get("status", ""),
            "risk": m.get("risk_rating", ""),
            "bias": m.get("primary_bias") or m.get("research_view", ""),
            "last_price": m.get("last_price", ""),
            "dq": m.get("data_quality_score", ""),
            "window_end": m.get("prediction_window_end_report_tz", ""),
            "report_date": m.get("report_date", rel.parts[0]),
            "paths": m.get("paths", {}),
        })
    editions.sort(key=lambda e: (e["date"], e["slug"]), reverse=True)
    return editions


def load_ledger(ledger_csv):
    if not ledger_csv.exists() or ledger_csv.stat().st_size == 0:
        return [], None
    rows = list(csv.DictReader(ledger_csv.open(encoding="utf-8")))
    calib = None
    if len(rows) >= 10:
        buckets = {"<=60": [], "61-75": [], ">75": []}
        for r in rows:
            try:
                c, hr = float(r["confidence"]), float(r["hit_rate_pct"])
            except (ValueError, KeyError):
                continue
            key = "<=60" if c <= 60 else ("61-75" if c <= 75 else ">75")
            buckets[key].append(hr)
        calib = {k: (round(sum(v) / len(v), 1) if v else None, len(v)) for k, v in buckets.items()}
    return rows, calib


def load_open_calls(pred_dir, scored_ids):
    calls = []
    for pf in sorted(pred_dir.glob("*_predictions.json")):
        try:
            p = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if p.get("report_id") in scored_ids:
            continue
        preds = p.get("predictions", [])
        calls.append({
            "report_id": p.get("report_id", pf.stem),
            "instrument": p.get("instrument", ""),
            "symbol": p.get("symbol", ""),
            "view": p.get("view", ""),
            "confidence": p.get("confidence", ""),
            "window_end": p.get("window_end_utc", ""),
            "n": len(preds),
            "n_manual": sum(1 for x in preds if x.get("type") == "manual"),
        })
    calls.sort(key=lambda c: c["window_end"])
    return calls


# ---------------------------------------------------------------- renderers
def render_index(catalog, include_pro=False):
    hero = f"""<section class="hero"><div class="wrap">
<h1>{BRAND}</h1><p class="tag">{E(TAGLINE)}</p>
<p class="sub">Pre-session research on the instruments that matter - a free one-page Snapshot for everyone,
and a full Pro report with conditional setups, a price ladder and a scored outcome ledger. Every call is
published <b>before</b> the outcome and graded against the tape afterwards.</p>
<a class="cta" href="track-record.html">See the track record &rarr;</a></div></section>"""

    if not catalog:
        cards = '<p class="lead">No editions published yet. Run the pipeline, then rebuild the site.</p>'
    else:
        cards = '<div class="grid">'
        for e in catalog:
            base = f'r/{E(e["date"])}/{E(e["slug"])}'
            p = e["paths"]
            free_html = f'{base}/{E(p.get("free_html", "free.html"))}'
            free_pdf = f'{base}/{E(p.get("free_pdf", "free.pdf"))}'
            sb = (f'<span class="badge" style="background:{status_color(e["status"])}">{E(e["status"])}</span>'
                  if e["status"] else "")
            rb = (f'<span class="badge" style="background:{risk_color(e["risk"])}">Risk: {E(e["risk"])}</span>'
                  if e["risk"] else "")
            dq = f'<span class="meta">Data quality {E(e["dq"])}/10</span>' if e["dq"] != "" else ""
            if include_pro:  # local preview build: link straight to the copied Pro file
                pro_btn = f'<a class="btn pro sm" href="{base}/pro.html" target="_blank">Preview Pro</a>'
            else:  # production: Pro is gated; the card explains the offer via pricing
                pro_btn = '<a class="btn pro sm" href="pricing.html">&#128274; Unlock Pro</a>'
            cards += f"""<div class="card">
<div class="top"><div><h3>{E(e["instrument"])}</h3><div class="tkr">{E(e["ticker"])}</div>
<div class="cls">{E(e["asset_class"])}</div></div></div>
<div class="badges">{sb}{rb}</div>
<div class="bias">{E(e["bias"])}</div>
<div class="meta">Edition {E(e["report_date"])} &middot; window to {E(e["window_end"])}</div>
{dq}
<div class="actions">
<a class="btn primary sm" href="{free_html}" target="_blank">Read Snapshot</a>
<a class="btn sm" href="{free_pdf}" target="_blank">PDF</a>
{pro_btn}
</div></div>"""
        cards += "</div>"

    body = f"""{hero}<div class="wrap">
<h2 class="section">Latest editions</h2>
<p class="lead">The free Snapshot opens in your browser. Pro reports - conditional long/short setups, the price
ladder, sentiment, risk math and the outcome ledger - unlock with a subscription.</p>
{cards}</div>"""
    return page("Market intelligence, scored after the fact", body, "reports")


def render_track_record(ledger_rows, calib, open_calls):
    total = len(ledger_rows)
    hits = sum(int(r.get("hits", 0) or 0) for r in ledger_rows)
    misses = sum(int(r.get("misses", 0) or 0) for r in ledger_rows)
    graded = hits + misses
    hit_rate = f"{round(100 * hits / graded, 1)}%" if graded else "--"

    stat = lambda n, l: f'<div class="stat"><div class="n">{n}</div><div class="l">{E(l)}</div></div>'
    band = f"""<div class="statband">
{stat(total, "Reports scored")}
{stat(len(open_calls), "Open calls awaiting scoring")}
{stat(hit_rate, "Hit rate (graded predictions)")}
{stat(f"{graded}", "Predictions graded so far")}</div>"""

    intro = """<div class="note"><b>How this works.</b> Every Pro report registers a handful of falsifiable
predictions - exact levels, an exact window - <b>before</b> the outcome is known. After the window closes the
engine grades each one against the price tape (Hit / Miss / No-trigger) and appends one row here. The ledger is
append-only: nothing is removed, re-tuned or cherry-picked. A calibration block appears once 10 reports are scored.</div>"""

    # open calls table
    if open_calls:
        rows = ""
        for c in open_calls:
            man = f' <span class="pill">+{c["n_manual"]} manual</span>' if c["n_manual"] else ""
            rows += f"""<tr><td><b>{E(c["instrument"])}</b><br><span class="tkr">{E(c["symbol"])}</span></td>
<td>{E(c["view"])}</td><td>{E(c["confidence"])}</td>
<td>{c["n"]} predictions{man}</td>
<td>{E(c["window_end"])} UTC</td><td><span class="pill pending">Pending</span></td></tr>"""
        open_tbl = f"""<table class="tr"><thead><tr><th>Instrument</th><th>Research view</th><th>Confidence</th>
<th>Registered calls</th><th>Scores after</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>"""
    else:
        open_tbl = '<p class="lead">No open calls right now.</p>'

    # scored table
    if ledger_rows:
        rows = ""
        for r in reversed(ledger_rows):
            hr = r.get("hit_rate_pct", "")
            pill = "hit" if (hr and float(hr) >= 50) else "miss"
            rows += f"""<tr><td><b>{E(r.get("instrument", ""))}</b></td><td>{E(r.get("view", ""))}</td>
<td>{E(r.get("confidence", ""))}</td><td>{E(r.get("results", ""))}</td>
<td><span class="pill {pill}">{E(hr)}%</span></td><td>{E(r.get("window_end_utc", ""))}</td></tr>"""
        scored_tbl = f"""<table class="tr"><thead><tr><th>Instrument</th><th>View</th><th>Conf.</th>
<th>Results</th><th>Hit rate</th><th>Window end</th></tr></thead><tbody>{rows}</tbody></table>"""
    else:
        scored_tbl = '<div class="note">No reports scored yet - the first results land once the open calls above close. <b>Ledger starts here.</b></div>'

    # calibration
    calib_html = ""
    if calib:
        rows = "".join(
            f'<tr><td>{E(k)}</td><td>{("%.1f%%" % v[0]) if v[0] is not None else "--"}</td><td>{v[1]}</td></tr>'
            for k, v in calib.items())
        calib_html = f"""<h2 class="section">Calibration</h2>
<p class="lead">Does stated confidence track realised hit rate? It should.</p>
<table class="tr"><thead><tr><th>Stated confidence</th><th>Realised hit rate</th><th>Reports</th></tr></thead>
<tbody>{rows}</tbody></table>"""

    body = f"""<div class="wrap">
<h2 class="section">Track record</h2>
<p class="lead">The scored-after-the-fact promise, made mechanical.</p>
{band}{intro}
<h2 class="section">Open calls</h2>
<p class="lead">Published now, graded when the window closes - so you can watch them resolve.</p>
{open_tbl}
<h2 class="section">Scored results</h2>
{scored_tbl}
{calib_html}</div>"""
    return page("Track record", body, "track")


def render_pricing():
    feat = lambda t: f'<tr><td>{E(t)}</td></tr>'
    free_rows = "".join(feat(t) for t in [
        "One-page Snapshot per edition", "Status, risk and broad expected range",
        "One chart with support/resistance", "Three-bullet thesis and broad scenarios",
        "Risk-window timeline"])
    pro_rows = "".join(feat(t) for t in [
        "Everything in the Snapshot, plus:", "Plain-English 30-second read + verdict",
        "Conditional long & short setups with R:R", "Price ladder with distances and key-level cards",
        "Scenario matrix, event-risk timeline, technicals", "Sentiment, positioning and options context where sourced",
        "Trade-quality scorecard and risk math", "The full source audit and outcome ledger",
        "Glossary - every chart abbreviation explained"])
    price = f"{PRO_PRICE} &middot; " if PRO_PRICE else ""
    if CHECKOUT_URL:
        sub_btn = f'<a class="btn pro" href="{E(CHECKOUT_URL)}">{E(BUY_LABEL)}</a>'
        note = ('<div class="note">After checkout, Lemon Squeezy emails you a licence key. '
                'Paste it on the <a href="redeem.html">Members</a> page to unlock Pro reports '
                'on your device.</div>')
    else:
        sub_btn = '<a class="btn pro" href="#" onclick="return false" style="opacity:.55">Checkout opening soon</a>'
        note = ('<div class="note">Checkout isn\'t switched on yet. Add your Lemon Squeezy buy link to '
                '<code>site.config.json</code> and rebuild - then this button goes live. '
                'See LAUNCH.md.</div>')
    body = f"""<section class="hero"><div class="wrap">
<h1>Pricing</h1><p class="tag">Start free. Upgrade for the full intelligence.</p></div></section>
<div class="wrap"><div class="grid" style="margin-top:26px">
<div class="card"><h3>AssetFrame Snapshot</h3><div class="bias"><b>Free</b></div>
<table class="tr">{free_rows}</table>
<div class="actions"><a class="btn primary" href="index.html">Browse free editions</a></div></div>
<div class="card" style="border-color:#9a6700"><h3>AssetFrame Pro</h3>
<div class="bias"><b>{price}Subscription</b> &middot; cancel anytime</div>
<table class="tr">{pro_rows}</table>
<div class="actions">{sub_btn}<a class="btn sm" href="redeem.html">Already subscribed?</a></div></div>
</div>{note}</div>"""
    return page("Pricing", body, "pricing")


def render_redeem(catalog):
    if catalog:
        items = "".join(
            f'<tr><td><b>{E(e["instrument"])}</b> <span class="tkr">{E(e["ticker"])}</span><br>'
            f'<span class="meta">Edition {E(e["report_date"])}</span></td>'
            f'<td style="text-align:right;white-space:nowrap">'
            f'<a class="btn sm prolink" href="pro/{E(e["date"])}/{E(e["slug"])}/pro.html" '
            f'data-base="pro/{E(e["date"])}/{E(e["slug"])}/pro.html" target="_blank">Read</a> '
            f'<a class="btn sm prolink" href="pro/{E(e["date"])}/{E(e["slug"])}/pro.pdf" '
            f'data-base="pro/{E(e["date"])}/{E(e["slug"])}/pro.pdf" target="_blank">PDF</a></td></tr>'
            for e in catalog)
        table = f'<table class="tr"><thead><tr><th>Pro report</th><th></th></tr></thead><tbody>{items}</tbody></table>'
    else:
        table = '<p class="lead">No Pro editions published yet.</p>'
    body = f"""<section class="hero"><div class="wrap">
<h1>Members</h1><p class="tag">Paste your licence key to access Pro reports.</p></div></section>
<div class="wrap">
<div class="note" id="status">After subscribing, Lemon Squeezy emails you a licence key. Paste it below to
unlock Pro reports on this device (stored only in your browser).</div>
<div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 22px">
<input id="lk" placeholder="Your licence key" style="flex:1;min-width:240px;padding:9px 12px;
border:1px solid #d8dee4;border-radius:8px;font-size:14px">
<button class="btn primary" onclick="afUnlock()">Unlock</button></div>
<h2 class="section">Your Pro reports</h2>
<p class="lead">These open once you've unlocked. Each click is checked against your live subscription.</p>
{table}</div>
<script>
function afSet(k){{document.cookie='af_license='+encodeURIComponent(k)+'; path=/; max-age=2592000; samesite=strict';
 document.querySelectorAll('a.prolink').forEach(function(a){{a.href=a.dataset.base+'?key='+encodeURIComponent(k);}});}}
function afUnlock(){{var k=document.getElementById('lk').value.trim();if(!k)return;
 localStorage.setItem('af_license',k);afSet(k);
 document.getElementById('status').textContent='Unlocked on this device. Open any Pro report below.';}}
window.addEventListener('load',function(){{var p=new URLSearchParams(location.search);
 if(p.get('error')){{document.getElementById('status').textContent='That licence key was not valid or has expired. Check the key from your Lemon Squeezy email, or subscribe on the Pricing page.';}}
 var k=localStorage.getItem('af_license');if(k){{document.getElementById('lk').value=k;afSet(k);}}}});
</script>"""
    return page("Members", body, "members")


# ---------------------------------------------------------------- build
def copy_artifacts(catalog, out, include_pro):
    """Public deploy gets free files only. Pro files stay out of the public build
    (they go to private R2 via publish.py) unless --include-pro for local preview."""
    r = out / "r"
    names = list(PUBLIC_FILES) + (PRO_FILES if include_pro else [])
    for e in catalog:
        dest = r / e["date"] / e["slug"]
        dest.mkdir(parents=True, exist_ok=True)
        for name in names:
            src = e["dir"] / name
            if src.exists():
                shutil.copy(src, dest / name)
    # ship the gating Function inside the deploy folder (Cloudflare Pages convention)
    fsrc = ROOT / "web" / "functions"
    if fsrc.exists():
        shutil.copytree(fsrc, out / "functions")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--include-dev", action="store_true")
    ap.add_argument("--include-pro", action="store_true",
                    help="copy Pro files into the build for LOCAL preview - never deploy this")
    a = ap.parse_args()

    out = (ROOT / a.out) if not Path(a.out).is_absolute() else Path(a.out)
    reports_dir = (ROOT / a.reports) if not Path(a.reports).is_absolute() else Path(a.reports)

    catalog = load_catalog(reports_dir, a.include_dev)
    ledger_rows, calib = load_ledger(ROOT / "ledger" / "outcome_ledger.csv")
    scored_ids = {r.get("report_id") for r in ledger_rows}
    open_calls = load_open_calls(ROOT / "data" / "predictions", scored_ids)

    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    logo = ROOT / "logo" / "logo_trimmed.png"
    if logo.exists():
        shutil.copy(logo, out / "assets" / "logo.png")

    (out / "index.html").write_text(render_index(catalog, a.include_pro), encoding="utf-8")
    (out / "track-record.html").write_text(render_track_record(ledger_rows, calib, open_calls), encoding="utf-8")
    (out / "pricing.html").write_text(render_pricing(), encoding="utf-8")
    (out / "redeem.html").write_text(render_redeem(catalog), encoding="utf-8")
    copy_artifacts(catalog, out, a.include_pro)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "LOCAL PREVIEW (Pro files included - do NOT deploy)" if a.include_pro else "PRODUCTION (free only; Pro gated via R2)"
    checkout = CHECKOUT_URL or "(not set - add to site.config.json)"
    print(f"Built site -> {out}   [{mode}]")
    print(f"  editions:    {len(catalog)}")
    print(f"  open calls:  {len(open_calls)}")
    print(f"  scored rows: {len(ledger_rows)}" + ("  (calibration shown)" if calib else ""))
    print(f"  pages:       index, track-record, pricing, redeem (+ functions/pro gating)")
    print(f"  checkout:    {checkout}")
    print(f"  built at:    {stamp}")
    print(f"Open {out / 'index.html'} to preview, or deploy the {out.name}/ folder to Cloudflare Pages (see LAUNCH.md).")


if __name__ == "__main__":
    main()
