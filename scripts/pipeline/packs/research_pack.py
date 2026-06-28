"""Research pack — validate + structure the AI's sourced news, never invent it.

The AI gathers macro / asset / earnings / calendar / regulatory / geopolitical
context with its own web tools, then hands a DRAFT JSON here. Python is the
compiler/validator: it does NOT call the web. This script normalizes that draft
and writes data/research/<NAME>_research_pack.json, which scaffold_payload.py and
confidence.catalyst_confidence read (a thesis claim whose source isn't in this
pack is downgraded).

THE GATE (the "AI may interpret news, never invent it" rule): every item marked
`used_in_thesis` MUST carry a non-empty source (source_url or named source) AND a
timestamp. A thesis item lacking either is an unsupported claim -> exit 2 before
anything is written. Unsourced non-thesis items are demoted into `source_gaps`
rather than silently kept.

With no --in, EMIT A TEMPLATE skeleton so the AI has the exact shape to fill.

Schema:
{
  "instrument": "Apple Inc. (AAPL)",
  "generated_at_utc": "2026-06-16 13:00 UTC",
  "items": [
    {"category": "macro|asset|earnings|calendar|regulatory|geopolitical",
     "headline": "...", "summary": "...", "source_url": "https://...",
     "timestamp": "2026-06-15 18:30 UTC", "source_quality": "high|medium|low",
     "used_in_thesis": true}
  ],
  "source_gaps": ["options IV not sourced", ...]
}

Usage:
  python -m scripts.pipeline.packs.research_pack <NAME> [--in <draft.json>]
         [--out data/research/<NAME>_research_pack.json] [--print]

Exit codes: 0 ok / 2 validation error (unsupported thesis claim, bad category...).
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = ("macro", "asset", "earnings", "calendar", "regulatory", "geopolitical")
QUALITY = ("high", "medium", "low")


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(2)


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def _has_source(item):
    return bool((item.get("source_url") or item.get("source") or "").strip())


def template(name):
    """Skeleton for the AI to fill — two illustrative items, one per common need."""
    return {
        "instrument": name,
        "generated_at_utc": _now_utc(),
        "items": [
            {"category": "asset", "headline": "", "summary": "",
             "source_url": "", "timestamp": "", "source_quality": "high",
             "used_in_thesis": True},
            {"category": "macro", "headline": "", "summary": "",
             "source_url": "", "timestamp": "", "source_quality": "medium",
             "used_in_thesis": False},
        ],
        "source_gaps": [],
    }


def validate(draft, name):
    """Normalize + enforce the no-invention gate. Returns the clean pack."""
    if not isinstance(draft, dict):
        die("research draft must be a JSON object")
    instrument = (draft.get("instrument") or name).strip() or name
    raw_items = draft.get("items")
    if raw_items is None:
        die("research draft missing 'items' (use the template with --in omitted)")
    if not isinstance(raw_items, list):
        die("'items' must be a list")

    gaps = list(draft.get("source_gaps") or [])
    items = []
    for n, it in enumerate(raw_items, 1):
        if not isinstance(it, dict):
            die(f"item {n} is not an object")
        cat = (it.get("category") or "").strip().lower()
        if cat not in CATEGORIES:
            die(f"item {n} category={it.get('category')!r} not one of {list(CATEGORIES)}")
        headline = (it.get("headline") or "").strip()
        if not headline:
            die(f"item {n} ({cat}) has an empty headline")
        quality = (it.get("source_quality") or "").strip().lower()
        if quality not in QUALITY:
            die(f"item {n} ({headline[:40]}) source_quality={it.get('source_quality')!r} "
                f"not one of {list(QUALITY)}")
        used = bool(it.get("used_in_thesis"))
        sourced = _has_source(it)
        ts = (it.get("timestamp") or "").strip()

        # THE GATE: a thesis item must be sourced AND timestamped.
        if used and not (sourced and ts):
            missing = []
            if not sourced:
                missing.append("source_url/source")
            if not ts:
                missing.append("timestamp")
            die(f"unsupported thesis claim: item {n} '{headline[:60]}' is used_in_thesis "
                f"but missing {', '.join(missing)} (the AI may interpret news, never invent it)")
        # A non-thesis item that lacks a source is a known gap, not a fact.
        if not sourced and headline not in gaps:
            gaps.append(f"unsourced: {headline[:80]}")

        url = (it.get("source_url") or it.get("url") or "").strip()
        items.append({
            "category": cat, "headline": headline,
            "summary": (it.get("summary") or "").strip(),
            "source_url": url,
            # `url` mirrors source_url so confidence._claim_traced (which reads
            # item['url']/item['source']) can trace a thesis claim back to this pack.
            "url": url,
            "source": (it.get("source") or "").strip(),
            "timestamp": ts, "source_quality": quality,
            "used_in_thesis": used,
        })

    return {
        "instrument": instrument,
        "generated_at_utc": (draft.get("generated_at_utc") or _now_utc()),
        "items": items,
        "source_gaps": gaps,
        "counts": {
            "items": len(items),
            "thesis_items": sum(1 for i in items if i["used_in_thesis"]),
            "by_category": {c: sum(1 for i in items if i["category"] == c) for c in CATEGORIES},
            "source_gaps": len(gaps),
        },
    }


def parse_args(argv):
    opts = {"in": None, "out": None, "print": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--in":
            i += 1; opts["in"] = argv[i]
        elif a == "--out":
            i += 1; opts["out"] = argv[i]
        elif a == "--print":
            opts["print"] = True
        else:
            die(f"unknown argument {a}")
        i += 1
    return opts


def main():
    if len(sys.argv) < 2:
        print("usage: python -m scripts.pipeline.packs.research_pack <NAME> [--in draft.json] "
              "[--out path] [--print]")
        sys.exit(2)
    name = sys.argv[1]
    opts = parse_args(sys.argv[2:])

    if opts["in"]:
        src = Path(opts["in"])
        if not src.exists():
            die(f"draft not found: {src}")
        try:
            draft = json.loads(src.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            die(f"invalid JSON in {src}: {e}")
        pack = validate(draft, name)
        emitted = "validated"
    else:
        pack = template(name)
        emitted = "template"

    out = Path(opts["out"] or f"data/research/{name}_research_pack.json")
    if opts["print"]:
        print(json.dumps(pack, indent=1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, indent=1) + "\n", encoding="utf-8")
    if not opts["print"]:
        c = pack.get("counts") or {}
        print(f"wrote {out} ({emitted}: {c.get('items', 0)} item(s), "
              f"{c.get('thesis_items', 0)} thesis, {c.get('source_gaps', len(pack.get('source_gaps', [])))} gap(s))")


if __name__ == "__main__":
    main()
