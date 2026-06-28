"""Social pack — OPTIONAL soft signal. Validate + structure social sentiment.

The AI gathers social chatter (last30days skill / WebSearch) and hands a DRAFT
JSON here; Python does NOT call the web. This script normalizes it and writes
data/social/<NAME>_social_pack.json.

OPTIONALITY: the whole pipeline runs without social — scaffold_payload.py passes
social=None and confidence falls through to a 0 adjustment. Skip this script
entirely and nothing breaks.

SOCIAL MAY ONLY REDUCE CONFIDENCE, NEVER RAISE IT. Social is sentiment awareness,
crowding/hype/contrarian risk and catalyst discovery — never a factual claim,
never a thesis driver, never confidence generation. The `aggregate` block here is
exactly what confidence.social_adjustment() consumes: it reads
`aggregate.hype_risk`, `aggregate.crowding_risk` (low|medium|high penalties) and a
truthy `aggregate.contrarian_warning`. Those are subtract-only.

With no --in, EMIT A TEMPLATE skeleton so the AI has the exact shape to fill.

Schema:
{
  "instrument": "...", "generated_at_utc": "... UTC",
  "sources": [
    {"platform": "X|Reddit|Stocktwits|YouTube|news_comments|other",
     "url": "...", "timestamp": "... UTC", "summary": "...",
     "sentiment": "bullish|bearish|mixed|neutral", "themes": ["..."],
     "signal_quality": "high|medium|low", "notes": "..."}
  ],
  "aggregate": {
    "sentiment": "bullish|bearish|mixed|neutral",
    "dominant_themes": ["..."],
    "crowding_risk": "low|medium|high|unknown",
    "hype_risk": "low|medium|high|unknown",
    "contrarian_warning": "" ,
    "source_gaps": ["..."]
  }
}

Usage:
  python -m scripts.pipeline.packs.social_pack <NAME> [--in <draft.json>]
         [--out data/social/<NAME>_social_pack.json] [--print]

Exit codes: 0 ok / 2 validation error (bad platform / sentiment / risk value).
"""
import json, sys
from datetime import datetime, timezone
from pathlib import Path

PLATFORMS = ("X", "Reddit", "Stocktwits", "YouTube", "news_comments", "other")
SENTIMENT = ("bullish", "bearish", "mixed", "neutral")
SIGNAL_QUALITY = ("high", "medium", "low")
RISK = ("low", "medium", "high", "unknown")

# canonical-case lookup so the AI can write "reddit", "STOCKTWITS", etc.
_PLATFORM_CANON = {p.lower(): p for p in PLATFORMS}


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(2)


def _now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def template(name):
    return {
        "instrument": name,
        "generated_at_utc": _now_utc(),
        "sources": [
            {"platform": "Reddit", "url": "", "timestamp": "", "summary": "",
             "sentiment": "mixed", "themes": [], "signal_quality": "medium", "notes": ""},
        ],
        "aggregate": {
            "sentiment": "neutral",
            "dominant_themes": [],
            "crowding_risk": "unknown",
            "hype_risk": "unknown",
            "contrarian_warning": "",
            "source_gaps": [],
        },
    }


def _check(value, allowed, field, idx=None):
    v = (value or "").strip().lower()
    if v not in allowed:
        where = f"source {idx} " if idx is not None else ""
        die(f"{where}{field}={value!r} not one of {list(allowed)}")
    return v


def validate(draft, name):
    """Normalize. Flags low-quality / unsourced sources but never asserts facts —
    it only structures sentiment so confidence can apply a subtract-only penalty."""
    if not isinstance(draft, dict):
        die("social draft must be a JSON object")
    instrument = (draft.get("instrument") or name).strip() or name
    raw_sources = draft.get("sources")
    if raw_sources is None:
        raw_sources = []
    if not isinstance(raw_sources, list):
        die("'sources' must be a list")

    agg_in = draft.get("aggregate") or {}
    gaps = list(agg_in.get("source_gaps") or [])

    sources = []
    for n, s in enumerate(raw_sources, 1):
        if not isinstance(s, dict):
            die(f"source {n} is not an object")
        plat_raw = (s.get("platform") or "").strip()
        plat = _PLATFORM_CANON.get(plat_raw.lower())
        if plat is None:
            die(f"source {n} platform={plat_raw!r} not one of {list(PLATFORMS)}")
        sentiment = _check(s.get("sentiment"), SENTIMENT, "sentiment", n)
        quality = _check(s.get("signal_quality"), SIGNAL_QUALITY, "signal_quality", n)
        themes = [str(t).strip() for t in (s.get("themes") or []) if str(t).strip()]
        url = (s.get("url") or "").strip()
        # flag, never reject: unsourced or low-signal chatter is noted as a gap.
        if not url:
            gaps.append(f"unsourced social ({plat}): {(s.get('summary') or '')[:60]}")
        if quality == "low":
            gaps.append(f"low-signal source ({plat}): {(s.get('summary') or '')[:60]}")
        sources.append({
            "platform": plat, "url": url,
            "timestamp": (s.get("timestamp") or "").strip(),
            "summary": (s.get("summary") or "").strip(),
            "sentiment": sentiment, "themes": themes,
            "signal_quality": quality, "notes": (s.get("notes") or "").strip(),
        })

    # aggregate — the exact shape confidence.social_adjustment() reads.
    aggregate = {
        "sentiment": _check(agg_in.get("sentiment") or "neutral", SENTIMENT, "aggregate.sentiment"),
        "dominant_themes": [str(t).strip() for t in (agg_in.get("dominant_themes") or [])
                            if str(t).strip()],
        "crowding_risk": _check(agg_in.get("crowding_risk") or "unknown", RISK, "aggregate.crowding_risk"),
        "hype_risk": _check(agg_in.get("hype_risk") or "unknown", RISK, "aggregate.hype_risk"),
        "contrarian_warning": (agg_in.get("contrarian_warning") or "").strip(),
        "source_gaps": gaps,
    }

    return {
        "instrument": instrument,
        "generated_at_utc": (draft.get("generated_at_utc") or _now_utc()),
        "sources": sources,
        "aggregate": aggregate,
        "note": "Supplementary, non-authoritative sentiment. Confidence impact is subtract-only.",
        "counts": {"sources": len(sources), "source_gaps": len(gaps)},
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
        print("usage: python -m scripts.pipeline.packs.social_pack <NAME> [--in draft.json] "
              "[--out path] [--print]  (entirely optional — pipeline runs without it)")
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

    out = Path(opts["out"] or f"data/social/{name}_social_pack.json")
    if opts["print"]:
        print(json.dumps(pack, indent=1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, indent=1) + "\n", encoding="utf-8")
    if not opts["print"]:
        c = pack.get("counts") or {}
        agg = pack.get("aggregate") or {}
        print(f"wrote {out} ({emitted}: {c.get('sources', 0)} source(s), "
              f"hype={agg.get('hype_risk')}, crowding={agg.get('crowding_risk')})")


if __name__ == "__main__":
    main()
