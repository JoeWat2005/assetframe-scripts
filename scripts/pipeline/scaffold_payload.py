"""scaffold_payload.py - compile the AssetFrame report payload from machine data
plus a small AI-authored research brief.

The analyst writes ONE artifact, data/briefs/<NAME>_research_brief.json (prose +
intent + sourced claims, NEVER prices). This script binds that intent to valid
report mechanics:

  * canonical levels are built from the engine analysis (pivots / ATR bands /
    swings / session OHLC) via a fixed id+class catalog;
  * setups, ladder, ledger_levels and the predictions file are all derived FROM
    those levels - the entry/invalidation/T1/T2 are level VALUES picked by
    reference, never free-typed, so they cannot drift;
  * R:R is computed at the zone-edge trigger and formatted to mvp_report's RR_OK;
  * canonical.last_price is read straight from the hourly CSV's last close, so
    the price triple-equality holds by construction;
  * confidence is computed by confidence.compute_confidence (the analyst explains
    it, never sets it) and written identically into the payload and predictions.

It rejects a brief whose claims aren't sourced or whose intent references prices
that aren't in the level set. The narrative (theses, scenarios, risks) comes from
the brief; the numbers and structure come from here.

Usage:
  python scripts/scaffold_payload.py <NAME> \
      [--analysis data/analysis/<NAME>_analysis.json] \
      [--brief data/briefs/<NAME>_research_brief.json] \
      [--research data/research/<NAME>_research_pack.json] \
      [--social data/social/<NAME>_social_pack.json] \
      [--ledger-context data/ledger_context/<NAME>_ledger_context.json] \
      [--calib ledger/calibration_map.json] \
      [--session-profile us_equity_rth] [--out data/payloads/<NAME>_af_payload.json] \
      [--predictions data/predictions/<NAME>_predictions.json] [--check]

--check validates the brief + emitted payload and prints the would-be confidence
without writing (still writes nothing). Exit 2 on a brief/validation error.
"""
import csv, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import taxonomy
import confidence as conf_engine
from sessions import get_session, get_window, get_cadence_window, CADENCE_WINDOWS

try:
    from zoneinfo import ZoneInfo
    _LONDON = ZoneInfo("Europe/London")
except Exception:                       # pragma: no cover - fallback for old stdlib
    _LONDON = None

VALID_QUALITY = {"High quality", "Acceptable", "Low quality", "Management only", "No-trade"}
DISCLAIMER_FREE = ("General market research only. Not personal financial advice. "
                   "Markets are uncertain. Verify data before acting.")
DISCLAIMER_PRO = ("General market research only. Not personal financial advice. Markets are "
                  "uncertain; prices gap across sessions and weekends; losses can exceed "
                  "expectations. No outcome is guaranteed. Verify data independently before "
                  "acting; consider an FCA-authorised adviser. This system never places trades.")


class BriefError(SystemExit):
    pass


def die(msg):
    print(f"ERROR: {msg}")
    raise BriefError(2)


# --- helpers ----------------------------------------------------------------

def read_last_bar(csv_path):
    """Return (last_close, last_ts_utc_str) from a candle CSV (col0=ts, col4=close)."""
    last = None
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for r in csv.reader(f):
            if len(r) >= 5 and r[0][:2].isdigit():
                last = r
    if not last:
        die(f"no data rows in {csv_path}")
    return round(float(last[4]), _dp(float(last[4]))), last[0][:16]


def _dp(v):
    """Sensible decimal places for a price. FX majors (~1-2, e.g. 1.3406) need 4dp or
    adjacent pivots/bands collapse onto a single value (3 levels, no setups); JPY
    crosses / indices / metals / futures (>=10) read fine at 2dp; sub-1 FX/crypto at 5dp."""
    av = abs(v)
    if av >= 10:
        return 2
    if av >= 1:
        return 4
    return 5


