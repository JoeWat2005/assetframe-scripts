"""Research memory — learn "what reasoning works" from the append-only ledger.

PURE derivation (no AI input): like ledger_context.py, but aggregated ACROSS ALL
instruments instead of one, so the system accumulates institutional knowledge —
"breakout in high_volatility regimes: 71%", "bearish continuation underperforms".
Writes ledger/research_memory.json, fed back to ledger_context / the AI and (later)
the Pro track record so it demonstrates LEARNING, not just accuracy.

SAME HARD RULE — NO LOOK-AHEAD: only rows whose prediction window closed strictly
before --as-of (default now) are aggregated. Report-level hits/misses suffice; we
do not re-parse the packed `results` string (per-report counts already carry it).

Degrades gracefully: an empty/young ledger yields a valid "no memory yet" object,
so day-one generation still runs. Reads the additive taxonomy columns
(pred_type / market_regime / asset_class / direction) added by score_report.py;
rows missing a dimension simply don't count toward that dimension's breakdown.

Usage:
  python scripts/research_memory.py [--ledger ledger/outcome_ledger.csv]
         [--as-of "YYYY-MM-DD HH:MM"] [--out ledger/research_memory.json]
         [--min-n 4] [--print]
"""
import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("ledger/outcome_ledger.csv")
DEFAULT_OUT = Path("ledger/research_memory.json")
MIN_N = 4   # same n>=4 guard ledger_context uses before naming a pattern


# parse_dt / _rate / load_rows live in the shared _ledger_io (deduped with ledger_context);
# re-exported under their original names. load_rows also tags _ticker — ignored here (this module
# aggregates ACROSS instruments), harmless extra key.
from _ledger_io import parse_dt, rate as _rate, load_rows  # noqa: E402,F401


def _breakdown(rows, key):
    """{value: {reports, hits, misses, hit_rate_pct}} over rows carrying `key`."""
    by = {}
    for r in rows:
        v = (r.get(key) or "").strip()
        if not v:
            continue
        b = by.setdefault(v, [0, 0, 0])  # reports, hits, misses
        b[0] += 1; b[1] += r["_hits"]; b[2] += r["_misses"]
    return {v: {"reports": b[0], "hits": b[1], "misses": b[2],
                "hit_rate_pct": _rate(b[1], b[2])} for v, b in by.items()}


def _cross_breakdown(rows, key_a, key_b):
    """{'a x b': {...}} over rows carrying BOTH dimensions (the learning cross)."""
    by = {}
    for r in rows:
        a = (r.get(key_a) or "").strip()
        b = (r.get(key_b) or "").strip()
        if not a or not b:
            continue
        cell = by.setdefault(f"{a} x {b}", [0, 0, 0])
        cell[0] += 1; cell[1] += r["_hits"]; cell[2] += r["_misses"]
    return {k: {"reports": c[0], "hits": c[1], "misses": c[2],
                "hit_rate_pct": _rate(c[1], c[2])} for k, c in by.items()}


def _best_worst(breakdowns, min_n):
    """Rank every (label, cell) across the supplied breakdowns by hit rate, with
    the n>=min_n guard, into best_patterns / worst_patterns lists."""
    scored = []
    for dim, table in breakdowns.items():
        for label, cell in table.items():
            hr, n = cell["hit_rate_pct"], cell["reports"]
            if hr is None or n < min_n:
                continue
            scored.append({"dimension": dim, "pattern": label,
                           "hit_rate_pct": hr, "reports": n})
    best = sorted(scored, key=lambda x: (-x["hit_rate_pct"], -x["reports"]))
    worst = sorted(scored, key=lambda x: (x["hit_rate_pct"], -x["reports"]))
    # keep them disjoint and short
    best_top = [p for p in best if p["hit_rate_pct"] >= 60][:5]
    worst_top = [p for p in worst if p["hit_rate_pct"] < 50][:5]
    return best_top, worst_top


