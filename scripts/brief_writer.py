"""brief_writer.py — autonomous research-brief author (replaces the operator).

In Engine V2 the research brief (`data/briefs/<NAME>_research_brief.json`) is the
ONLY hand-authored artifact: the analyst's intent, thesis, sourced claims,
scenarios and verdict. Everything numeric/structural (levels, R:R, ladder,
predictions, confidence) is compiled downstream by scaffold_payload.py. This
script lets a Claude model play that analyst role — author INTENT, never prices.

What it does:
  1. Loads the engine analysis (compacted to a small market-context summary — trend,
     momentum, levels, freshness — NOT dumped wholesale), the bounded memory_pack
     (ledger history + lessons), and the optional research / social packs.
  2. Builds a strong SYSTEM prompt that encodes the SKILL.md authoring rules
     (banned language, claim gating, prediction taxonomy, no price/figure
     fabrication, research/decision-support posture) plus the EXACT brief schema.
  3. Calls the Anthropic Messages API with the server-side `web_search` tool so the
     model can research current macro/asset news and cite REAL source URLs in
     `claims[]` ({claim, status, source, used_in_thesis}).
  4. Forces a single JSON object, parses it, and VALIDATES it against the schema
     (required keys + enums). On a validation miss it RE-PROMPTS ONCE with the
     errors; if still invalid it exits non-zero with the errors.
  5. Writes the validated brief to --out and prints a one-line token/cost summary
     to stderr.

The schema validator (`validate_brief`) is the load-bearing contract — it mirrors
what scaffold_payload.py consumes, and is unit-tested against the real briefs in
data/briefs/ so it can never silently drift from the schema.

Usage:
  python scripts/brief_writer.py <TICKER> --analysis <path> --memory-pack <path> \
      [--research <path>] [--social <path>] --out <path> \
      [--model <id>] [--max-tokens N] [--guidance "critic issues ..."]

Reads ANTHROPIC_API_KEY from the environment (clear error + non-zero exit if unset,
so a keyless run degrades gracefully — run_daily falls back to needs_brief).

Exit codes: 0 ok / 2 usage or validation error / 3 API/auth error.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Latest capable Claude model — kept as a single constant so it is trivial to bump.
DEFAULT_MODEL = "claude-opus-4-8"
DEFAULT_MAX_TOKENS = 16000  # a full brief is ~7k tok of JSON and web_search tool turns
                            # share this budget; 8000 truncated the JSON mid-object ->
                            # parse/validation fail -> needs_brief (see req-8104b5f4).
# Per-million-token USD prices used only for the stderr cost estimate (best-effort;
# the ledger records the live `usage` numbers, not this estimate). Override via env.
PRICE_IN_PER_MTOK = float(os.environ.get("ANTHROPIC_PRICE_IN", "5.0"))
PRICE_OUT_PER_MTOK = float(os.environ.get("ANTHROPIC_PRICE_OUT", "25.0"))

# --- enums (mirror taxonomy.py + the claim/quality vocab the scaffold enforces) ---
PREDICTION_TYPES = ("breakout", "rejection", "continuation",
                    "mean_reversion", "range_hold", "volatility_expansion")
DIRECTIONS = ("bullish", "bearish", "neutral", "mixed")
SETUP_SIDES = ("long", "short", "wait")
HORIZONS = ("intraday", "next_session", "multi_session")
ASSET_CLASS_KEYS = ("equity", "crypto", "fx", "futures", "index", "commodity")
RISK_LEVELS = ("Low", "Medium", "High")
QUALITY = ("High quality", "Acceptable", "Low quality", "Management only", "No-trade")
CLAIM_STATUSES = ("confirmed", "multiple-source", "single-source",
                  "unverified", "stale", "unavailable")


# =====================================================================
# Schema validator — the contract scaffold_payload.py consumes.
# =====================================================================

def validate_brief(brief):
    """Return a list of human-readable validation errors ([] == valid).

    Checks the required keys + the enum/shape rules the scaffold relies on. This is
    deliberately the SAME contract scaffold_payload.py enforces (claim statuses,
    used_in_thesis gating, taxonomy enums) so a brief that passes here will not be
    rejected downstream for a structural reason. It does NOT re-run the engine's
    price/level binding (that is the scaffold's job) — it validates AUTHORSHIP.
    """
    errs = []
    if not isinstance(brief, dict):
        return ["brief is not a JSON object"]

    def req(key, typ=None, where=brief, label=None):
        label = label or key
        if key not in where or where[key] in (None, ""):
            errs.append(f"missing required field '{label}'")
            return False
        if typ is not None and not isinstance(where[key], typ):
            errs.append(f"'{label}' must be {typ.__name__}, got {type(where[key]).__name__}")
            return False
        return True

    def enum(key, allowed, where=brief, label=None):
        label = label or key
        if key in where and where[key] not in (None, ""):
            if where[key] not in allowed:
                errs.append(f"'{label}'={where[key]!r} not in {list(allowed)}")

    # --- identity / framing -------------------------------------------------
    for k in ("name", "ticker", "instrument", "asset_class_label",
              "asset_class_key", "session_profile", "venue"):
        req(k, str)
    enum("asset_class_key", ASSET_CLASS_KEYS)

    # status / risk / direction / horizon (status is free-ish text; risk is an enum)
    req("status", str)
    req("risk", str)
    enum("risk", RISK_LEVELS)
    req("directional_view", str)
    enum("directional_view", DIRECTIONS)
    req("horizon", str)
    enum("horizon", HORIZONS)
    req("market_regime", str)            # free text, normalized downstream

    req("primary_bias", str)
    req("research_view", str)
    enum("long_scenario_quality", QUALITY)
    enum("short_scenario_quality", QUALITY)

    # --- prediction intent (NO prices) -------------------------------------
    if req("primary_prediction", dict):
        pp = brief["primary_prediction"]
        if "type" not in pp or pp.get("type") in (None, ""):
            errs.append("missing required field 'primary_prediction.type'")
        elif pp["type"] not in PREDICTION_TYPES:
            errs.append(f"'primary_prediction.type'={pp['type']!r} not in {list(PREDICTION_TYPES)}")
        for sub in ("expected_move", "time_horizon", "reasoning"):
            if not pp.get(sub):
                errs.append(f"missing required field 'primary_prediction.{sub}'")
        if not isinstance(pp.get("invalidators"), list) or not pp.get("invalidators"):
            errs.append("'primary_prediction.invalidators' must be a non-empty list")

    if req("alternative_prediction", dict):
        ap = brief["alternative_prediction"]
        if ap.get("type") and ap["type"] not in PREDICTION_TYPES:
            errs.append(f"'alternative_prediction.type'={ap['type']!r} not in {list(PREDICTION_TYPES)}")
        if not ap.get("reasoning"):
            errs.append("missing required field 'alternative_prediction.reasoning'")

    if req("preferred_setup", dict):
        ps = brief["preferred_setup"]
        if ps.get("side") not in SETUP_SIDES:
            errs.append(f"'preferred_setup.side'={ps.get('side')!r} not in {list(SETUP_SIDES)}")
        if not ps.get("why_this_setup"):
            errs.append("missing required field 'preferred_setup.why_this_setup'")

    # --- narrative + context -----------------------------------------------
    req("exec_summary", str)
    if req("verdict", dict):
        for sub in ("line", "best", "risk", "stand_aside"):
            if not brief["verdict"].get(sub):
                errs.append(f"missing required field 'verdict.{sub}'")

    if "options_context_included" in brief and \
            not isinstance(brief["options_context_included"], bool):
        errs.append("'options_context_included' must be a boolean")

    # --- claims[] (the gated, sourced layer) -------------------------------
    if req("claims", list):
        for i, c in enumerate(brief["claims"]):
            tag = f"claims[{i}]"
            if not isinstance(c, dict):
                errs.append(f"{tag} is not an object")
                continue
            if not c.get("claim"):
                errs.append(f"{tag} missing 'claim' text")
            st = (c.get("status") or "").lower()
            if st not in CLAIM_STATUSES:
                errs.append(f"{tag}.status={c.get('status')!r} not in {list(CLAIM_STATUSES)}")
            used = bool(c.get("used_in_thesis"))
            # claim-gating rule (matches scaffold._claims + mvp_report THESIS_BLOCKED):
            # unverified/stale/unavailable claims must NOT drive the thesis.
            if used and st in ("unverified", "stale", "unavailable"):
                errs.append(f"{tag} is {st} but used_in_thesis=true — weak claims cannot drive the thesis")
            # a thesis claim needs a real source (the catalyst-confidence + audit need it)
            if used and not c.get("source"):
                errs.append(f"{tag} used_in_thesis=true but has no source — never assert an unsourced thesis claim")

    # --- the rest of the structured narrative ------------------------------
    if req("catalysts", list):
        for i, c in enumerate(brief["catalysts"]):
            if not isinstance(c, dict) or not c.get("label"):
                errs.append(f"catalysts[{i}] missing 'label'")
    req("risks", list)
    if req("scenario_matrix", list):
        for i, s in enumerate(brief["scenario_matrix"]):
            if not isinstance(s, dict) or not s.get("case"):
                errs.append(f"scenario_matrix[{i}] missing 'case'")

    if req("narrative", dict):
        nb = brief["narrative"]
        if not isinstance(nb.get("free_bullets"), list) or not nb.get("free_bullets"):
            errs.append("'narrative.free_bullets' must be a non-empty list")
        if not isinstance(nb.get("market_summary"), list) or not nb.get("market_summary"):
            errs.append("'narrative.market_summary' must be a non-empty list")
        for sub in ("long_short_view", "technicals_note", "stats_html"):
            if not nb.get(sub):
                errs.append(f"missing required field 'narrative.{sub}'")

    return errs


# =====================================================================
# Input loading + market-context compaction
# =====================================================================

def _load_json(path, required=True, what=""):
    if path is None:
        if required:
            print(f"ERROR: missing required input {what}", file=sys.stderr)
            sys.exit(2)
        return None
    p = Path(path)
    if not p.exists():
        if required:
            print(f"ERROR: {what or 'input'} not found: {path}", file=sys.stderr)
            sys.exit(2)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        print(f"ERROR: invalid JSON in {path}: {e}", file=sys.stderr)
        sys.exit(2)


def summarize_analysis(a):
    """Compact the engine analysis into the small market context the author needs:
    trend / momentum / levels / freshness. Deliberately NOT a wholesale dump — and
    note prominently that LEVEL VALUES are reference-only context, NEVER to be typed
    into the brief (the scaffold owns every price)."""
    h = a.get("hourly") or {}
    d = a.get("daily") or {}
    fr = a.get("freshness") or {}
    macd = h.get("macd") or {}
    piv = a.get("pivots_classic") or {}
    bands = a.get("atr_day_bands") or {}
    win = a.get("windows") or {}

    def r(x, n=2):
        return round(x, n) if isinstance(x, (int, float)) and not isinstance(x, bool) else x

    related = [{"symbol": x.get("symbol"), "chg_1d_pct": x.get("chg_1d_pct"),
                "chg_5d_pct": x.get("chg_5d_pct")} for x in (a.get("related") or [])]

    return {
        "symbol": a.get("symbol"),
        "last_price_DO_NOT_AUTHOR": a.get("last_price"),   # context only; scaffold owns prices
        "last_bar_utc": a.get("last_bar_utc"),
        "freshness": {"age_minutes": fr.get("age_minutes"), "market_state": fr.get("market_state"),
                      "stale": fr.get("stale"), "degraded": a.get("degraded")},
        "indicators_warm": (win.get("sma_warm_at_display_start") or {}),
        "trend": a.get("trend") or {},
        "momentum": {
            "rsi14_hourly": h.get("rsi14"), "rsi14_daily": d.get("rsi14"),
            "macd_cross_hourly": macd.get("cross"), "macd_hist": r(macd.get("hist")),
            "macd_hist_prev": r(macd.get("hist_prev")), "ema_cross_hourly": h.get("ema_cross"),
            "above_sma20_hourly": h.get("above_sma20"),
        },
        "daily_context": {
            "sma20": r(d.get("sma20")), "sma50": r(d.get("sma50")), "sma200": r(d.get("sma200")),
            "atr14": r(d.get("atr14")), "realized_vol_20d_pct": d.get("realized_vol_20d_pct"),
            "prior_session": d.get("prior_session"), "today_session": d.get("today_session"),
        },
        "stats_last_sessions": a.get("stats_last_sessions") or {},
        # Levels are CONTEXT for reasoning about proximity/structure — the brief must
        # describe them in words ("near the pivot", "below S1"), never quote the numbers.
        "levels_context_only_never_author": {
            "pivots_classic": {k: r(v) for k, v in piv.items()},
            "atr_day_bands": {k: r(v) for k, v in bands.items() if isinstance(v, (int, float))},
        },
        "related": related,
        "lookback": {"daily": f"{win.get('daily_display','')} shown / {win.get('daily_fetched','')} fetched",
                     "hourly": f"{win.get('hourly_display','')} shown / {win.get('hourly_fetched','')} fetched"},
    }


def summarize_research(pack):
    """Compact the research pack into a cited list the author turns into claims[]."""
    if not pack:
        return None
    items = []
    for it in (pack.get("items") or []):
        items.append({"category": it.get("category"), "headline": it.get("headline"),
                      "summary": (it.get("summary") or "")[:600],
                      "source_url": it.get("source_url") or it.get("url"),
                      "timestamp": it.get("timestamp"),
                      "source_quality": it.get("source_quality"),
                      "used_in_thesis": it.get("used_in_thesis")})
    return {"instrument": pack.get("instrument"), "generated_at_utc": pack.get("generated_at_utc"),
            "items": items, "source_gaps": pack.get("source_gaps") or []}


def summarize_social(pack):
    """Compact the optional social pack — aggregate signal only, never facts."""
    if not pack:
        return None
    agg = pack.get("aggregate") or {}
    return {"note": "MARKET CONVERSATION ONLY — never a factual source; may only LOWER conviction",
            "aggregate": {k: agg.get(k) for k in
                          ("sentiment", "dominant_themes", "crowding_risk",
                           "hype_risk", "contrarian_warning")}}


# =====================================================================
# Prompt construction
# =====================================================================

SYSTEM_PROMPT = """\
You are the senior research analyst for AssetFrame, a next-session market-intelligence \
service. You author the research BRIEF for one instrument: directional view, thesis, \
prediction intent, scenarios, sourced claims, risks and a verdict. Downstream Python \
(scaffold_payload.py) compiles every NUMBER and structure from your intent — levels, \
pivots, bands, R:R, the price ladder, the predictions file and the confidence score. \
You author INTENT; the engine authors PRICES.

ROLE AND LIMITS
- This is general market research and decision support, NOT regulated financial advice. \
Never tell anyone to buy or sell; never guarantee an outcome; never imply a risk-free or \
sure trade. Frame everything as research: Research view / Long-biased scenario / \
Short-biased scenario / Invalidation / No-trade condition / Stand-aside.
- The verdict is a conditional sentence ("If X confirms, expect Y"), never an instruction.

HARD RULES (a violation makes the brief unusable)
1. NEVER author prices, levels, pivots, bands, R:R ratios, ladders, position sizes or a \
confidence number. Describe structure in WORDS ("near the pivot", "below S1", "toward the \
outer lower band"). The market-context block gives you level numbers as READ-ONLY context \
for reasoning about proximity — do not copy them into any field.
2. NEVER fabricate prices, news, analyst ratings, financial metrics, dates, or sources. \
Every factual claim must trace to a real source. If you cannot source something, say so \
in source_gaps[] and do NOT let it drive the thesis.
3. BANNED LANGUAGE anywhere in the brief: "you should buy", "you should sell", "sure trade", \
"risk-free", "easy profit", "guaranteed" (the words "guaranteed"/"recommendation" are allowed \
ONLY in negated compliance form, e.g. "no outcome is guaranteed"). Keep the free_* fields \
PLAIN — no Pro vocabulary (no "R:R", "entry zone", "invalidation", "T1/T2", "ladder", \
"source audit", "outcome ledger", "hedging", "risk math").

CLAIM GATING (claims[] array — this is how facts enter the brief)
Each claim is {claim, status, source, used_in_thesis}. status is one of: \
confirmed, multiple-source, single-source, unverified, stale, unavailable.
- confirmed / multiple-source MAY drive the thesis (used_in_thesis: true).
- single-source MAY support but never CENTRE a thesis (use sparingly with used_in_thesis).
- unverified / stale / unavailable MUST NOT drive the thesis (used_in_thesis MUST be false).
- Every used_in_thesis claim MUST carry a real source URL. Never overstate — write \
"multiple-source reports of a draft agreement; signature unconfirmed", not "confirmed deal".
- Prefer claims whose source also appears in the supplied research pack; you may add \
freshly web-searched claims, but cite the real URL you found.

PREDICTION TAXONOMY (primary_prediction.type and alternative_prediction.type)
One of: breakout, rejection, continuation, mean_reversion, range_hold, volatility_expansion. \
Pick the strategic archetype that matches your thesis. directional_view is one of bullish, \
bearish, neutral, mixed. horizon is one of intraday, next_session, multi_session.

USING THE INPUTS
- Market context: the engine's trend / momentum / levels / freshness. Honour cold indicators \
and staleness (lower conviction, say so). Do not over-read.
- Memory pack: the ledger's realised hit rates, streaks, and lessons (no look-ahead). You MAY \
adjust conviction from history — e.g. "similar breakouts here recently underperformed → keep \
the thesis, cut conviction" — but never invent a number.
- Research pack: your sourced news. Social pack (if present): market-conversation sentiment \
only; may only LOWER conviction, never a fact, never raises confidence.

OUTPUT FORMAT
Return EXACTLY ONE JSON object — the complete brief — and NOTHING else (no prose, no markdown \
fences). It must match the schema and field names given in the user message precisely. Author \
real, specific, institutional-tone prose; no placeholders, no lorem, no "TBD"."""


def _schema_doc():
    """The exact schema, inlined into the user message so the model produces precisely
    what validate_brief() (and the scaffold) require. Enumerations are spelled out."""
    return f"""\
THE BRIEF SCHEMA — return a single JSON object with exactly these fields:

Identity / framing (all strings, all required):
  "name", "ticker", "instrument", "asset_class_label",
  "asset_class_key"   one of {list(ASSET_CLASS_KEYS)},
  "session_profile"   (e.g. "us_equity_rth","crypto_24_7","fx_spot","cme_futures"),
  "venue"

Stance (required):
  "status"            short phrase, e.g. "Wait" / "Long-biased" / "Short-biased",
  "risk"              one of {list(RISK_LEVELS)},
  "directional_view"  one of {list(DIRECTIONS)},
  "horizon"           one of {list(HORIZONS)},
  "market_regime"     free text (e.g. "trend_down"), normalized downstream,
  "primary_bias"      one sentence,
  "research_view"     one-line stance,
  "long_scenario_quality"  one of {list(QUALITY)},
  "short_scenario_quality" one of {list(QUALITY)}

Prediction intent (NO prices anywhere):
  "primary_prediction": {{
     "type"          one of {list(PREDICTION_TYPES)},
     "expected_move" words (no prices),
     "time_horizon" , "reasoning" ,
     "invalidators"  non-empty array of described (not priced) invalidation conditions
  }},
  "alternative_prediction": {{ "type" one of the taxonomy, "reasoning" }},
  "preferred_setup": {{ "side" one of {list(SETUP_SIDES)}, "why_this_setup", "avoid_if" }},
  "manual_prediction"  optional prose for a P6 manual check (no prices; the engine binds it)

Narrative + context:
  "exec_summary"      tight paragraph,
  "verdict": {{ "line" (conditional, not an instruction), "best", "risk", "stand_aside" }},
  "catalyst_status", "next_major_event", "cross_check"  (strings),
  "options_context_included"  boolean, "options_context_reason" string,
  "source_gaps"       array of strings,
  "asset_specific_stats_included" array of strings,
  "claims": [ {{ "claim", "status" one of {list(CLAIM_STATUSES)}, "source", "used_in_thesis" bool }} ],
  "catalysts": [ {{ "when","label","in_window" bool,"gap_risk","relevance" }} ],
  "risks": [ short prose strings ],
  "scenario_matrix": [ {{ "case","trigger","move","invalidation","confidence" (word band),"watch" }} ],
  "narrative": {{
     "free_bullets":   [ {{"label","text"}} ]  PLAIN language, no Pro vocab,
     "free_scenarios": [ {{"scenario","trigger","move","watch"}} ],
     "market_summary": [ {{"label","text"}} ]  labels: Technical thesis / Macro-catalyst thesis /
                        Cross-asset read / What would prove this wrong / Timing risk,
     "long_short_view" HTML <ul>...</ul> string,
     "technicals_note" prose (describe levels in words, no quoted prices),
     "stats_html"      an HTML <table> of SOURCED, timestamped stats
  }},
  "source_confidence": [ ["label","assessment"], ... ]   (optional but recommended)

Reminders: keep free_bullets / free_scenarios PLAIN. Describe levels in words. Every \
used_in_thesis claim needs a real source and a strong status. Output the JSON object ONLY."""


def build_user_message(ticker, analysis, memory_pack, research, social, guidance):
    """Assemble the compact context block + the schema + (optional) critic guidance."""
    ctx = {
        "instruction": (f"Author the AssetFrame research brief for {ticker}. Research the "
                        "current macro and asset-specific picture with web_search, then write "
                        "the brief. Cite real source URLs in claims[]. Author intent and prose "
                        "only — never prices, levels, R:R or a confidence number."),
        "market_context": summarize_analysis(analysis),
        "memory_pack": memory_pack,
        "research_pack": summarize_research(research),
        "social_pack": summarize_social(social),
    }
    parts = [
        "=== CONTEXT (read all of it before writing) ===",
        json.dumps(ctx, ensure_ascii=False, indent=1),
        "",
        "=== " + _schema_doc(),
    ]
    if guidance:
        parts += ["", "=== REVISION GUIDANCE — the adversarial critic flagged these issues; "
                  "fix every one while keeping the brief honest (do NOT pad confidence): ===",
                  guidance]
    return "\n".join(parts)


# =====================================================================
# Anthropic call + JSON extraction
# =====================================================================

def _require_sdk():
    try:
        import anthropic            # noqa: F401  (imported for side effect / clarity)
        return anthropic
    except ImportError:
        print("ERROR: the 'anthropic' SDK is not installed. Run: pip install anthropic",
              file=sys.stderr)
        sys.exit(3)


def _client(anthropic):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        # Clear, non-crashing error. run_daily catches the non-zero exit and falls
        # back to the operator-written ("needs_brief") path — a keyless run degrades.
        print("ERROR: ANTHROPIC_API_KEY not set — cannot author the brief. "
              "Set it in the environment, or supply an operator-written brief.",
              file=sys.stderr)
        sys.exit(3)
    return anthropic.Anthropic(api_key=key)


def _extract_json(blocks):
    """Pull the brief JSON object out of the response content blocks. The model is
    instructed to emit ONLY the JSON object; we still defensively slice from the first
    '{' to the last '}' across the concatenated text blocks (web_search interleaves
    tool blocks, so we join only the text)."""
    text = "".join(b.text for b in blocks if getattr(b, "type", None) == "text")
    if not text.strip():
        return None, "model returned no text content"
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return None, "no JSON object found in the model output"
    try:
        return json.loads(text[s:e + 1]), None
    except json.JSONDecodeError as ex:
        return None, f"could not parse JSON: {ex}"


def _usage_line(model, usage_in, usage_out, web_searches, attempts):
    cost = (usage_in / 1e6) * PRICE_IN_PER_MTOK + (usage_out / 1e6) * PRICE_OUT_PER_MTOK
    return (f"brief_writer: model={model} attempts={attempts} "
            f"in_tok={usage_in} out_tok={usage_out} web_searches={web_searches} "
            f"est_cost_usd=${cost:.4f}")


def author_brief(ticker, analysis, memory_pack, research, social, *, model,
                 max_tokens, guidance=None):
    """Call the model (with web_search), parse + validate, RE-PROMPT ONCE on a
    validation miss. Returns (brief, telemetry). Raises SystemExit on hard failure."""
    anthropic = _require_sdk()
    client = _client(anthropic)

    system = SYSTEM_PROMPT
    user = build_user_message(ticker, analysis, memory_pack, research, social, guidance)
    messages = [{"role": "user", "content": user}]
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]

    tot_in = tot_out = tot_web = 0
    last_err = None
    # attempt 1 = author; attempt 2 = repair with the validation errors fed back.
    for attempt in (1, 2):
        try:
            resp = client.messages.create(
                model=model, max_tokens=max_tokens, system=system,
                tools=tools, messages=messages,
            )
        except Exception as ex:                      # network / auth / rate limit
            print(f"ERROR: Anthropic API call failed: {type(ex).__name__}: {ex}",
                  file=sys.stderr)
            sys.exit(3)

        u = getattr(resp, "usage", None)
        tot_in += getattr(u, "input_tokens", 0) or 0
        tot_out += getattr(u, "output_tokens", 0) or 0
        srv = getattr(u, "server_tool_use", None)
        tot_web += getattr(srv, "web_search_requests", 0) or 0 if srv else 0

        brief, perr = _extract_json(resp.content)
        errs = [perr] if perr else validate_brief(brief)
        if errs and getattr(resp, "stop_reason", None) == "max_tokens":
            # Output hit the token ceiling, so the JSON was cut off. Name the real cause
            # instead of a downstream "could not parse JSON" guess (still re-prompts once).
            errs = [f"model hit max_tokens={max_tokens} (output truncated before the JSON "
                    f"closed); raise --max-tokens"] + errs
        telemetry = {"model": model, "input_tokens": tot_in, "output_tokens": tot_out,
                     "web_searches": tot_web, "attempts": attempt,
                     "est_cost_usd": round((tot_in / 1e6) * PRICE_IN_PER_MTOK
                                           + (tot_out / 1e6) * PRICE_OUT_PER_MTOK, 4)}
        if not errs:
            return brief, telemetry

        last_err = errs
        if attempt == 1:
            # Re-prompt once: keep the model's own draft in the thread and ask it to
            # return a corrected COMPLETE object addressing the listed errors.
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content":
                             "Your brief failed schema validation with these errors:\n- "
                             + "\n- ".join(errs)
                             + "\n\nReturn the COMPLETE corrected brief as a single JSON "
                             "object (no prose, no fences). Fix every error and keep all "
                             "other fields. Do not author prices or pad confidence."})

    # still invalid after the repair attempt
    print("ERROR: brief failed schema validation after re-prompt:\n  - "
          + "\n  - ".join(last_err or ["unknown error"]), file=sys.stderr)
    print(_usage_line(model, tot_in, tot_out, tot_web, 2), file=sys.stderr)
    sys.exit(2)


# =====================================================================
# CLI
# =====================================================================

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="brief_writer.py",
        description="Author an AssetFrame research brief with the Anthropic API "
                    "(web_search-enabled) and validate it against the brief schema.")
    p.add_argument("ticker", help="instrument ticker / NAME prefix (e.g. BTC, AAPL)")
    p.add_argument("--analysis", required=True, help="engine analysis JSON (data/analysis/<NAME>_analysis.json)")
    p.add_argument("--memory-pack", required=True, dest="memory_pack",
                   help="bounded memory pack JSON (data/memory_packs/<NAME>_memory_pack.json)")
    p.add_argument("--research", help="optional research pack JSON")
    p.add_argument("--social", help="optional social pack JSON")
    p.add_argument("--out", required=True, help="path to write the validated brief")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model id (default {DEFAULT_MODEL})")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, dest="max_tokens",
                   help=f"max output tokens (default {DEFAULT_MAX_TOKENS})")
    p.add_argument("--guidance", default=None,
                   help="extra authoring guidance (e.g. a critic's issues for a revise loop)")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    analysis = _load_json(args.analysis, required=True, what="analysis")
    memory_pack = _load_json(args.memory_pack, required=True, what="memory pack")
    research = _load_json(args.research, required=False, what="research pack")
    social = _load_json(args.social, required=False, what="social pack")

    brief, telemetry = author_brief(
        args.ticker, analysis, memory_pack, research, social,
        model=args.model, max_tokens=args.max_tokens, guidance=args.guidance)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(brief, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(_usage_line(telemetry["model"], telemetry["input_tokens"],
                      telemetry["output_tokens"], telemetry["web_searches"],
                      telemetry["attempts"]), file=sys.stderr)
    # one-line machine-readable summary on stdout (run_daily parses this for the manifest)
    print(json.dumps({"ok": True, "out": str(out), "ticker": args.ticker, **telemetry}))


if __name__ == "__main__":
    main()
