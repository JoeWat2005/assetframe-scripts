"""AssetFrame report generator - Snapshot (free) + Pro pair, website-ready.

Usage:
  python -m scripts.pipeline.render.mvp_report <payload.json>            generate everything
  python -m scripts.pipeline.render.mvp_report <out_dir> --stamp-visual  set visual_inspection_passed

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
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    LONDON = ZoneInfo("Europe/London")
except Exception:
    LONDON = None

sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp

from mvp_report_const import (BRAND, TAGLINE, LOGO, LOGO_ASPECT, FREE_CHART_NOTE, PIVOT_CHART_NOTE,
    LADDER_LEGEND, _items_to_html, _section_body, _pct_from, _ladder_dp, _glossary_rows,
    ladder_geometry, _report_quality_rows, _fundamentals_rows)  # noqa: F401
from mvp_report_qa import (BANNED, NEGATED_ONLY, RR_OK, RR_BAD, QUALITY_LABELS, CLAIM_STATUSES,
    THESIS_BLOCKED, SECTION_ORDER, CAPS_ALLOW, PREDICTION_TYPES, _num_in_levels, run_qa)  # noqa: F401
from mvp_report_pdf import (ACCENT, ACCENT_FILL, STATUS_COLORS, LADDER_COLORS, FG_ZONES_PDF, wrap_text,
    brand_band, title_block, chips, card_grid, boxed, info_box, section_heading, disclaimer,
    timeline_strip, price_ladder, chart_note, kv_card, key_levels_strip, fg_gauge, new_pdf, build_free,
    build_pro, _fundamentals_pdf)  # noqa: F401
from mvp_report_html import (FG_ZONES_CSS, _CSS, ladder_svg, _logo_b64, _rsi_svg, _timeline_html,
    _gauge_svg, _html_head, _cards_html, build_free_html, _info_box_html, _fg_svg, _kl_html,
    _sentiment_html, _fundamentals_html, build_pro_html)  # noqa: F401

# bullets must never carry a literal leading dash - strip at structural starts only,
# and only when a letter/currency follows (so "-0.2%" negative numbers survive)
_DASH = r"[-–—•·]\s*(?=[A-Za-z(£$€])"
RX_DASH_LI = re.compile(r"(<(?:li|p|div)[^>]*>\s*(?:<b[^>]*>)?)\s*" + _DASH)
RX_DASH_BR = re.compile(r"(<br\s*/?>\s*(?:<b[^>]*>)?)\s*" + _DASH)


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
