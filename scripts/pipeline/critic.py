"""critic.py — adversarial reviewer for an AssetFrame research brief.

A SECOND, independent Anthropic call that stress-tests the brief brief_writer.py
produced. It is deliberately suspicious: its job is to catch claim-gating breaches,
fabrication, banned language, taxonomy errors, internal inconsistency, look-ahead,
and over-confidence BEFORE the brief is compiled and a prediction is frozen into the
append-only ledger. It is the AI half of "AI drafts, Python validates, a human
approves" — a cheap automated reviewer in front of the human gate.

It returns a structured JSON verdict and signals its decision two ways (the caller
may use EITHER):
  * stdout: the verdict JSON (parse `decision`), and
  * exit code: 0 on approve/revise, 2 on reject/stand_aside.

Verdict shape:
  {
    "decision": "approve" | "revise" | "reject" | "stand_aside",
    "summary": "one-line overall judgement",
    "issues": [ {"severity": "blocker|major|minor", "field": "...", "problem": "...",
                 "fix": "..."} ],
    "confidence_adjustments": [ "lower conviction because ..." ],
    "publish_blockers": [ "..." ],          # must be empty for approve
    "stand_aside_reason": ""                  # set when decision == stand_aside
  }

Decision meanings:
  approve       publishable as-is (no blockers).
  revise        fixable issues — the caller should re-author once with these issues.
  reject        a hard rule is broken and the brief should not be used this run.
  stand_aside   the HONEST call is no-trade / wait (not a brief defect) — skip publish.

Usage:
  python scripts/critic.py <brief_path> --asset <TICKER> \
      [--analysis <path>] [--research <path>] [--model <id>] [--max-tokens N]

Reads ANTHROPIC_API_KEY from the environment (clear error + exit 3 if unset).
Exit codes: 0 approve/revise · 2 reject/stand_aside · 3 API/auth/usage error.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Reuse the writer's model default + input loaders so the two stay in lock-step.
from brief_writer import (DEFAULT_MODEL, _load_json, _require_sdk, _client,
                          summarize_analysis, summarize_research)

DEFAULT_MAX_TOKENS = 3000
DECISIONS = ("approve", "revise", "reject", "stand_aside")
# A non-defect skip (the market call is genuinely no-trade) vs a defect rejection.
PASS_DECISIONS = ("approve", "revise")     # exit 0
FAIL_DECISIONS = ("reject", "stand_aside")  # exit 2


SYSTEM_PROMPT = """\
You are the adversarial reviewer for AssetFrame research briefs. You did NOT write the \
brief; your job is to try to BREAK it before it is compiled into a published report and a \
prediction is frozen into an append-only ledger. Be rigorous, specific and fair — flag real \
problems, do not invent nitpicks, and do not rewrite the brief.

Review the brief against these rules and report what you find:

1. CLAIM GATING. Each claim is {claim, status, source, used_in_thesis}. confirmed / \
multiple-source may drive the thesis; single-source may support but not centre it; \
unverified / stale / unavailable MUST NOT be used_in_thesis. Every used_in_thesis claim \
needs a real source URL. Flag overstated status (e.g. a single blog cited as "confirmed"), \
a thesis resting on a single-source or weak claim, or a missing/empty source on a thesis claim.

2. NO FABRICATION. Prices, news, analyst ratings, metrics, dates and sources must be real and \
sourced. Flag any figure or event that looks invented, internally contradictory, or \
unsupported by the supplied research pack / market context.

3. NO PRICES AUTHORED. The brief must NOT contain authored price levels, pivots, bands, R:R \
ratios, ladders or a confidence number — those are the engine's job. Levels must be described \
in words. Flag any quoted price/level/R:R/confidence number that the analyst typed in.

4. BANNED LANGUAGE. No "you should buy/sell", "sure trade", "risk-free", "easy profit"; \
"guaranteed"/"recommendation" only in negated compliance form. The verdict must be conditional, \
never an instruction. free_bullets / free_scenarios must be PLAIN (no R:R, entry zone, \
invalidation, T1/T2, ladder, source audit, outcome ledger, hedging, risk math).