def _notes(total_n, overall_hr, best, worst, min_n):
    notes = []
    if total_n == 0:
        notes.append("No scored history yet across any instrument — research memory is empty; "
                     "generation proceeds on market + catalyst signal with a neutral ledger prior.")
        return notes
    notes.append(f"{total_n} scored report(s) across all instruments, {overall_hr}% overall.")
    if total_n < min_n:
        notes.append(f"Too few scored reports (<{min_n}) to name reliable reasoning patterns yet.")
    for p in best:
        notes.append(f"Works well: {p['pattern']} ({p['dimension']}) at {p['hit_rate_pct']}% "
                     f"(n={p['reports']}).")
    for p in worst:
        notes.append(f"Underperforms: {p['pattern']} ({p['dimension']}) at {p['hit_rate_pct']}% "
                     f"(n={p['reports']}) — keep the thesis but cut conviction.")
    return notes


def build_memory(rows, as_of, min_n=MIN_N):
    tot_h = sum(r["_hits"] for r in rows)
    tot_m = sum(r["_misses"] for r in rows)
    overall_hr = _rate(tot_h, tot_m)

    by_type = _breakdown(rows, "pred_type")
    by_regime = _breakdown(rows, "market_regime")
    by_class = _breakdown(rows, "asset_class")
    by_direction = _breakdown(rows, "direction")
    by_type_regime = _cross_breakdown(rows, "pred_type", "market_regime")

    best, worst = _best_worst(
        {"prediction_type": by_type, "market_regime": by_regime,
         "asset_class": by_class, "direction": by_direction,
         "prediction_type x market_regime": by_type_regime},
        min_n)

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC",
        "as_of_utc": as_of.strftime("%Y-%m-%d %H:%M") + " UTC",
        "no_look_ahead": "only rows with window_end_utc strictly before as_of are counted",
        "min_n": min_n,
        "total_scored_reports": len(rows),
        "overall_hit_rate_pct": overall_hr,
        "by_prediction_type": by_type,
        "by_market_regime": by_regime,
        "by_asset_class": by_class,
        "by_direction": by_direction,
        "by_prediction_type_x_regime": by_type_regime,
        "best_patterns": best,
        "worst_patterns": worst,
        "notes": _notes(len(rows), overall_hr, best, worst, min_n),
    }


def parse_args(argv):
    opts = {"ledger": DEFAULT_LEDGER, "as_of": None, "out": None,
            "min_n": MIN_N, "print": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ledger":
            i += 1; opts["ledger"] = Path(argv[i])
        elif a == "--as-of":
            i += 1; opts["as_of"] = argv[i]
        elif a == "--out":
            i += 1; opts["out"] = Path(argv[i])
        elif a == "--min-n":
            i += 1; opts["min_n"] = int(argv[i])
        elif a == "--print":
            opts["print"] = True
        else:
            print(f"ERROR: unknown argument {a}"); sys.exit(2)
        i += 1
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    as_of = parse_dt(opts["as_of"]) if opts["as_of"] else datetime.now(timezone.utc)
    if as_of is None:
        print("ERROR: --as-of must be 'YYYY-MM-DD HH:MM'"); sys.exit(2)
    rows = load_rows(opts["ledger"], as_of)
    mem = build_memory(rows, as_of, min_n=opts["min_n"])
    out = opts["out"] or DEFAULT_OUT
    if opts["print"]:
        print(json.dumps(mem, indent=1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mem, indent=1) + "\n", encoding="utf-8")
    if not opts["print"]:
        print(f"wrote {out} ({mem['total_scored_reports']} scored report(s) pre-"
              f"{as_of:%Y-%m-%d %H:%M} UTC, overall {mem['overall_hit_rate_pct']}%, "
              f"{len(mem['best_patterns'])} best / {len(mem['worst_patterns'])} worst pattern(s))")


if __name__ == "__main__":
    main()
