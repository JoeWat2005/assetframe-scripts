"""memory_pack.py — assemble ONE token-bounded memory pack per asset for the brief
writer + critic. Bounded by design so prompt context never balloons as the ledger
grows: the underlying ledger_context / research_memory are already capped (recent_k,
top-N); this merges global + asset-class + instrument layers into a single compact
object and enforces an explicit token budget with compaction (drop least-specific
bulk first).

Layers (most → least specific):
  global         overall hit rate + calibration health (ledger/calibration_map.json)
  asset_class    this class's hit rate + best/worst reasoning patterns
  instrument     this instrument's hit rate, recent streak/drift, per-type rates, notes
  lessons        a short, capped list of the most relevant do/don't lines

NO LOOK-AHEAD: all history is filtered to windows that closed before `as_of`
(delegated to ledger_context.load_rows, which enforces it).

Usage:
  from memory_pack import build_pack
  pack = build_pack(asset, as_of=datetime.now(timezone.utc))   # dict, <= budget tokens
  python -m scripts.analytics.memory.memory_pack <asset_id> [--as-of "YYYY-MM-DD HH:MM"]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_context as LC
import research_memory as RM

CALIB_MAP = Path("ledger/calibration_map.json")
DEFAULT_LEDGER = Path("ledger/outcome_ledger.csv")
TOKEN_BUDGET = 1500          # approx ceiling for the whole pack
MAX_LESSONS = 8


def _approx_tokens(obj):
    return len(json.dumps(obj, ensure_ascii=False)) // 4


def _load(path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def build_pack(asset, as_of=None, ledger=DEFAULT_LEDGER, token_budget=TOKEN_BUDGET):
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    name = asset.get("instrument") or asset.get("ticker") or asset.get("id", "")
    cls = asset.get("asset_class", "")

    rows = LC.load_rows(ledger, as_of)                       # no-look-ahead
    ctx = LC.build_context(name, rows, ticker=asset.get("ticker"), asset_class=cls)
    # Compute cross-instrument memory FRESH from the ledger (the source of truth) rather
    # than trusting ledger/research_memory.json, which is stale if the daily refresh has
    # not run. The ledger read is cheap; staleness here would silently mis-inform the brief.
    mem = RM.build_memory(rows, as_of)
    calib = _load(CALIB_MAP) or {}

    by_class = (mem.get("by_asset_class") or {}).get(cls, {})

    def _class_rel(patterns):
        # asset_class is exact-matched; the old substring branch leaked unrelated cross-dimension
        # patterns (e.g. a 'crypto' regime pattern into an equity pack).
        return [p for p in (patterns or [])
                if p.get("dimension") == "asset_class" and p.get("pattern") == cls][:3]

    pack = {
        "instrument": ctx.get("instrument"), "ticker": ctx.get("ticker"), "asset_class": cls,
        "as_of_utc": as_of.strftime("%Y-%m-%d %H:%M") + " UTC",
        "global": {
            "overall_hit_rate_pct": mem.get("overall_hit_rate_pct"),
            "total_scored_reports": mem.get("total_scored_reports", 0),
            "calibration": {"method": calib.get("method"), "shrinkage_w": calib.get("shrinkage_w"),
                            "n_rows": calib.get("n_rows")},
        },
        "asset_class_history": {
            "hit_rate_pct": by_class.get("hit_rate_pct"), "reports": by_class.get("reports", 0),
            "best": _class_rel(mem.get("best_patterns")),
            "worst": _class_rel(mem.get("worst_patterns")),
        },
        "instrument_history": {
            "hit_rate_pct": ctx.get("instrument_hit_rate"),
            "reports": ctx.get("historical_prediction_count", 0),
            "recent_streak": ctx.get("recent_streak"),
            "recent_drift": ctx.get("recent_drift"),
            "prediction_type_hit_rates": ctx.get("prediction_type_hit_rates"),
            "known_failure_patterns": (ctx.get("known_failure_patterns") or [])[:3],
        },
        "lessons_for_ai": list(ctx.get("notes_for_ai", []))[:MAX_LESSONS],
    }

    # compaction to budget: shed least-specific bulk first, in order
    def _trim():
        if _approx_tokens(pack) <= token_budget:
            return False
        ach = pack["asset_class_history"]
        if len(ach.get("best", [])) + len(ach.get("worst", [])) > 2:
            ach["best"], ach["worst"] = ach["best"][:1], ach["worst"][:1]
            return True
        if len(pack["lessons_for_ai"]) > 3:
            pack["lessons_for_ai"] = pack["lessons_for_ai"][:3]
            return True
        if pack["instrument_history"].get("known_failure_patterns"):
            pack["instrument_history"]["known_failure_patterns"] = []
            return True
        return False
    while _trim():
        pass
    pack["budget"] = {"approx_tokens": _approx_tokens(pack), "limit": token_budget,
                      "within_budget": _approx_tokens(pack) <= token_budget}
    return pack


def main():
    import config_loader
    if len(sys.argv) < 2:
        print("usage: python -m scripts.analytics.memory.memory_pack <asset_id> [--as-of 'YYYY-MM-DD HH:MM']")
        sys.exit(2)
    asset = config_loader.get_asset(sys.argv[1])
    as_of = None
    if "--as-of" in sys.argv:
        s = sys.argv[sys.argv.index("--as-of") + 1]
        as_of = datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    print(json.dumps(build_pack(asset, as_of), indent=1))


if __name__ == "__main__":
    main()