5. TAXONOMY VALIDITY. primary_prediction.type and alternative_prediction.type must be one of \
breakout / rejection / continuation / mean_reversion / range_hold / volatility_expansion. \
directional_view in bullish/bearish/neutral/mixed. The type must actually MATCH the thesis \
(e.g. don't tag a fade as "continuation").

6. INTERNAL CONSISTENCY. status, directional_view, primary_bias, the scenarios, the preferred \
setup and the verdict must tell ONE coherent story. Flag contradictions (e.g. bullish view but \
short-biased preferred setup with no explanation).

7. NO LOOK-AHEAD / honest uncertainty. The brief must not assume the outcome of an in-window \
event it cannot yet know (e.g. treating an unconfirmed press-conference tone as fact). \
Unknowns belong in source_gaps and must cap conviction.

8. OVER-CONFIDENCE. Given cold/stale indicators, thin sourcing, weak ledger history, or \
unresolved in-window catalysts, the conviction and scenario qualities must be honest. Recommend \
conviction reductions where warranted (the deterministic engine sets the actual number; you \
advise direction of travel).

DECISION:
  approve      no blockers; publishable as the analyst's honest research view.
  revise       fixable issues exist — list them so the author can repair the brief.
  reject       a hard rule is broken (fabrication, a weak claim driving the thesis, authored \
prices, banned language) and the brief must not be used this run.
  stand_aside  the brief is sound but the HONEST market call is no-trade / wait — not a defect, \
but do not publish a conviction view. Use this only when the analyst's own preferred_setup is \
"wait" AND neither side is better than Low quality.

OUTPUT EXACTLY ONE JSON object, nothing else:
{
  "decision": "approve|revise|reject|stand_aside",
  "summary": "one sentence",
  "issues": [ {"severity": "blocker|major|minor", "field": "path", "problem": "...", "fix": "..."} ],
  "confidence_adjustments": [ "short directional notes, may be empty" ],
  "publish_blockers": [ "hard blockers; MUST be empty when decision is approve" ],
  "stand_aside_reason": "set only when decision is stand_aside, else empty string"
}"""


def _verdict_errors(v):
    """Validate the critic's own output so a malformed verdict can't masquerade as a pass."""
    if not isinstance(v, dict):
        return ["verdict is not a JSON object"]
    errs = []
    if v.get("decision") not in DECISIONS:
        errs.append(f"decision={v.get('decision')!r} not in {list(DECISIONS)}")
    if not v.get("summary"):
        errs.append("missing 'summary'")
    for key in ("issues", "confidence_adjustments", "publish_blockers"):
        if not isinstance(v.get(key, []), list):
            errs.append(f"'{key}' must be a list")
    return errs


def _extract_json(blocks):
    text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
    if not text.strip():
        return None, "critic returned no text content"
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None, "no JSON object in critic output"
    try:
        return json.loads(text[s:e + 1]), None
    except json.JSONDecodeError as ex:
        return None, f"could not parse critic JSON: {ex}"


def build_user_message(asset, brief, analysis, research):
    payload = {
        "asset": asset,
        "brief_under_review": brief,
        # context the critic checks the brief AGAINST (compacted, same as the writer saw)
        "market_context": summarize_analysis(analysis) if analysis else
                          "not supplied — judge fabrication/consistency on the brief alone",
        "research_pack": summarize_research(research) if research else
                         "not supplied — flag thesis claims that cite sources you cannot verify here",
    }
    return ("Adversarially review this AssetFrame brief and return your verdict JSON only.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1))


def review_brief(asset, brief, analysis, research, *, model, max_tokens):
    """Run the critic. Returns (verdict, telemetry). Raises SystemExit(3) on API error
    or an unparseable/malformed verdict."""
    anthropic = _require_sdk()
    client = _client(anthropic)
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens,
            # Prompt caching: the critic's rubric SYSTEM_PROMPT (~1k tok) is static across every
            # asset, so cache it once per run and read it at 0.1x thereafter.
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": build_user_message(asset, brief, analysis, research)}],
        )
    except Exception as ex:
        print(f"ERROR: Anthropic API call failed: {type(ex).__name__}: {ex}", file=sys.stderr)
        sys.exit(3)

    u = getattr(resp, "usage", None)
    in_tok = getattr(u, "input_tokens", 0) or 0
    out_tok = getattr(u, "output_tokens", 0) or 0

    verdict, perr = _extract_json(resp.content)
    errs = [perr] if perr else _verdict_errors(verdict)
    if errs:
        print("ERROR: critic produced an invalid verdict:\n  - " + "\n  - ".join(errs),
              file=sys.stderr)
        sys.exit(3)

    # defensive coherence: an approve must not carry publish blockers
    if verdict["decision"] == "approve" and verdict.get("publish_blockers"):
        verdict["decision"] = "revise"
        verdict.setdefault("summary", "")
        verdict["summary"] += " [downgraded approve->revise: publish_blockers were present]"

    telemetry = {"model": model, "input_tokens": in_tok, "output_tokens": out_tok}
    return verdict, telemetry


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="critic.py",
        description="Adversarially review an AssetFrame research brief with a second, "
                    "independent Anthropic call. Exit 0 approve/revise, 2 reject/stand_aside.")
    p.add_argument("brief_path", help="path to the research brief JSON to review")
    p.add_argument("--asset", required=True, help="instrument ticker / NAME (e.g. BTC, AAPL)")
    p.add_argument("--analysis", help="optional engine analysis JSON (improves the review)")
    p.add_argument("--research", help="optional research pack JSON (to check claim sourcing)")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model id (default {DEFAULT_MODEL})")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, dest="max_tokens",
                   help=f"max output tokens (default {DEFAULT_MAX_TOKENS})")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    brief = _load_json(args.brief_path, required=True, what="brief")
    analysis = _load_json(args.analysis, required=False, what="analysis")
    research = _load_json(args.research, required=False, what="research pack")

    verdict, telemetry = review_brief(args.asset, brief, analysis, research,
                                      model=args.model, max_tokens=args.max_tokens)

    # verdict JSON to stdout (machine-parseable); telemetry to stderr
    print(json.dumps({**verdict, "_telemetry": telemetry}, ensure_ascii=False, indent=1))
    print(f"critic: decision={verdict['decision']} model={telemetry['model']} "
          f"in_tok={telemetry['input_tokens']} out_tok={telemetry['output_tokens']}",
          file=sys.stderr)

    sys.exit(0 if verdict["decision"] in PASS_DECISIONS else 2)


if __name__ == "__main__":
    main()
