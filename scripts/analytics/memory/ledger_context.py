"""Ledger context — turn the append-only outcome ledger into a research INPUT.

Produces data/ledger_context/<NAME>_ledger_context.json, handed to the AI BEFORE
it writes the research brief (and consumed by confidence.ledger_confidence). It
lets the analyst reason like: "similar upside breakouts on this instrument have
recently underperformed -> keep the thesis, cut conviction."

HARD RULE - NO LOOK-AHEAD: only rows whose prediction window closed strictly
before the report's generation time (`--as-of`, default now) are aggregated.
Scoring already happens after window close, but we filter on window_end_utc to be
provably free of look-ahead even if a row is mis-stamped.

Degrades gracefully: an empty or young ledger yields a valid "no history yet"
context (neutral), so the pipeline runs on day one. Reads the optional taxonomy
columns (asset_class, pred_type, direction, ...) added by score_report.py; older
rows that lack them still count toward the instrument/overall hit rate.

Usage:
  python scripts/ledger_context.py <NAME> [--ticker T] [--asset-class equity]
         [--ledger ledger/outcome_ledger.csv] [--as-of "YYYY-MM-DD HH:MM"]
         [--recent-k 8] [--out data/ledger_context/<NAME>_ledger_context.json]
         [--print]
"""
import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("ledger/outcome_ledger.csv")
RECENT_K = 8


# parse_dt / _ticker_of / _rate / load_rows live in the shared _ledger_io (deduped with
# research_memory); re-exported under their original names so the rest of this module is unchanged.
from _ledger_io import parse_dt, ticker_of as _ticker_of, rate as _rate, load_rows  # noqa: E402,F401


def _agg(rows):
    h = sum(r["_hits"] for r in rows)
    m = sum(r["_misses"] for r in rows)
    return len(rows), _rate(h, m)


def _type_breakdown(rows):
    """{pred_type: hit_rate}, {pred_type: n} over rows that carry a pred_type."""
    by = {}
    for r in rows:
        pt = (r.get("pred_type") or "").strip()
        if not pt:
            continue
        b = by.setdefault(pt, [0, 0, 0])  # reports, hits, misses
        b[0] += 1; b[1] += r["_hits"]; b[2] += r["_misses"]
    rates = {pt: _rate(b[1], b[2]) for pt, b in by.items()}
    counts = {pt: b[0] for pt, b in by.items()}
    return rates, counts


def _streak(rows, recent_k=RECENT_K):
    """Most recent run of wins/losses by per-report hit-rate (>50 win, <50 loss)."""
    outcomes = []
    for r in rows:
        hr = _rate(r["_hits"], r["_misses"])
        if hr is not None:
            outcomes.append("W" if hr > 50 else ("L" if hr < 50 else "T"))
    direction, length = None, 0
    if outcomes:
        direction = outcomes[-1]
        for o in reversed(outcomes):
            if o == direction:
                length += 1
            else:
                break
    return {"direction": direction, "length": length,
            "recent_results": [_rate(r["_hits"], r["_misses"]) for r in rows[-recent_k:]]}


def build_context(name, rows, ticker=None, asset_class=None, recent_k=RECENT_K):
    ticker = (ticker or name).upper()
    # Match the instrument by its EXACT ticker only. The production caller passes the short
    # ticker as `name` too, so a substring "name in instrument" fallback would leak unrelated
    # instruments' rows into this one's hit rate (e.g. 'es' is inside 'British Pound / Japanese
    # Yen'), biasing the published per-instrument confidence. The ticker match already covers
    # every legitimate row for this instrument.
    inst_rows = [r for r in rows if r["_ticker"] == ticker]
    cls_rows = [r for r in rows if asset_class
                and (r.get("asset_class") or "").strip() == asset_class]

    overall_n, overall_hr = _agg(rows)
    inst_n, inst_hr = _agg(inst_rows)
    cls_n, cls_hr = _agg(cls_rows)

    # prediction-type rates scoped to the instrument (counts let confidence shrink),
    # plus a global view for the AI's situational awareness.
    type_rates, type_counts = _type_breakdown(inst_rows)
    type_scope = "instrument"
    if not type_rates and cls_rows:
        type_rates, type_counts = _type_breakdown(cls_rows); type_scope = "asset_class"
    global_type_rates, global_type_counts = _type_breakdown(rows)

    # recent drift on the instrument
    drift = None
    if inst_n >= recent_k + 2:
        rec_n, rec_hr = _agg(inst_rows[-recent_k:])
        if rec_hr is not None and inst_hr is not None:
            drift = {"last_k": recent_k, "recent_hit_rate": rec_hr,
                     "delta_vs_instrument": round(rec_hr - inst_hr, 1)}

    similar = [{"report_id": r.get("report_id"), "window_end_utc": r.get("window_end_utc"),
                "pred_type": r.get("pred_type") or None, "direction": r.get("direction") or None,
                "hit_rate_pct": _rate(r["_hits"], r["_misses"]),
                "setup_outcome": r.get("setup_outcome") or None}
               for r in inst_rows[-recent_k:]]

    success, failure, notes = _patterns_and_notes(name, ticker, inst_n, inst_hr,
                                                  global_type_rates, global_type_counts, drift)

    return {
        "instrument": name,
        "ticker": ticker,
        "asset_class": asset_class,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC",
        "ledger_rows_considered": len(rows),
        "historical_prediction_count": inst_n,
        "instrument_hit_rate": inst_hr,
        "asset_class_hit_rate": cls_hr,
        "asset_class_count": cls_n,
        "overall_hit_rate": overall_hr,
        "overall_count": overall_n,
        "prediction_type_hit_rates": type_rates,
        "prediction_type_counts": type_counts,
        "prediction_type_scope": type_scope,
        "global_prediction_type_hit_rates": global_type_rates,
        "global_prediction_type_counts": global_type_counts,
        "recent_streak": _streak(inst_rows, recent_k),
        "recent_drift": drift,
        "similar_setup_history": similar,
        "known_success_patterns": success,
        "known_failure_patterns": failure,
        "notes_for_ai": notes,
    }


