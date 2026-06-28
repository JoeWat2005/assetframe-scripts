"""Shared prediction taxonomy for AssetFrame.

One vocabulary that threads through the whole pipeline:
predictions -> ledger -> track record -> confidence -> calibration -> research memory.

Two distinct "type" concepts must NOT be confused:
  * PREDICTION TYPE (the archetype, defined here): the strategic shape of the
    call the analyst is making - breakout / rejection / continuation /
    mean_reversion / range_hold / volatility_expansion. One per report (it tags
    the primary prediction and the whole edition).
  * SCORING MECHANIC (in score_report.py: close_above, range_inside, touches,
    no_close_below, ...): how an individual falsifiable prediction P1..Pn is
    graded. Those stay where they are; this module does not touch them.

Stdlib-only, pure functions. Validators raise TaxonomyError on bad values so a
typo can never silently freeze into the append-only ledger.
"""

# --- canonical sets ---------------------------------------------------------

PREDICTION_TYPES = (
    "breakout", "rejection", "continuation",
    "mean_reversion", "range_hold", "volatility_expansion",
)

DIRECTIONS = ("bullish", "bearish", "neutral", "mixed")   # the analyst's directional_view
SETUP_SIDES = ("long", "short", "wait")                   # the preferred_setup side

HORIZONS = ("intraday", "next_session", "multi_session")

ASSET_CLASS_KEYS = ("equity", "crypto", "fx", "futures", "index", "commodity")

# Trend/structure regimes are derivable from the engine; high/low_volatility and
# breakout are analyst-set refinements (see derive_market_regime).
MARKET_REGIMES = (
    "trend_up", "trend_down", "range", "choppy",
    "high_volatility", "low_volatility", "breakout",
)

# Display bands for confidence (UI + push payloads). Distinct from the
# statistical calibration buckets below.
CONFIDENCE_BANDS = ("Low", "Moderate", "Elevated", "High")

# Confidence calibration buckets. KEEP IN SYNC with web/lib/content.ts
# computeCalibration and score_report.calibration / export_content (all three
# import confidence_bucket from here on the Python side; content.ts mirrors it).
CONFIDENCE_BUCKETS = ("<=60", "61-75", ">75")

# session profile (sessions.py PROFILES) -> base asset class
_PROFILE_ASSET_CLASS = {
    "us_equity_rth": "equity",
    "crypto_24_7": "crypto",
    "fx_spot": "fx",
    "cme_futures": "futures",
}

# Best-effort refinement of generic CME futures into index vs commodity.
_INDEX_FUTURES = ("ES", "NQ", "YM", "RTY", "FTSE", "DAX", "NKD", "FDAX", "STOXX")
_COMMODITY_FUTURES = ("CL", "WTI", "BRENT", "BZ", "NG", "GC", "SI", "HG", "PL",
                      "PA", "ZC", "ZW", "ZS", "ZL", "KC", "CT", "SB", "CC", "HO", "RB")

_REGIME_ALIASES = {
    "uptrend": "trend_up", "trend up": "trend_up", "bull": "trend_up",
    "downtrend": "trend_down", "trend down": "trend_down", "bear": "trend_down",
    "ranging": "range", "rangebound": "range", "range bound": "range",
    "consolidation": "range", "sideways": "range",
    "chop": "choppy", "choppy": "choppy", "whipsaw": "choppy",
    "high vol": "high_volatility", "high volatility": "high_volatility",
    "volatile": "high_volatility", "elevated vol": "high_volatility",
    "low vol": "low_volatility", "low volatility": "low_volatility",
    "calm": "low_volatility", "quiet": "low_volatility",
    "breakout": "breakout", "break out": "breakout", "expansion": "breakout",
}


# --- validation -------------------------------------------------------------

class TaxonomyError(ValueError):
    pass


def _check(value, allowed, field):
    if value not in allowed:
        raise TaxonomyError(f"{field}={value!r} is not one of {list(allowed)}")
    return value


def validate_prediction_type(v):
    return _check(v, PREDICTION_TYPES, "prediction_type")


def validate_direction(v):
    return _check(v, DIRECTIONS, "direction")


def validate_setup_side(v):
    return _check(v, SETUP_SIDES, "setup_side")


def validate_horizon(v):
    return _check(v, HORIZONS, "horizon")


def validate_asset_class(v):
    return _check(v, ASSET_CLASS_KEYS, "asset_class_key")


