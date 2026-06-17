"""Social posts — template distribution copy from a published edition. NO posting.

Reads a compiled payload (data/payloads/<NAME>_af_payload.json, the same artifact
scaffold_payload.py writes) and optionally its brief, and templates four
distribution drafts -> data/social_posts/<NAME>_<DATE>_posts.json:
  x · linkedin · newsletter_snippet · reddit_summary

Consistent with the no-auto-trading / no-auto-posting policy this NEVER posts; it
emits drafts for a human (or a future, human-gated integration) to publish.

Every post:
  * uses "AssetFrame published..." framing, NEVER "you should buy/sell...";
  * expresses confidence as a BAND (taxonomy.confidence_band), never a hard promise;
  * carries a report-link placeholder and the "scored after the session closes" line;
  * ends with a short no-advice disclaimer.

SAFE-WORDING QA (REQUIRED): every generated post is scanned for pump/advice
language (the spirit of mvp_report's BANNED list — "buy now", "sell now",
"guaranteed", "sure thing", "you should buy/sell", "easy profit", "risk-free"). A
hit is a build error -> exit 2, nothing written. "Guaranteed" is allowed only in
negated compliance form ("no outcome is guaranteed").

Usage:
  python scripts/social_posts.py <NAME> [--payload data/payloads/<NAME>_af_payload.json]
         [--date YYYY-MM-DD] [--out data/social_posts/<NAME>_<DATE>_posts.json] [--print]

Exit codes: 0 ok / 2 missing payload, bad args, or safe-wording QA failure.
"""
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from taxonomy import confidence_band
except Exception:                       # standalone fallback (mirrors taxonomy.py)
    def confidence_band(score):
        try:
            s = float(score)
        except (TypeError, ValueError):
            return "Unknown"
        return "Low" if s < 50 else ("Moderate" if s < 65 else ("Elevated" if s < 80 else "High"))

REPORT_LINK = "{report_link}"          # placeholder filled at publish time
SCORED_LINE = "Scored after the session closes."
DISCLAIMER = "General market research, not personal financial advice. No outcome is guaranteed."

# Safe-wording QA — pump/advice phrases that must never appear in a draft.
BANNED = [r"\bbuy now\b", r"\bsell now\b", r"\bsure thing\b", r"\bsure trade\b",
          r"\beasy profit\b", r"\brisk[- ]free\b",
          r"\byou should buy\b", r"\byou should sell\b",
          r"\bget rich\b", r"\bcan'?t lose\b", r"\bto the moon\b"]
# "guaranteed" is allowed ONLY in negated compliance form.
GUARANTEED_OK = re.compile(r"(no outcome is|not|never|nothing[^.]{0,20}is)\s+guaranteed")


def die(msg):
    print(f"ERROR: {msg}")
    sys.exit(2)


def safe_wording_check(posts):
    """Reject any draft containing pump/advice language. Returns on pass; exits 2
    with a clear message on the first offending post."""
    for key, text in posts.items():
        low = text.lower()
        for pat in BANNED:
            if re.search(pat, low):
                die(f"safe-wording QA failed in '{key}' post: matched /{pat}/ — "
                    f"rephrase to neutral 'AssetFrame published...' framing")
        for m in re.finditer(r"guaranteed", low):
            window = low[max(0, m.start() - 40):m.start()]
            if not GUARANTEED_OK.search(window + " guaranteed"):
                die(f"safe-wording QA failed in '{key}' post: 'guaranteed' used outside "
                    f"negated compliance form")


def _meta(payload):
    m = payload.get("meta") or {}
    title = payload.get("title") or m.get("instrument") or "this instrument"
    ticker = m.get("ticker") or ""
    status = (payload.get("status") or m.get("status") or "").strip()
    risk = (payload.get("risk") or m.get("risk_rating") or "").strip()
    band = m.get("confidence_band") or confidence_band(payload.get("confidence"))
    view = (m.get("research_view") or m.get("primary_bias") or "").strip()
    window = (f"{m.get('prediction_window_start_report_tz', '')} -> "
              f"{m.get('prediction_window_end_report_tz', '')}").strip(" ->")
    report_id = payload.get("report_id") or ""
    return {"title": title, "ticker": ticker, "status": status, "risk": risk,
            "band": band, "view": view, "window": window, "report_id": report_id}