def _patterns_and_notes(name, ticker, inst_n, inst_hr, gtype_rates, gtype_counts, drift):
    success, failure, notes = [], [], []
    if inst_n == 0:
        notes.append(f"No scored history yet for {name} ({ticker}). Confidence rests on the "
                     f"market and catalyst components; the ledger component stays neutral.")
        return success, failure, notes
    notes.append(f"{name}: {inst_n} scored report(s), {inst_hr}% hit rate to date.")
    for pt, rate in sorted(gtype_rates.items(), key=lambda kv: (kv[1] is None, kv[1])):
        n = gtype_counts.get(pt, 0)
        if rate is None or n < 4:
            continue
        if rate >= 65:
            success.append(f"{pt} calls have worked well ({rate}%, n={n}).")
        elif rate < 45:
            failure.append(f"{pt} calls have underperformed ({rate}%, n={n}) - keep the thesis "
                           f"but cut conviction.")
    if drift and drift["delta_vs_instrument"] <= -10:
        notes.append(f"Recent accuracy on {name} is sliding: {drift['recent_hit_rate']}% over the "
                     f"last {drift['last_k']} vs {inst_hr}% overall - be cautious.")
    return success, failure, notes


def parse_args(argv):
    opts = {"ticker": None, "asset_class": None, "ledger": DEFAULT_LEDGER,
            "as_of": None, "recent_k": RECENT_K, "out": None, "print": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ticker":
            i += 1; opts["ticker"] = argv[i]
        elif a == "--asset-class":
            i += 1; opts["asset_class"] = argv[i]
        elif a == "--ledger":
            i += 1; opts["ledger"] = Path(argv[i])
        elif a == "--as-of":
            i += 1; opts["as_of"] = argv[i]
        elif a == "--recent-k":
            i += 1; opts["recent_k"] = int(argv[i])
        elif a == "--out":
            i += 1; opts["out"] = Path(argv[i])
        elif a == "--print":
            opts["print"] = True
        else:
            print(f"ERROR: unknown argument {a}"); sys.exit(2)
        i += 1
    return opts


def main():
    if len(sys.argv) < 2:
        print("usage: python scripts/ledger_context.py <NAME> [--ticker T] "
              "[--asset-class equity] [--as-of 'YYYY-MM-DD HH:MM'] [--out path] [--print]")
        sys.exit(2)
    name = sys.argv[1]
    opts = parse_args(sys.argv[2:])
    if opts["as_of"]:
        as_of = parse_dt(opts["as_of"])
        if as_of is None:                     # malformed -> exit cleanly (else load_rows: wend >= None TypeError)
            print(f"ERROR: bad --as-of '{opts['as_of']}' (want 'YYYY-MM-DD HH:MM')")
            sys.exit(2)
    else:
        as_of = datetime.now(timezone.utc)
    rows = load_rows(opts["ledger"], as_of)
    ctx = build_context(name, rows, ticker=opts["ticker"], asset_class=opts["asset_class"],
                        recent_k=opts["recent_k"])
    out = opts["out"] or Path(f"data/ledger_context/{name}_ledger_context.json")
    if opts["print"]:
        print(json.dumps(ctx, indent=1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(ctx, indent=1) + "\n", encoding="utf-8")
    if not opts["print"]:
        print(f"wrote {out} ({ctx['historical_prediction_count']} prior {name} report(s), "
              f"{ctx['ledger_rows_considered']} ledger rows pre-{as_of:%Y-%m-%d %H:%M} UTC)")


if __name__ == "__main__":
    main()
