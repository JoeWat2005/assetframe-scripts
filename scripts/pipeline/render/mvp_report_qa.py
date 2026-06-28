"""AssetFrame pre-render QA gate (extracted from mvp_report). Imports the shared leaf only; never
imports mvp_report/_pdf/_html."""
import json, re, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import report_pdf as rp
from mvp_report_const import LOGO, _glossary_rows
try:
    from taxonomy import PREDICTION_TYPES
except Exception:  # taxonomy is stdlib-only; fall back so the generator stays runnable
    PREDICTION_TYPES = ("breakout", "rejection", "continuation",
                        "mean_reversion", "range_hold", "volatility_expansion")

BANNED = [r"sure trade", r"risk[- ]free(?!\s+(rate|yield|asset|benchmark))", r"easy profit",
          r"you should buy", r"you should sell"]  # "risk-free RATE/yield/asset" is legit finance
# phrases allowed ONLY in negated compliance form ("no outcome is guaranteed",
# "not a personal recommendation")
# A negation token anywhere in the short preceding window clears these (so "no guaranteed returns",
# "is not guaranteed", "isn't guaranteed", "nothing is guaranteed" all pass) — same false-positive
# fix-class as the risk-free lookahead; only a bare positive claim ("guaranteed profit") is flagged.
NEGATED_ONLY = {"guaranteed": r"\b(no|not|never|without|nothing|cannot|none)\b|n't\b",
                "personal recommendation": r"\b(not|no|never|nothing)\b|n't\b"}
# `n/a` is a legitimate target form: scaffold_payload._fmt_rr emits it for a missing target (e.g. a
# long setup whose R1 collided away at display precision, leaving t2=None) — the QA gate must accept
# it, else that otherwise-valid build hard-aborts.
RR_OK = re.compile(r"^(T1 (below 1\.0x|\d+(\.\d+)?x|n/a); T2 (below 1\.0x|\d+(\.\d+)?x|\d+(\.\d+)?x .*|n/a)|No valid R:R - (excluded|setup excluded)).*$")
RR_BAD = re.compile(r"(~\s*-\d)|(-\d+(\.\d+)?\s*/\s*-?\d+(\.\d+)?x)|(R:R[^.<]{0,30}[-−]\d)")
QUALITY_LABELS = {"High quality", "Acceptable", "Low quality", "Management only", "No-trade"}
CLAIM_STATUSES = {"confirmed", "multiple-source", "single-source", "unverified", "stale", "unavailable"}
THESIS_BLOCKED = {"unverified", "stale", "unavailable"}  # cannot drive thesis

# canonical Pro section order (relative; unknown headings slot anywhere)
SECTION_ORDER = ["market summary", "long / short research view", "scenario matrix",
                 "event-risk timeline", "technicals", "conditional setups",
                 "options / hedging", "asset-specific statistics",
                 "sentiment", "what can go wrong", "contract", "trade-quality scorecard",
                 "outcome ledger", "source audit", "asset-session rules"]

# acronyms that may legitimately appear in caps; other ALL-CAPS words draw a QA warn
CAPS_ALLOW = {"RSI", "MACD", "SMA", "EMA", "ATR", "VWAP", "ETF", "ETFS", "FOMC", "UTC",
              "BST", "GMT", "USD", "USDT", "GBP", "JPY", "EUR", "BTC", "ETH", "SOL",
              "DXY", "VIX", "VVIX", "GDP", "CPI", "PMI", "BOE", "ECB", "FCA", "CME",
              "NYMEX", "COMEX", "LSE", "IPO", "ATH", "API", "EIA", "OPEC", "CFTC", "COT",
              "PDF", "RTH", "AAPL", "QQQ", "SPY", "NVDA", "MSFT", "WTI", "OKX", "MEXC",
              "WWDC", "EDT", "EST"}


def _num_in_levels(v, level_vals, tol=1e-6):
    return any(abs(float(v) - lv) <= max(tol, abs(lv) * 1e-6) for lv in level_vals)


def run_qa(p):
    """Pre-render QA. Returns (qa_dict, errors, warnings). Errors abort the build."""
    errs, warns = [], []
    c = p["canonical"]
    meta = p["meta"]
    level_vals = [float(l["value"]) for l in c["levels"]]
    last = float(c["last_price"]["value"])

    # --- price triple-equality (header/chart/metadata) from the finest chart CSV that has bars.
    # Defined unconditionally so the qa dict references below can never hit UnboundLocalError.
    ok_price = False
    hourly_cfg = next((ch for ch in p["pro"]["charts"] if "hourly" in ch["csv"].lower()
                       or ch.get("display_days", 99) <= 30), p["pro"]["charts"][-1])
    rows = rp.read_series(Path(hourly_cfg["csv"]))
    if not rows:
        # Degraded/empty intraday CSV (e.g. backdated runs with no hourly history): fall back to
        # the coarsest pro chart that DOES have bars rather than crashing on rows[-1] or hard-failing.
        for ch in p["pro"]["charts"]:
            alt = rp.read_series(Path(ch["csv"]))
            if alt:
                hourly_cfg, rows = ch, alt
                break
    if not rows:
        errs.append("no pro chart CSV has rows - cannot verify the canonical price")
    else:
        csv_last = rows[-1]["c"]
        ok_price = abs(csv_last - last) <= max(0.01, last * 1e-5)
        if not ok_price:
            errs.append(f"canonical last {last} != chart CSV last close {csv_last}")
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
            # Clear the phrase if a negation sits in its SHORT preceding window: up to 60 chars back
            # (a legit "no representation ... is guaranteed" reaches ~40 chars), but NOT past a
            # clause/sentence boundary (.!?;\n) — so a negation in a different clause ("...we do not
            # expect a pullback; a rally is guaranteed") can't mask a real claim. The old fixed 34-char
            # window missed legit negations (false-positive abort); an unbounded sentence would let a
            # distant unrelated negation slip a claim through (compliance miss) — 60 + boundary splits the difference.
            bound = max((blob.rfind(c, 0, m.start()) for c in ".!?;\n"), default=-1) + 1
            ctx = blob[max(bound, m.start() - 60):m.start()]
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
    # --- fabricated price-level scan (WARN only; NEVER blocks the run). Catches a hallucinated price
    # (e.g. "resistance at 4,521") in the authored NARRATIVE that isn't backed by a canonical level.
    # Setups/ladder are already level-bound (errs above); free-text numbers were the gap. Tightly
    # scoped to avoid noise: only price-like tokens (a decimal or thousands-grouped number), only
    # those in the instrument's price BAND, only those NOT near any canonical level — so RSI/ATR
    # values, percentages, years and small integers are excluded by construction.
    try:
        band = [float(v) for v in level_vals] + ([float(last)] if last else [])
        if band:
            blo, bhi, tol = min(band) * 0.95, max(band) * 1.05, 0.0025
            bad = set()
            for t in texts:
                for m in re.finditer(r"(?<![\d.])(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.\d+)", t):
                    if t[m.end():m.end() + 1] == "%":
                        continue                          # a percentage, not a price
                    try:
                        num = float(m.group(0).replace(",", ""))
                    except ValueError:
                        continue
                    if not (blo <= num <= bhi):
                        continue                          # outside the price band -> not a price claim
                    if any(abs(num - lv) <= tol * max(1.0, abs(lv)) for lv in band):
                        continue                          # near a canonical level/last -> fine
                    bad.add(m.group(0))
            for s in sorted(bad)[:6]:
                warns.append(f"narrative cites price-like '{s}' with no matching canonical level "
                             f"(possible fabricated number — verify)")
    except Exception:
        pass
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
