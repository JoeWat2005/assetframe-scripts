"""The research-brief schema contract: the enum vocabulary + validate_brief().

Extracted from brief_writer so the validator can be imported WITHOUT pulling in the Anthropic SDK
client setup (brief_batch + critic + the tests use it). validate_brief enforces the SAME contract
scaffold_payload consumes, so a brief that passes here won't be rejected downstream for a structural
reason — it validates AUTHORSHIP, not the engine's price/level binding."""

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