def _to_london_dt(utc_str):
    try:
        dt = datetime.strptime(utc_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.astimezone(_LONDON) if _LONDON else dt + timedelta(hours=1)


def to_london(utc_str):
    """'YYYY-MM-DD HH:MM' UTC -> 'Mon 15 Jun 2026 14:30 UK' (portable, no %-d)."""
    loc = _to_london_dt(utc_str)
    if loc is None:
        return utc_str
    return f"{loc:%a} {loc.day} {loc:%b %Y %H:%M} UK"


def to_display(utc_str):
    """'YYYY-MM-DD HH:MM' UTC -> 'Mon 15 Jun 2026 14:30 UTC (15:30 BST)' -
    UTC primary (standard) with the London local time + abbrev alongside."""
    try:
        u = datetime.strptime(utc_str[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc_str
    loc = _to_london_dt(utc_str)
    base = f"{u:%a} {u.day} {u:%b %Y %H:%M} UTC"
    if loc is None:
        return base
    if _LONDON:
        ld = f"{loc:%H:%M %Z}"  # zoneinfo gives the correct BST/GMT abbrev
    else:
        # no tz database: approximate UK clock (matches to_london's fallback) and
        # label by season so winter never gets mislabelled as BST
        abbr = "BST" if 3 <= u.month <= 10 else "GMT"
        ld = f"{loc:%H:%M} {abbr}"
    return f"{base} ({ld})"


def _ld_short(utc_str):
    loc = _to_london_dt(utc_str)
    return f"{loc:%a %H:%M} UK" if loc else utc_str


def load_json(path, required=False, what=""):
    p = Path(path)
    if not p.exists():
        if required:
            die(f"missing {what or path}")
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {path}: {e}")


# --- level catalog ----------------------------------------------------------

def build_levels(analysis, last_price):
    """Deterministic id+class catalog from the engine analysis. Returns the
    levels list (sorted high->low) and a name->level dict for setup wiring."""
    piv = analysis.get("pivots_classic") or {}
    bands = analysis.get("atr_day_bands") or {}
    h = analysis.get("hourly") or {}
    sh = [s["p"] for s in (h.get("swing_highs") or []) if isinstance(s.get("p"), (int, float))]
    sl = [s["p"] for s in (h.get("swing_lows") or []) if isinstance(s.get("p"), (int, float))]

    cand = []  # (id, value, base_cls, label)

    def add(_id, val, cls, label):
        if isinstance(val, (int, float)):
            cand.append({"id": _id, "value": round(float(val), _dp(float(val))),
                         "cls": cls, "label": label})

    add("r2", piv.get("R2"), "resistance", "R2 resistance pivot - beyond a normal session")
    add("tail_hi", bands.get("outer_hi"), "tail", "Upper tail - outer ATR band")
    if sh:
        add("swing_hi", max(sh), "resistance", "Swing-high cluster")
    add("r1", piv.get("R1"), "resistance", "R1 resistance pivot")
    add("inner_hi", bands.get("inner_hi"), "resistance", "Inner band high")
    add("pp", piv.get("PP"), "target", "PP pivot / balance level")
    add("anchor", last_price, "support", "Last close - anchor / manual reference")
    add("s1", piv.get("S1"), "entry", "S1 support pivot")
    add("inner_lo", bands.get("inner_lo"), "entry", "Inner band low")
    if sl:
        add("swing_lo", min(sl), "entry", "Swing-low cluster")
    add("s2", piv.get("S2"), "invalidation", "S2 support pivot")
    add("tail_lo", bands.get("outer_lo"), "tail", "Lower tail - outer ATR band")

    # de-dupe by value (keep the first/most-specific id), then sort high->low. Round at the RENDER
    # precision (_dp), not a fixed 4dp: a sub-1 FX/crypto pair renders at 5dp, so two levels that
    # differ only in the 5th decimal display distinctly and must NOT collapse to one (which would
    # silently drop a level + a setup).
    seen, levels = set(), []
    for lv in cand:
        key = round(lv["value"], _dp(lv["value"]))
        if key in seen:
            continue
        seen.add(key)
        levels.append(lv)
    levels.sort(key=lambda l: -l["value"])
    by_id = {l["id"]: l for l in levels}
    return levels, by_id


def _fmt_rr(ref, inval, t1, t2):
    risk = abs(ref - inval)
    if risk <= 0:
        return "No valid R:R - excluded", None, None
    def mult(t):
        return abs(t - ref) / risk if t is not None else None
    m1, m2 = mult(t1), mult(t2)
    def part(m):
        if m is None:
            return "n/a"                       # no such target (e.g. a long setup with no R1) - not "<1.0x"
        return f"{m:.1f}x" if m >= 1.0 else "below 1.0x"
    return f"T1 {part(m1)}; T2 {part(m2)}", m1, m2


def build_setups(by_id, levels):
    """Long + short conditional setups, every price a canonical level value. R:R
    is computed at the zone-edge trigger (reclaim above entry_hi long / below
    entry_lo short), reproducing the house convention."""
    setups = []
    # LONG: entry = support cluster just below last; inval below; T1 balance, T2 first resistance
    floor = [l for l in ("swing_lo", "inner_lo", "s1") if l in by_id]
    if len(floor) >= 2 and "s2" in by_id and "pp" in by_id:
        elo = min(by_id[i]["value"] for i in floor)
        ehi = max(by_id[i]["value"] for i in floor)
        t1 = by_id["pp"]["value"]
        t2 = by_id["r1"]["value"] if "r1" in by_id else None
        inval = by_id["s2"]["value"]
        rr, _, _ = _fmt_rr(ehi, inval, t1, t2)
        setups.append({"name": "Long-biased (washout into the floor cluster)", "direction": "long",
                       "entry_lo": elo, "entry_hi": ehi, "invalidation": inval,
                       "t1": t1, "t2": t2, "rr": rr})
        for i in floor:
            by_id[i]["cls"] = "entry"
        by_id["s2"]["cls"] = "invalidation"
    # SHORT: entry = pivot/inner-band roof just above last; inval above; T1 floor, T2 lower
    roof = [l for l in ("pp", "inner_hi") if l in by_id]
    if len(roof) >= 2 and "r1" in by_id and "s1" in by_id and "s2" in by_id:
        elo = min(by_id[i]["value"] for i in roof)
        ehi = max(by_id[i]["value"] for i in roof)
        inval = by_id["r1"]["value"]
        t1 = by_id["s1"]["value"]
        t2 = by_id["s2"]["value"]
        rr, _, _ = _fmt_rr(elo, inval, t1, t2)
        setups.append({"name": "Short-biased (failed bounce at the pivot)", "direction": "short",
                       "entry_lo": elo, "entry_hi": ehi, "invalidation": inval,
                       "t1": t1, "t2": t2, "rr": rr})
    return setups


def _apply_setup_override(primary, by_id, override):
    """Analyst-selected setup: the brief's preferred_setup may name CANONICAL level ids for the
    entry zone / invalidation / targets -- {'entry_ids': [...], 'invalidation_id': ..., 't1_id': ...,
    't2_id': ...}. Returns a NEW setup using those canonical VALUES when at least one entry id and the
    invalidation id are real canonical levels (no fabrication -- every price stays canonical, and the
    QA gate re-checks it); otherwise `primary` unchanged. Direction is never changed here. This is
    how the AI gets more control over the prediction WITHOUT breaking the no-fabrication guarantee or
    touching the deterministic, calibrated confidence."""
    if not primary or not isinstance(override, dict):
        return primary
    ent = [i for i in (override.get("entry_ids") or []) if i in by_id]
    inv = override.get("invalidation_id")
    if not ent or inv not in by_id:
        return primary
    s = dict(primary)
    vals = [by_id[i]["value"] for i in ent]
    s["entry_lo"], s["entry_hi"] = min(vals), max(vals)
    s["invalidation"] = by_id[inv]["value"]
    for key, idk in (("t1", "t1_id"), ("t2", "t2_id")):
        if override.get(idk) in by_id:
            s[key] = by_id[override[idk]]["value"]
    trigger = s["entry_hi"] if s.get("direction") == "long" else s["entry_lo"]
    s["rr"], _, _ = _fmt_rr(trigger, s["invalidation"], s.get("t1"), s.get("t2"))
    s["name"] = (primary.get("name") or "Setup").split(" (")[0] + " (analyst-selected levels)"
    s["analyst_selected"] = True
    return s


def build_ladder(levels, setups):
    """All level ids except the bare anchor (kept out so it renders as LAST),
    ensuring every setup inval/t1/t2 id is present."""
    needed = set()
    val_to_id = {round(l["value"], 4): l["id"] for l in levels}
    for s in setups:
        for k in ("invalidation", "t1", "t2"):
            v = s.get(k)
            if v is not None:
                needed.add(val_to_id.get(round(v, 4)))
    ids = [l["id"] for l in levels if l["id"] != "anchor"]
    for nid in needed:
        if nid and nid not in ids:
            ids.append(nid)
    return ids[:12]


def build_predictions_spec(by_id, brief, direction):
    """P1..P6 mapped onto canonical levels; returns (predictions, ledger_levels).

    The DIRECTIONAL predictions (P1 settle-vs-PP, P3 R1-touch) are emitted only for a genuine
    bull/bear view. A neutral/mixed brief gets ONLY the symmetric range/floor/ceiling predictions
    (P2/P4/P5) — registering a directional bet the analyst never made would both misframe the
    report and corrupt the track record / calibration. (Previously `bull = direction=="bullish"`
    silently made neutral AND mixed bearish.)"""
    d = (direction or "").strip().lower()
    bull = d == "bullish"
    directional = d in ("bullish", "bearish")    # neutral/mixed -> no P1/P3
    preds, lv = [], []

    def v(_id):
        return by_id[_id]["value"] if _id in by_id else None

    pp, tlo, thi = v("pp"), v("tail_lo"), v("tail_hi")
    r1, r2, anchor = v("r1"), v("r2"), v("anchor")
    floor = v("swing_lo") or v("inner_lo") or v("s1")

    if pp is not None and directional:
        preds.append({"id": "P1", "type": "close_above", "level": pp, "expect": bool(bull),
                      "text": f"Session settles {'above' if bull else 'below'} PP {pp}"})
        lv.append(pp)
    if tlo is not None and thi is not None:
        preds.append({"id": "P2", "type": "range_inside", "lo": tlo, "hi": thi, "expect": True,
                      "text": f"Stays inside the outer bands {tlo} - {thi}"})
        lv += [tlo, thi]
    if r1 is not None and directional:
        preds.append({"id": "P3", "type": "touches", "level": r1, "expect": bool(bull),
                      "text": f"R1 {r1} is {'' if bull else 'not '}touched"})
        lv.append(r1)
    if floor is not None:
        preds.append({"id": "P4", "type": "no_close_below", "level": floor, "expect": True,
                      "text": f"No hourly close below the floor {floor}"})
        lv.append(floor)
    if r1 is not None and r2 is not None:
        preds.append({"id": "P5", "type": "no_close_above_after_touch", "touch": r1, "level": r2,
                      "expect": True, "text": f"First touch of R1 {r1} does not close an hour "
                      f"above R2 {r2} (NT if untouched)"})
        lv.append(r2)
    manual = (brief or {}).get("manual_prediction")
    if manual and anchor is not None:
        preds.append({"id": "P6", "type": "manual", "note": manual})
        lv.append(anchor)
    # distinct ledger levels, all guaranteed to be canonical values
    seen, ledger_levels = set(), []
    for x in lv:
        k = round(x, 4)
        if k not in seen:
            seen.add(k); ledger_levels.append(x)
    return preds, ledger_levels


# --- payload assembly -------------------------------------------------------

def assemble(name, analysis, brief, session, last_price, last_ts, levels, by_id,
             setups, ladder, ledger_levels, conf, asset_class, regime, pred_type,
             as_of_dt=None, cadence=None):
    # The report identity (slug / report_id / out_dir / R2 key / scoring scope) is pinned to the
    # asset NAME the scheduler passed (run_daily sends the asset ticker), NOT the AI/operator brief's
    # `ticker` field — a divergent brief ticker would de-scope the asset from scoring and could leak
    # an unsafe symbol like "GC=F". Kept strictly ASCII-alphanumeric so it is URL/object-key safe and
    # the report_id parser (year = leading digits, ticker = last "-" segment) can never be broken.
    ticker = "".join(c for c in (name or "").upper() if c.isascii() and c.isalnum()) or "ASSET"
    instrument = brief.get("instrument", name)
    report_date = (session.get("window_start_utc") or analysis.get("fetched_utc") or "")[:10]
    # report_id is the ledger/edition primary key (ON CONFLICT (report_id)) and the scorer's
    # dedup key. A live daily run keeps the stable one-per-UTC-day id AF-YYYYMMDD-TICKER. A
    # BACKDATED run embeds the window-start time (AF-YYYYMMDDHHMM-TICKER) so several reports
    # on the same UTC date (different as-of moments) land DISTINCT ledger rows instead of the
    # second being dropped as an already-scored duplicate — that's how you grow the track
    # record quickly when testing. Ticker stays the rsplit('-',1) suffix, so every downstream
    # parser (year = first 4 digits, ticker = last segment) is unaffected.
    win_s, win_e = session["window_start_utc"], session["window_end_utc"]
    # One ledger row per cadence PERIOD: the stamp is period-based (weekly AF-YYYYWww, monthly
    # AF-YYYYMM); daily keeps the stable per-day id (and the per-minute backdated id for seeding).
    rid_stamp = _period_stamp(cadence, win_s, as_of_dt)
    report_id = f"AF-{rid_stamp}-{ticker}"
    scored_cadence = (cadence or session.get("scored_cadence") or "daily")
    # Window-freshness guard (defends the late-run window-switch case): if the analysis was
    # fetched well before this payload is assembled (execution backlog between the intraday
    # fetch and scaffold), the levels may be stale for the chosen window. Surface a warning the
    # QA/render can act on. Skipped for backdated runs (as_of trims data to the past moment).
    window_freshness_warning = None
    if as_of_dt is None:
        fetched = analysis.get("fetched_utc") or (analysis.get("freshness") or {}).get("last_bar_utc")
        if fetched:
            try:
                ft = datetime.strptime(str(fetched)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                age_h = (datetime.now(timezone.utc) - ft).total_seconds() / 3600
                if age_h > 6:
                    window_freshness_warning = (f"analysis fetched {age_h:.1f}h before this window "
                                                f"was built - possible execution backlog / stale levels")
                    print(f"WARNING: {window_freshness_warning}", file=sys.stderr)
            except (ValueError, TypeError):
                pass
    dq = conf_engine.compute_dq(analysis, brief.get("claims"),
                                brief.get("options_context_included", False))

    canonical = {
        "last_price": {
            "value": last_price, "symbol": ticker,
            "source": brief.get("price_source", "Project intraday engine"),
            "timestamp_utc": last_ts, "exchange_tz": analysis.get("timezone", ""),
            "bar_interval": "60m", "bar_complete": True,
            "price_type": brief.get("price_type", "session close"),
            "adjustment": "none", "contract_month": brief.get("contract_month", "n/a"),
        },
        "levels": levels, "setups": setups, "ladder": ladder, "ledger_levels": ledger_levels,
    }

    meta = {
        "instrument": instrument, "ticker": ticker,
        "asset_class": brief.get("asset_class_label", asset_class),
        "asset_class_key": asset_class,
        "venue": brief.get("venue", session.get("market_session_type", "")),
        "exchange_timezone": analysis.get("timezone", ""), "report_timezone": "UTC",
        "report_date": report_date,
        "prediction_window_start_utc": win_s, "prediction_window_end_utc": win_e,
        "prediction_window_start_report_tz": to_display(win_s),
        "prediction_window_end_report_tz": to_display(win_e),
        "market_session_type": session["market_session_type"],
        "market_open_utc": session.get("market_open_utc", ""),
        "market_close_utc": session.get("market_close_utc", ""),
        "next_maintenance_break": session.get("next_maintenance_break", ""),
        "next_major_event": brief.get("next_major_event", "None scheduled in-window."),
        "latest_bar_timestamp_utc": last_ts,
        "latest_bar_timestamp_report_tz": to_display(last_ts),
        "latest_bar_complete": True,
        "data_provider": (analysis.get("provider") or {}).get("hourly", "yahoo"),
        "data_provider_daily": (analysis.get("provider") or {}).get("daily"),
        "data_license_mode": (analysis.get("provider") or {}).get("license_mode", "personal"),
        "data_license_degraded": bool((analysis.get("provider") or {}).get("license_degraded")),
        "data_provider_note": (analysis.get("provider") or {}).get("note"),
        "window_freshness_warning": window_freshness_warning,
        "cross_check_provider": brief.get("cross_check", "single-source (no independent cross-check this run)"),
        "price_type": brief.get("price_type", "session close"),
        "contract_month": brief.get("contract_month", "n/a"),
        "adjustment_type": "none",
        "last_price": brief.get("last_price_note") or f"{last_price} (last completed bar {to_display(last_ts)})",
        "status": brief["status"], "risk_rating": brief["risk"],
        "research_view": brief.get("research_view", ""),
        "primary_bias": brief.get("primary_bias", brief.get("directional_view", "")),
        "long_scenario_quality": _quality(brief.get("long_scenario_quality")),
        "short_scenario_quality": _quality(brief.get("short_scenario_quality")),
        "data_quality_score": dq,
        "market_regime": regime, "direction_view": brief.get("directional_view", ""),
        "prediction_type": pred_type, "horizon": brief.get("horizon", "next_session"),
        # cadence/intervals carried through for the editions table + track-record grouping (Workstream D)
        "report_id": report_id, "scored_cadence": scored_cadence,
        "chart_intervals": analysis.get("chart_intervals") or [],
        "forecast_window": session.get("forecast_window") or "",
        "confidence_band": taxonomy.confidence_band(conf["published"]),
        "lookback_used": _lookback(analysis),
        "asset_specific_stats_included": brief.get("asset_specific_stats_included", []),
        "options_context_included": brief.get("options_context_included", False),
        "options_context_reason": brief.get("options_context_reason",
                                            "No options feed - expected-range frame is ATR-based."),
        "source_gaps": brief.get("source_gaps") or (brief.get("news_context") or {}).get("source_gaps", []),
        "high_impact_claims": _claims(brief.get("claims", [])),
        "disclaimer": "General market research only. Not personal financial advice.",
    }

    free = build_free(name, analysis, brief, session, last_price, dq, by_id)
    pro = build_pro(name, analysis, brief, session, last_price, dq, levels, setups,
                    canonical["ledger_levels"], conf, by_id, cadence=cadence)

    return {
        "report_id": report_id,
        "title": brief.get("title", f"{instrument} ({ticker})"),
        "subtitle": brief.get("subtitle", f"{meta['venue']} - {meta['prediction_window_start_report_tz']}"
                    f" -> {meta['prediction_window_end_report_tz']}"),
        "status": brief["status"], "risk": brief["risk"], "confidence": conf["published"],
        # SANDBOX: route the rendered edition under reports/sim/ so a backtest never writes
        # into a live published edition folder. Env UNSET -> the live reports/ path.
        "out_dir": (f"reports/sim/{report_date}/{ticker}"
                    if os.environ.get("ASSETFRAME_SANDBOX") == "1"
                    else f"reports/{report_date}/{ticker}"),
        "confidence_breakdown": conf,
        "canonical": canonical, "meta": meta, "free": free, "pro": pro,
    }


def _quality(v):
    if not v:
        return "Acceptable"
    return v if v in VALID_QUALITY else "Acceptable"


def _lookback(a):
    w = a.get("windows") or {}
    return {"daily": f"{w.get('daily_display','')} shown / {w.get('daily_fetched','')} fetched",
            "intraday": f"{w.get('hourly_display','')} shown / {w.get('hourly_fetched','')} fetched"}


def _claims(claims):
    out = []
    valid = {"confirmed", "multiple-source", "single-source", "unverified", "stale", "unavailable"}
    for c in claims:
        st = (c.get("status") or "").lower()
        if st not in valid:
            die(f"claim status '{c.get('status')}' invalid (use {sorted(valid)})")
        if c.get("used_in_thesis") and st in {"unverified", "stale", "unavailable"}:
            die(f"claim '{c.get('claim','?')[:40]}' is {st} but used_in_thesis - cannot drive thesis")
        out.append({"claim": c.get("claim", ""), "status": st,
                    "source": c.get("source", "-"), "used_in_thesis": bool(c.get("used_in_thesis"))})
    return out


def build_free(name, analysis, brief, session, last_price, dq, by_id):
    nb = brief.get("narrative", {})
    # free bullets/scenarios from the brief, sanitised of pro-only vocabulary
    bullets = nb.get("free_bullets") or [{"label": "Core thesis", "text": brief.get("research_view", "")}]
    bl = "<ul>" + "".join(f"<li><b>{b['label']}:</b> {b['text']}</li>" for b in bullets) + "</ul>"
    scen = nb.get("free_scenarios") or []
    sc = "<table><tr><th>Scenario</th><th>Trigger</th><th>Broad expected move</th><th>What to watch</th></tr>"
    for s in scen:
        sc += (f"<tr><td>{s.get('scenario','')}</td><td>{s.get('trigger','')}</td>"
               f"<td>{s.get('move','')}</td><td>{s.get('watch','')}</td></tr>")
    sc += "</table>"
    chart_csv = (analysis.get("files") or {}).get("hourly_csv", f"data/candles/{name}_hourly.csv")
    free = {
        "cards": [
            ["Last price", brief.get("free_last_price", f"{last_price} (last completed bar)")],
            ["Broad expected range", brief.get("free_expected_range", "see Pro for the level map")],
            ["Risk window", f"{_ld_short(session['window_start_utc'])} -> {_ld_short(session['window_end_utc'])}"],
            ["Data quality", f"{dq}/10 - audit in Pro"],
        ],
        "chart": {"csv": chart_csv, "label": brief.get("free_chart_label", f"{name} hourly"),
                  "height": 250, "display_days": 10, "smas": [20, 50],
                  "support": [by_id["swing_lo"]["value"]] if "swing_lo" in by_id else [],
                  "resistance": [by_id["pp"]["value"]] if "pp" in by_id else []},
        "bullets_html": bl, "scenarios_html": sc,
        "timeline_events": brief.get("timeline_events", [
            {"t": _ld_short(session["window_start_utc"]), "label": "Window opens"},
            {"t": _ld_short(session["window_end_utc"]), "label": "Window closes (scored)"}]),
        "teaser": ("Pro adds the conditional level map with activation rules, the price ladder with "
                   "distances, risk math, the scored outcome record, and the full source audit."),
        "disclaimer": DISCLAIMER_FREE,
    }
    _assert_free_split(free)
    return free


PRO_ONLY = ("r:r", "per contract", "entry zone", "invalidation", "t1 ", "t2 ", "ladder",
            "glossary", "source audit", "outcome ledger", "hedging", "risk math")


def _assert_free_split(free):
    blob = json.dumps({k: v for k, v in free.items() if k not in ("teaser", "disclaimer")},
                      ensure_ascii=False).lower()
    for w in PRO_ONLY:
        if w in blob:
            die(f"free tier contains pro-only vocabulary {w!r} - rephrase the brief's free_* fields")
    fch = free["chart"]
    if len(fch.get("support", [])) + len(fch.get("resistance", [])) > 3 or "pivots" in fch or "bands" in fch:
        die("free chart exceeds 3 levels or carries pivots/bands")


def build_pro(name, analysis, brief, session, last_price, dq, levels, setups, ledger_levels, conf,
              by_id, cadence=None):
    nb = brief.get("narrative", {})
    files = analysis.get("files") or {}
    hourly = files.get("hourly_csv", f"data/candles/{name}_hourly.csv")
    daily = files.get("daily_csv", f"data/candles/{name}_daily.csv")

    # The daily + hourly pair is always present: the hourly chart is the QA price anchor (its last
    # close must equal canonical.last_price) and the daily is the regime base. Higher cadences PREPEND
    # coarser context charts (weekly -> +weekly; monthly -> +monthly+weekly) read from the analysis
    # interval blocks, falling back silently when an interval wasn't fetched.
    charts = [
        {"csv": daily, "label": "Daily regime - 1 year shown", "height": 220,
         "display_days": 366, "smas": [50, 200]},
        {"csv": hourly, "label": "Intraday - hourly, 10 days", "height": 290, "display_days": 10,
         "smas": [20, 50], "rsi": True, "rsi_tag": "hourly",
         "pivots": {k.upper(): by_id[k]["value"] for k in ("pp", "r1", "r2", "s1") if k in by_id}},
    ]
    intervals = analysis.get("intervals") or {}

    def _ictx(iv, label, display_days, smas):
        blk = intervals.get(iv) or {}
        csv_path = blk.get("csv")
        return ({"csv": csv_path, "label": label, "height": 220,
                 "display_days": display_days, "smas": smas} if csv_path and blk.get("bars") else None)

    ctx = []
    if (cadence or "").lower() == "weekly":
        ctx = [_ictx("1week", "Weekly regime - 2 years shown", 730, [20, 50])]
    elif (cadence or "").lower() == "monthly":
        ctx = [_ictx("1month", "Monthly regime - 5 years shown", 1825, [12]),
               _ictx("1week", "Weekly - 1 year shown", 365, [20])]
    charts = [c for c in ctx if c] + charts

    sections = []
    if nb.get("market_summary"):
        sections.append({"heading": "Market summary", "items": nb["market_summary"],
                         "html": nb.get("cross_asset_html", "")})
    if nb.get("long_short_view"):
        sections.append({"heading": "Long / Short Research View", "html": nb["long_short_view"]})
    if brief.get("scenario_matrix"):
        sections.append({"heading": "Scenario matrix", "html": _scenario_matrix_html(brief["scenario_matrix"])})
    if brief.get("catalysts"):
        sections.append({"heading": "Event-risk timeline", "html": _events_html(brief["catalysts"])})
    sections.append({"heading": "Technicals and key levels", "html": _technicals_html(analysis, levels, last_price, nb)})
    sections.append({"heading": "Conditional setups", "html": _setups_html(setups)})
    sections.append({"heading": "Asset-Specific Statistics", "html": nb.get("stats_html", "<p>See cards above.</p>")})
    if brief.get("risks"):
        sections.append({"heading": "What can go wrong?", "html": "<ul>"
                        + "".join(f"<li>{r}</li>" for r in brief["risks"]) + "</ul>"})
    sections.append({"heading": "Trade-quality scorecard", "html": _scorecard_html(conf)})
    sections.append({"heading": "Outcome ledger", "html": _ledger_html(brief, ledger_levels)})
    sections.append({"heading": "Source audit", "html": _source_audit_html(brief, analysis, dq)})
    sections.append({"heading": "Asset-session rules", "html": "<ul>"
                    + "".join(f"<li>{p}</li>" for p in session.get("session_prose", [])) + "</ul>"})

    return {
        "exec": brief.get("exec") or [["Last price", f"{last_price} (last completed bar)"],
                                      ["Research view", brief.get("research_view", "")],
                                      ["Risk window", f"{_ld_short(session['window_start_utc'])} -> "
                                       f"{_ld_short(session['window_end_utc'])}"],
                                      ["Data mode / quality", f"{dq}/10"]],
        "overview": brief.get("exec_summary") or brief.get("research_view", ""),
        "verdict": brief.get("verdict", {}),
        "catalyst_status": brief.get("catalyst_status", ""),
        "charts": charts, "sections": sections,
        "source_confidence": brief.get("source_confidence", [["Overall", f"Data quality {dq}/10"]]),
        "disclaimer": DISCLAIMER_PRO,
    }


def _scenario_matrix_html(rows):
    h = ("<table><tr><th>Case</th><th>Trigger</th><th>Expected move/range</th>"
         "<th>Invalidation</th><th>Confidence</th><th>What to watch</th></tr>")
    for r in rows:
        h += (f"<tr><td>{r.get('case','')}</td><td>{r.get('trigger','')}</td><td>{r.get('move','')}</td>"
              f"<td>{r.get('invalidation','')}</td><td>{r.get('confidence','')}</td><td>{r.get('watch','')}</td></tr>")
    return h + "</table>"


def _events_html(cats):
    h = ("<table><tr><th>Event</th><th>Time</th><th>Relevance</th><th>In window?</th><th>Gap risk?</th></tr>")
    for c in cats:
        h += (f"<tr><td>{c.get('label','')}</td><td>{c.get('when','')}</td><td>{c.get('relevance','')}</td>"
              f"<td>{'Yes' if c.get('in_window') else 'No'}</td><td>{'Yes' if c.get('gap_risk') else '-'}</td></tr>")
    return h + "</table>"


def _technicals_html(analysis, levels, last_price, nb):
    pre = nb.get("technicals_note", "")
    h = (f"<p>{pre}</p>" if pre else "") + ("<table><tr><th>Level</th><th>Price</th>"
         "<th>Distance</th><th>Classification</th></tr>")
    for l in levels:
        dist = l["value"] - last_price
        h += (f"<tr><td>{l['label']}</td><td>{l['value']}</td><td>{dist:+.2f}</td>"
              f"<td>{l['cls'].title()}</td></tr>")
    return h + "</table>"


def _setups_html(setups):
    h = ("<table><tr><th>Setup</th><th>Dir</th><th>Entry zone</th><th>Invalidation</th>"
         "<th>T1 / T2</th><th>R:R</th></tr>")
    for s in setups:
        h += (f"<tr><td>{s['name']}</td><td>{s['direction'].title()}</td>"
              f"<td>{s['entry_lo']} - {s['entry_hi']}</td><td>{s['invalidation']}</td>"
              f"<td>{s.get('t1')} / {s.get('t2')}</td><td>{s['rr']}</td></tr>")
    return h + "</table>"


def _scorecard_html(conf):
    h = "<table><tr><th>Component</th><th>Weight</th><th>Score</th></tr>"
    for c in conf["components"]:
        sc = f"{c['score']:.2f}" if isinstance(c["score"], float) else c["score"]
        wt = f"{c['weight']}%" if c["weight"] else "adj"
        h += f"<tr><td>{c['name']}</td><td>{wt}</td><td>{sc}</td></tr>"
    h += "</table><ul>"
    h += f"<li><b>Published confidence: {conf['published']}/100</b> ({conf['band']}); raw {conf['raw']}.</li>"
    if conf["caps_applied"]:
        h += f"<li>Caps applied: {', '.join(conf['caps_applied'])}.</li>"
    h += (f"<li>Calibration: {'applied from the ledger' if conf['calibrated'] else 'identity (too few scored rows yet)'}"
          f"; engine v{conf['conf_version']}. The analyst explains this score; the engine computes it.</li></ul>")
    return h


def _ledger_html(brief, ledger_levels):
    return ("<ul><li><b>Ledger:</b> this report's window is registered; predictions are scored "
            "against the tape after the window closes (Hit / Miss / No trigger / Manual review).</li></ul>"
            "<p>Levels under test: " + ", ".join(str(x) for x in ledger_levels) + ".</p>")


def _source_audit_html(brief, analysis, dq):
    prov = analysis.get("provider") or {}
    hp = prov.get("hourly") or "engine"
    dp = prov.get("daily")
    src = hp if (not dp or dp == hp) else f"{hp} (hourly) + {dp} (daily)"   # G6: don't hide a split source
    gaps = brief.get("source_gaps") or (brief.get("news_context") or {}).get("source_gaps", [])
    h = (f"<ul><li><b>Primary data provider:</b> project intraday engine ({src}).</li>"
         f"<li><b>Cross-check:</b> {brief.get('cross_check','single-source this run')}.</li>")
    if prov.get("license_mode") == "commercial":
        if prov.get("license_degraded"):
            h += ("<li><b>&#9888; Data licensing:</b> this edition fell back to a "
                  "non-commercially-licensed source — not for redistribution.</li>")
        else:
            h += "<li><b>Data licensing:</b> commercially-licensed feed.</li>"
    if gaps:
        h += "<li><b>Gaps:</b> " + "; ".join(gaps) + ".</li>"
    h += f"<li><b>Overall data quality: {dq}/10.</b></li></ul>"
    return h


# --- main -------------------------------------------------------------------

def parse_args(argv):
    o = {"analysis": None, "brief": None, "research": None, "social": None,
         "ledger_context": None, "calib": None, "session_profile": None, "forecast_window": None,
         "out": None, "predictions": None, "as_of": None, "window_end": None,
         "timeframes": None, "cadence": None, "check": False}
    i = 0
    keys = {"--analysis": "analysis", "--brief": "brief", "--research": "research",
            "--social": "social", "--ledger-context": "ledger_context", "--calib": "calib",
            "--session-profile": "session_profile", "--forecast-window": "forecast_window",
            "--out": "out", "--predictions": "predictions",
            "--as-of": "as_of", "--window-end": "window_end", "--timeframes": "timeframes",
            "--cadence": "cadence"}
    while i < len(argv):
        a = argv[i]
        if a == "--check":
            o["check"] = True
        elif a in keys:
            i += 1
            if i >= len(argv):
                die(f"{a} needs a value")
            o[keys[a]] = argv[i]
        else:
            die(f"unknown argument {a}")
        i += 1
    return o


def _horizon_for(forecast_window):
    """Taxonomy horizon (the scoring/calibration bucket) for a forecast window."""
    from sessions import LONG_WINDOWS
    return "multi_session" if (forecast_window or "").strip().lower() in LONG_WINDOWS else "next_session"


_HORIZON_TAG = {"intraday": "H", "next_session": "", "multi_session": "MS"}


def _track_report_id(base_report_id, horizon):
    """Distinct ledger id for a NON-primary timeframe track. A horizon tag is inserted into the
    date stamp so the ticker (last '-' segment) and year (leading digits) still parse:
    AF-20260623-GOLD -> AF-20260623MS-GOLD. The primary track keeps the canonical id."""
    tag = _HORIZON_TAG.get(horizon, (horizon or "")[:2].upper())
    if not tag:
        return base_report_id
    head, _, tick = base_report_id.rpartition("-")
    return f"{head}{tag}-{tick}" if head else base_report_id + tag


def _period_stamp(cadence, window_start_utc, as_of_dt):
    """The report_id date stamp, one per cadence PERIOD (so the ledger row is unique per period):
      daily   -> AF-YYYYMMDD (live) / AF-YYYYMMDDHHMM (backdated, to seed fast)
      weekly  -> AF-YYYYWww   (ISO week; one row per week, backdated or live)
      monthly -> AF-YYYYMM    (one row per month)
    The leading 4 digits stay the year and the ticker stays the rsplit('-',1) suffix, so every
    downstream parser is unaffected."""
    cad = (cadence or "daily").strip().lower()
    if cad == "weekly":
        d = datetime.strptime(window_start_utc[:10], "%Y-%m-%d").date()
        iso = d.isocalendar()
        return f"{iso[0]}W{iso[1]:02d}"
    if cad == "monthly":
        return window_start_utc[:7].replace("-", "")          # YYYYMM
    if as_of_dt is not None:                                   # daily backdated
        return as_of_dt.strftime("%Y%m%d%H%M")
    return window_start_utc[:10].replace("-", "")             # daily live -> YYYYMMDD


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/scaffold_payload.py <NAME> [--brief ...] [--session-profile ...] [--check]")
        sys.exit(2)
    name = sys.argv[1]
    o = parse_args(sys.argv[2:])

    # Under sandbox, read the brief/research/social from the sim/ subtrees so a backtest NEVER picks
    # up the LIVE news-laden brief/packs (look-ahead). run_daily authors a fresh technical-only brief
    # into data/briefs/sim; the sim research/social dirs stay empty (no web search), so those load {}.
    _sb = "/sim" if os.environ.get("ASSETFRAME_SANDBOX") == "1" else ""
    analysis = load_json(o["analysis"] or f"data/analysis/{name}_analysis.json", True, "analysis")
    brief = load_json(o["brief"] or f"data/briefs{_sb}/{name}_research_brief.json", True, "research brief")
    research = load_json(o["research"] or f"data/research{_sb}/{name}_research_pack.json")
    social = load_json(o["social"] or f"data/social{_sb}/{name}_social_pack.json")
    ledger_ctx = load_json(o["ledger_context"] or f"data/ledger_context/{name}_ledger_context.json")
    calib = load_json(o["calib"] or "ledger/calibration_map.json")

    profile = o["session_profile"] or brief.get("session_profile")
    if not profile:
        die("no session profile (pass --session-profile or set session_profile in the brief)")
    for req in ("status", "risk"):
        if not brief.get(req):
            die(f"brief missing required field '{req}'")

    # As-of / explicit-window overrides (retroactive generation). --as-of backdates the
    # session clock so get_session reports the correct state for that past moment; for a
    # single-day window on a 24/5 FX week (where get_session would otherwise target the
    # weekly close) --window-end caps the prediction window so it can be scored once it
    # passes. report_date is taken from window_start, so an as-of run dates correctly.
    now_dt = None
    if o["as_of"]:
        s = o["as_of"].strip()
        try:
            now_dt = datetime.strptime(s, "%Y-%m-%d %H:%M" if len(s) > 10 else "%Y-%m-%d") \
                .replace(tzinfo=timezone.utc)
        except ValueError:
            die(f"--as-of must be 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' UTC (got {o['as_of']!r})")
    # get_window is horizon-aware: for the standard next-session forecast windows it returns
    # the exact get_session() result (the live universe is unchanged); for 'next_week' /
    # 'next_5_sessions' it extends the window end to a multi-session horizon.
    # multi-timeframe: one report carries a prediction TRACK per configured timeframe. The first is
    # the primary/published window; extras are scored on their own windows under a horizon-tagged id.
    timeframes = [t.strip() for t in (o["timeframes"] or o["forecast_window"] or "next_session").split(",")
                  if t.strip()]
    _seen = set()
    timeframes = [t for t in timeframes if not (t in _seen or _seen.add(t))]
    primary_fw = timeframes[0]
    # Cadence (daily/weekly/monthly) drives the CANONICAL per-period window: one prediction set
    # scored at the period close. Falls back to the forecast-window machinery when no cadence is
    # passed (back-compat for manual/legacy runs). chart_intervals (analysis) stay a distinct concept.
    cadence = (o.get("cadence") or "").strip().lower() or None
    if cadence in CADENCE_WINDOWS:
        session = get_cadence_window(profile, cadence, now=now_dt)
    else:
        session = get_window(profile, now=now_dt, forecast_window=primary_fw)
    if now_dt is not None:
        session["window_start_utc"] = now_dt.strftime("%Y-%m-%d %H:%M")
    if o["window_end"]:
        we = o["window_end"].strip()
        try:
            datetime.strptime(we[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            die(f"--window-end must be 'YYYY-MM-DD HH:MM' UTC (got {o['window_end']!r})")
        if we[:16] <= session["window_start_utc"]:
            die("--window-end must be after the window start / as-of moment")
        session["window_end_utc"] = we[:16]
        session["window_label"] = "explicit window (as-of / retroactive run)"
    # a window per configured timeframe (track 0 = the primary/published window above). The horizon
    # follows the WINDOW (forecast_window) -- NOT the brief's label -- so the primary's ledger row and
    # its applied calibration land in the same bucket the extra tracks use.
    # horizon (the scoring/calibration bucket) follows the cadence when one is set: daily ->
    # next_session, weekly/monthly -> multi_session; else it follows the forecast window.
    if cadence in ("weekly", "monthly"):
        primary_horizon = "multi_session"
    elif cadence == "daily":
        primary_horizon = "next_session"
    else:
        primary_horizon = _horizon_for(primary_fw)
    track_specs = [{"forecast_window": primary_fw, "horizon": primary_horizon,
                    "window_start_utc": session["window_start_utc"],
                    "window_end_utc": session["window_end_utc"]}]
    for tf in timeframes[1:]:
        w = get_window(profile, now=now_dt, forecast_window=tf)
        ws = now_dt.strftime("%Y-%m-%d %H:%M") if now_dt is not None else w["window_start_utc"]
        track_specs.append({"forecast_window": tf, "horizon": _horizon_for(tf),
                            "window_start_utc": ws, "window_end_utc": w["window_end_utc"]})
    hourly_csv = (analysis.get("files") or {}).get("hourly_csv", f"data/candles/{name}_hourly.csv")
    try:
        last_price, last_ts = read_last_bar(hourly_csv)
    except BriefError:
        # Hourly series empty (e.g. a feed degraded to daily-only) — fall back to the daily candles
        # instead of aborting the whole asset. read_last_bar still dies if BOTH are empty.
        daily_csv = (analysis.get("files") or {}).get("daily_csv", f"data/candles/{name}_daily.csv")
        last_price, last_ts = read_last_bar(daily_csv)

    levels, by_id = build_levels(analysis, last_price)
    direction = taxonomy.validate_direction(brief.get("directional_view", "neutral"))
    setups = build_setups(by_id, levels)
    # analyst-selected setup levels (applied BEFORE the ladder/QA so the chosen canonical levels are
    # included and re-validated). Falls back to the deterministic setup if the override is incomplete.
    ovr = brief.get("preferred_setup") or {}
    if setups and isinstance(ovr, dict) and (ovr.get("entry_ids") or ovr.get("invalidation_id")):
        base = next((s for s in setups if s["direction"] == ovr.get("side")), setups[0])
        new = _apply_setup_override(base, by_id, ovr)
        if new is not base:
            setups[setups.index(base)] = new
    ladder = build_ladder(levels, setups)
    preds, ledger_levels = build_predictions_spec(by_id, brief, direction)

    asset_class = taxonomy.asset_class_key(profile, brief.get("ticker", name),
                                           brief.get("asset_class_key"))
    regime = taxonomy.normalize_market_regime(brief.get("market_regime"), analysis)
    pred_type = taxonomy.validate_prediction_type((brief.get("primary_prediction") or {}).get("type", "range_hold"))

    # primary setup = the one matching the brief's preferred side, else the first
    side = (brief.get("preferred_setup") or {}).get("side")
    primary = next((s for s in setups if s["direction"] == side), setups[0] if setups else None)
    conf = conf_engine.compute_confidence(analysis, primary, brief, research, social,
                                          ledger_ctx, calib,
                                          options_included=brief.get("options_context_included", False),
                                          levels=[l["value"] for l in levels], horizon=primary_horizon)

    payload = assemble(name, analysis, brief, session, last_price, last_ts, levels, by_id,
                       setups, ladder, ledger_levels, conf, asset_class, regime, pred_type,
                       as_of_dt=now_dt, cadence=cadence)
    payload["timeframes"] = track_specs   # multi-timeframe outlook (one report, N horizon tracks)
    if analysis.get("fundamentals"):      # canonical equity fundamentals (Pro render; never scored)
        payload["fundamentals"] = analysis["fundamentals"]

    predictions = {
        "report_id": payload["report_id"], "instrument": payload["meta"]["instrument"],
        "symbol": payload["meta"]["ticker"], "roll_utc": brief.get("roll_utc", 0),
        "view": payload["meta"]["research_view"], "confidence": conf["published"],
        "conf_version": conf["conf_version"], "conf_raw": conf["capped"],
        "taxonomy": taxonomy.build_taxonomy(pred_type, direction,
                                            primary_horizon, asset_class, regime),
        "window_start_utc": session["window_start_utc"], "window_end_utc": session["window_end_utc"],
        "hourly_csv": hourly_csv, "predictions": preds,
        "setup": {k: primary.get(k) for k in ("direction", "entry_lo", "entry_hi", "invalidation", "t1")}
        if primary else None,
    }

    out = Path(o["out"] or f"data/payloads/{name}_af_payload.json")
    # SANDBOX: default the predictions base dir to data/predictions/sim so a backtest's
    # prediction files (and the per-timeframe tracks written under pred_out.parent) never
    # land in the live data/predictions/ scope the scorer reads. An explicit --predictions
    # always wins; env UNSET -> the live path. Byte-identical when neither is set.
    _pred_default = (f"data/predictions/sim/{name}_predictions.json"
                     if os.environ.get("ASSETFRAME_SANDBOX") == "1"
                     else f"data/predictions/{name}_predictions.json")
    pred_out = Path(o["predictions"] or _pred_default)
    summary = {"name": name, "confidence": conf["published"], "raw": conf["raw"],
               "band": conf["band"], "caps": conf["caps_applied"], "pred_type": pred_type,
               "levels": len(levels), "setups": len(setups), "predictions": len(preds),
               "payload": str(out), "predictions_file": str(pred_out)}
    if o["check"]:
        print(json.dumps({**summary, "mode": "check (nothing written)"}, indent=1))
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    pred_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    pred_out.write_text(json.dumps(predictions, indent=1) + "\n", encoding="utf-8")
    # extra multi-timeframe tracks: a standard single-window predictions file per NON-primary
    # timeframe, with a horizon-tagged report_id so the scorer (dedup on report_id) records a
    # SEPARATE ledger row per horizon — and calibration is horizon-bucketed. The published edition
    # stays the canonical report_id; these files are scoring-only.
    extra = []
    seen_ids = {predictions["report_id"]}
    for spec in track_specs[1:]:
        hz = spec["horizon"]
        rid = _track_report_id(predictions["report_id"], hz)
        if rid in seen_ids:
            # two configured timeframes map to the same horizon bucket -> identical tagged report_id
            # AND filename; keep the first, skip the rest (else they collide and the scorer drops one).
            print(f"  warning: timeframe '{spec['forecast_window']}' shares horizon '{hz}' with an "
                  f"earlier track ({rid}) — skipping the colliding track", file=sys.stderr)
            continue
        seen_ids.add(rid)
        tp = dict(predictions)
        tp["report_id"] = rid
        tp["window_start_utc"], tp["window_end_utc"] = spec["window_start_utc"], spec["window_end_utc"]
        tp["forecast_window"] = spec["forecast_window"]
        tp["taxonomy"] = taxonomy.build_taxonomy(pred_type, direction, hz, asset_class, regime)
        tf_path = pred_out.parent / f"{name}_{hz}_predictions.json"
        tf_path.write_text(json.dumps(tp, indent=1) + "\n", encoding="utf-8")
        extra.append(tf_path.name)
    if extra:
        summary["extra_timeframe_files"] = extra
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
