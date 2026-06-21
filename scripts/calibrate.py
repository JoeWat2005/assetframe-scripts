"""Confidence calibration — fit realised hit-rate on the engine's confidence and
write ledger/calibration_map.json for confidence.compute_confidence to apply.

The map is fitted on the PRE-calibration score (ledger column `conf_raw`, the
capped score before any map was applied) so there is no feedback loop. It falls
back to the published `confidence` column for legacy rows that predate conf_raw.

Method: weighted isotonic regression (Pool Adjacent Violators) of realised
hit-rate on conf_raw, then SHRINKAGE toward identity:

    published = (1 - w) * raw + w * (realised_rate * 100),   w = min(1, n_rows / N_FULL)

So with < ~10 scored rows the map is essentially the identity (we never "correct"
on noise); it earns its adjustment only as the ledger fills. Only rows from the
current engine (`conf_version` == confidence.CONF_VERSION) are used, so the old
freehand-era scores don't contaminate the fit.

Usage:
  python scripts/calibrate.py [--ledger ledger/outcome_ledger.csv]
         [--out ledger/calibration_map.json] [--conf-version 2]
         [--n-full 40] [--min-rows 5] [--dry-run]

Exit 0 always (an empty/young ledger writes a valid identity map).
"""
import csv, json, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from confidence import CONF_VERSION
except Exception:           # allow standalone use if confidence.py is unavailable
    CONF_VERSION = 2

DEFAULT_LEDGER = Path("ledger/outcome_ledger.csv")
DEFAULT_OUT = Path("ledger/calibration_map.json")
N_FULL = 40        # rows at which the fit is trusted at full weight
MIN_ROWS = 5       # below this, identity regardless of weight (extra safety)


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def load_points(ledger_path, conf_version):
    """Return [(raw_score, realised_rate_0_1, weight_n), ...] for usable rows."""
    pts = []
    if not Path(ledger_path).exists():
        return pts
    with open(ledger_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            cv = (r.get("conf_version") or "").strip()
            if cv and conf_version is not None and cv != str(conf_version):
                continue
            raw = (r.get("conf_raw") or r.get("confidence") or "").strip()
            try:
                x = float(raw)
            except ValueError:
                continue
            try:
                hits = int(r.get("hits") or 0)
                misses = int(r.get("misses") or 0)
            except ValueError:
                continue
            n = hits + misses
            if n <= 0:
                continue
            pts.append((x, hits / n, n))
    return pts


def _merge_duplicate_x(points):
    """Combine rows with the same raw score into one weighted observation."""
    agg = {}
    for x, y, w in points:
        sw, swy = agg.get(x, (0.0, 0.0))
        agg[x] = (sw + w, swy + y * w)
    # sw is a sum of positive sample counts (load_points enforces n>0), but guard divide-by-zero
    # defensively so a future caller passing a zero-weight point can't crash the fit.
    merged = [(x, (swy / sw if sw else 0.5), sw) for x, (sw, swy) in agg.items()]
    merged.sort(key=lambda p: p[0])
    return merged


def pava(ys, ws):
    """Weighted Pool Adjacent Violators -> monotonic non-decreasing fit, one
    value per input point (inputs assumed sorted by x)."""
    blocks = []  # [mean, weight, count]
    for y, w in zip(ys, ws):
        blocks.append([y, w, 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            y2, w2, c2 = blocks.pop()
            y1, w1, c1 = blocks.pop()
            ws_ = w1 + w2
            blocks.append([(y1 * w1 + y2 * w2) / ws_, ws_, c1 + c2])
    out = []
    for mean, _w, c in blocks:
        out.extend([mean] * c)
    return out


def build_map(points, n_full=N_FULL, min_rows=MIN_ROWS):
    n_rows = len(points)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    base = {"version": 1, "conf_version": CONF_VERSION, "n_rows": n_rows,
            "fitted_at": stamp + " UTC"}
    if n_rows < min_rows:
        return {**base, "method": "identity", "shrinkage_w": 0.0,
                "knots": [[0.0, 0.0], [100.0, 100.0]]}
    merged = _merge_duplicate_x(points)
    xs = [p[0] for p in merged]
    ys = [p[1] for p in merged]
    ws = [p[2] for p in merged]
    fitted = pava(ys, ws)            # realised-rate fit in 0..1, monotonic
    w = min(1.0, n_rows / float(n_full))
    knots = []
    for x, y01 in zip(xs, fitted):
        y = (1 - w) * x + w * (y01 * 100.0)
        knots.append([round(x, 1), round(_clamp(y), 1)])
    # guarantee endpoints and strictly ascending x for safe interpolation
    if knots[0][0] > 0:
        knots.insert(0, [0.0, knots[0][1]])
    if knots[-1][0] < 100:
        knots.append([100.0, knots[-1][1]])
    dedup = []
    for kx, ky in knots:
        if dedup and kx == dedup[-1][0]:
            dedup[-1][1] = ky
        else:
            dedup.append([kx, ky])
    return {**base, "method": "isotonic+shrinkage", "shrinkage_w": round(w, 3),
            "knots": dedup}


def parse_args(argv):
    opts = {"ledger": DEFAULT_LEDGER, "out": DEFAULT_OUT, "conf_version": CONF_VERSION,
            "n_full": N_FULL, "min_rows": MIN_ROWS, "dry_run": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--ledger":
            i += 1; opts["ledger"] = Path(argv[i])
        elif a == "--out":
            i += 1; opts["out"] = Path(argv[i])
        elif a == "--conf-version":
            i += 1; opts["conf_version"] = None if argv[i] in ("", "all") else int(argv[i])
        elif a == "--n-full":
            i += 1; opts["n_full"] = int(argv[i])
        elif a == "--min-rows":
            i += 1; opts["min_rows"] = int(argv[i])
        elif a == "--dry-run":
            opts["dry_run"] = True
        else:
            print(f"ERROR: unknown argument {a}"); sys.exit(2)
        i += 1
    return opts


def main():
    opts = parse_args(sys.argv[1:])
    points = load_points(opts["ledger"], opts["conf_version"])
    cmap = build_map(points, opts["n_full"], opts["min_rows"])
    print(json.dumps(cmap, indent=1))
    if not opts["dry_run"]:
        opts["out"].parent.mkdir(parents=True, exist_ok=True)
        opts["out"].write_text(json.dumps(cmap, indent=1) + "\n", encoding="utf-8")
        print(f"\nwrote {opts['out']}")


if __name__ == "__main__":
    main()