def validate_market_regime(v):
    return _check(v, MARKET_REGIMES, "market_regime")


# --- helpers ----------------------------------------------------------------

def asset_class_key(profile_key, symbol="", override=None):
    """Normalized asset class. The session profile is authoritative; an explicit
    override (from the brief) wins; symbol refines generic futures into
    index/commodity where the root is unambiguous."""
    if override:
        return validate_asset_class(override)
    base = _PROFILE_ASSET_CLASS.get(profile_key)
    if base is None:
        raise TaxonomyError(f"unknown session profile {profile_key!r}")
    if base == "futures" and symbol:
        root = symbol.upper().lstrip("^").split("=")[0].split("-")[0]
        if any(root.startswith(tok) for tok in _INDEX_FUTURES):
            return "index"
        if any(root.startswith(tok) for tok in _COMMODITY_FUTURES):
            return "commodity"
    return base


def derive_market_regime(analysis):
    """Data-driven baseline regime from the engine analysis JSON, so 'regime' is
    never pure narrative. Uses trend + structure only (volatility regimes are
    asset-relative and left to analyst override, since one absolute vol cutoff
    can't span equities and crypto). Returns one of MARKET_REGIMES."""
    trend = analysis.get("trend") or {}
    lt = (trend.get("long_term_daily") or "").lower()
    intraday = (trend.get("intraday_hourly") or "").lower()
    align = (trend.get("alignment") or "").lower()
    if "range" in align or "range" in intraday:
        return "range"
    if "uptrend" in lt and "mixed" not in align:
        return "trend_up"
    if "downtrend" in lt and "mixed" not in align:
        return "trend_down"
    return "choppy"


def normalize_market_regime(text, analysis=None):
    """Map an analyst's free-text regime onto MARKET_REGIMES, falling back to the
    data-derived baseline when it doesn't match a known label."""
    if text:
        t = text.strip().lower().replace("_", " ")
        if t.replace(" ", "_") in MARKET_REGIMES:
            return t.replace(" ", "_")
        for alias, canon in _REGIME_ALIASES.items():
            if alias in t:
                return canon
    return derive_market_regime(analysis or {})


def confidence_band(score):
    """0-100 score -> display band label (UI + push payloads)."""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "Unknown"
    if s < 50:
        return "Low"
    if s < 65:
        return "Moderate"
    if s < 80:
        return "Elevated"
    return "High"


def confidence_bucket(score):
    """0-100 score -> statistical calibration bucket, or None if unparseable.
    The single source of truth for the Python side; mirror in content.ts."""
    try:
        c = float(score)
    except (TypeError, ValueError):
        return None
    return "<=60" if c <= 60 else ("61-75" if c <= 75 else ">75")


def build_taxonomy(prediction_type, direction, horizon, asset_class, market_regime):
    """Validate + assemble the taxonomy block embedded in a predictions file and
    carried into the ledger and editions table."""
    return {
        "prediction_type": validate_prediction_type(prediction_type),
        "direction": validate_direction(direction),
        "horizon": validate_horizon(horizon),
        "asset_class": validate_asset_class(asset_class),
        "market_regime": validate_market_regime(market_regime),
    }



if __name__ == "__main__":
    import json
    sample = {"trend": {"long_term_daily": "Uptrend", "intraday_hourly": "Range",
                        "alignment": "mixed (intraday range)"}}
    demo = {
        "PREDICTION_TYPES": PREDICTION_TYPES,
        "DIRECTIONS": DIRECTIONS,
        "HORIZONS": HORIZONS,
        "ASSET_CLASS_KEYS": ASSET_CLASS_KEYS,
        "MARKET_REGIMES": MARKET_REGIMES,
        "asset_class(us_equity_rth, AAPL)": asset_class_key("us_equity_rth", "AAPL"),
        "asset_class(cme_futures, ES=F)": asset_class_key("cme_futures", "ES=F"),
        "asset_class(cme_futures, CL=F)": asset_class_key("cme_futures", "CL=F"),
        "derive_regime(sample)": derive_market_regime(sample),
        "normalize('consolidating', sample)": normalize_market_regime("consolidating", sample),
        "confidence_band(53)": confidence_band(53),
        "confidence_bucket(53)": confidence_bucket(53),
        "build_taxonomy(...)": build_taxonomy("range_hold", "neutral", "next_session",
                                              "equity", "range"),
    }
    print(json.dumps(demo, indent=1, default=str))