def build_posts(payload, brief=None):
    d = _meta(payload)
    # research_view can be a long sentence; trim for the short channels.
    view = d["view"]
    view_short = view if len(view) <= 180 else view[:177].rstrip() + "..."
    tail = f"{d['title']}".strip()
    status_clause = f"Status: {d['status']}." if d["status"] else ""
    risk_clause = f"Risk: {d['risk']}." if d["risk"] else ""

    x = (f"AssetFrame published its next-session read on {tail}. "
         f"{status_clause} Confidence band: {d['band']}. {risk_clause} "
         f"{SCORED_LINE} Full report: {REPORT_LINK} "
         f"{DISCLAIMER}").replace("  ", " ").strip()

    linkedin = (
        f"AssetFrame published a next-session market-intelligence report on {tail}.\n\n"
        f"Research view: {view_short or 'see report'}\n"
        f"{status_clause} {risk_clause} Confidence band: {d['band']}.\n"
        f"Window: {d['window']}.\n\n"
        f"Every call is a falsifiable prediction registered up front and scored after the session "
        f"closes, so the track record is the audit. Read it: {REPORT_LINK}\n\n"
        f"{DISCLAIMER}"
    ).strip()

    newsletter_snippet = (
        f"{tail}: AssetFrame's latest read is published. {status_clause} "
        f"Confidence band {d['band']}; {risk_clause.lower() or 'risk noted in the report.'} "
        f"{view_short or ''} {SCORED_LINE} Read the full report -> {REPORT_LINK}. {DISCLAIMER}"
    ).replace("  ", " ").strip()

    reddit_summary = (
        f"AssetFrame published a next-session report on {tail} "
        f"({d['report_id'] or 'see link'}).\n\n"
        f"- Research view: {view_short or 'see report'}\n"
        f"- {status_clause} {risk_clause} Confidence band: {d['band']}\n"
        f"- Window: {d['window']}\n"
        f"- {SCORED_LINE} Predictions are registered before the window and graded against the "
        f"tape, so accuracy is checkable, not claimed.\n\n"
        f"Report: {REPORT_LINK}\n\n"
        f"{DISCLAIMER} Not affiliated advice; do your own research."
    ).strip()

    return {"x": x, "linkedin": linkedin,
            "newsletter_snippet": newsletter_snippet, "reddit_summary": reddit_summary}


def parse_args(argv):
    opts = {"payload": None, "date": None, "out": None, "print": False}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--payload":
            i += 1; opts["payload"] = argv[i]
        elif a == "--date":
            i += 1; opts["date"] = argv[i]
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
        print("usage: python scripts/social_posts.py <NAME> [--payload path] "
              "[--date YYYY-MM-DD] [--out path] [--print]")
        sys.exit(2)
    name = sys.argv[1]
    opts = parse_args(sys.argv[2:])

    payload_path = Path(opts["payload"] or f"data/payloads/{name}_af_payload.json")
    if not payload_path.exists():
        die(f"payload not found: {payload_path} (publish/scaffold the edition first)")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {payload_path}: {e}")

    brief = None
    brief_path = Path(f"data/briefs/{name}_research_brief.json")
    if brief_path.exists():
        try:
            brief = json.loads(brief_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            brief = None

    date = (opts["date"] or (payload.get("meta") or {}).get("report_date")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    posts = build_posts(payload, brief)
    safe_wording_check(posts)           # REQUIRED gate — exits 2 on any pump/advice phrase

    result = {
        "instrument": (payload.get("meta") or {}).get("instrument", name),
        "ticker": (payload.get("meta") or {}).get("ticker", ""),
        "report_id": payload.get("report_id", ""),
        "date": date,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC",
        "report_link_placeholder": REPORT_LINK,
        "auto_post": False,
        "safe_wording_qa": "passed",
        "posts": posts,
    }

    out = Path(opts["out"] or f"data/social_posts/{name}_{date}_posts.json")
    if opts["print"]:
        print(json.dumps(result, indent=1))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    if not opts["print"]:
        print(f"wrote {out} (4 drafts, safe-wording QA passed, no auto-posting)")


if __name__ == "__main__":
    main()
