"""Asset universe loader + validator for the AssetFrame scheduler.

config/assets.json is the single source of truth for WHAT the engine can run.
Zero extra dependencies (stdlib json only) so it runs anywhere the engine runs.
Validators reuse taxonomy.ASSET_CLASS_KEYS and sessions.PROFILES, so the universe
can never reference an asset class the taxonomy doesn't know or a session profile
sessions.py can't resolve. load_assets() raises ConfigError listing EVERY problem
at once, BEFORE any run touches the network.

Schema (per asset):
  id                str, unique, lowercase                  (required)
  name              str display name                        (required)
  instrument        str full instrument name                (required)
  ticker            str report ticker (-> report_id slug)   (required)
  provider_symbols  {"yahoo": str, "eodhd"?: str}           (required: yahoo)
  asset_class       taxonomy.ASSET_CLASS_KEYS               (required)
  session_profile   sessions.PROFILES key                   (required)
  cadence           CADENCES                                (required)
  cadence_day       int 0-6 (Mon=0) or weekday name         (optional; only 'weekly')
  timezone          IANA tz (zoneinfo-resolvable)           (required)
  roll_utc          int 0..23 (intraday --roll-utc)         (default 0)
  related           str comma list for intraday --related   (default "")
  forecast_window   FORECAST_WINDOWS                        (default "next_session")
  timeframes        list[FORECAST_WINDOWS], one prediction   (optional; default [forecast_window])
                    track per entry — the multi-timeframe set
  chart_intervals   list[CHART_INTERVALS] candle intervals    (optional; default ["60m","1d"])
                    the engine analyses (60m/2h/4h/8h/1d/1week/1month) — the charts the
                    directional view is built FROM (distinct from timeframes/forecast windows)
  include_fundamentals bool                                  (optional; default: equities only)
  include_news      bool                                    (optional; default true)
  fundamentals_source  auto | twelvedata | none             (optional; default "auto")
  publish_policy    PUBLISH_POLICIES                        (default "approval_required")
  report_tier       REPORT_TIERS                            (default "official")
  enabled           bool                                    (default true)

Usage:
  from config_loader import load_assets, get_asset, ConfigError
  assets = load_assets()                 # all, validated
  assets = load_assets(enabled_only=True)
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import ASSET_CLASS_KEYS

try:
    from sessions import PROFILES as _SP
    SESSION_KEYS = tuple(_SP)
except Exception:                       # standalone fallback
    SESSION_KEYS = ("cme_futures", "fx_spot", "us_equity_rth", "crypto_24_7")

try:
    from zoneinfo import ZoneInfo
except Exception:                       # pragma: no cover
    ZoneInfo = None

DEFAULT_CONFIG = Path("config/assets.json")
# Windows ships no IANA tz DB, so zoneinfo.ZoneInfo() raises on perfectly valid zones
# unless the `tzdata` package is installed (the engine's other modules fall back to a
# fixed offset for the same reason). We accept a zone if zoneinfo resolves it OR it's in
# this curated allowlist of zones we actually use — so the universe still validates on a
# bare Windows box, while a genuine typo ("Mars/Phobos") is still rejected.
KNOWN_TZ = {"UTC", "Europe/London", "Europe/Zurich", "Europe/Frankfurt", "Europe/Paris",
            "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Toronto",
            "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore", "Australia/Sydney"}
# Scheduling vocabulary (distinct from taxonomy's prediction vocabulary).
CADENCES = ("daily", "weekday", "trading_day", "weekday_or_market_open", "weekly", "monthly")
_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PUBLISH_POLICIES = ("approval_required", "auto")
REPORT_TIERS = ("official", "watchlist", "staged", "backtest")
FORECAST_WINDOWS = ("next_liquid_session", "next_regular_session", "rolling_24h", "next_session",
                    "next_week", "next_5_sessions")
# Candle intervals the engine fetches + analyses (mirror of intraday.SUPPORTED_INTERVALS).
# Distinct from FORECAST_WINDOWS: these are the charts the view is built FROM, not when a
# prediction scores. 2h/4h/8h are resampled from 60m; 1week/1month from daily.
CHART_INTERVALS = ("60m", "2h", "4h", "8h", "1d", "1week", "1month")
CANONICAL_INTERVALS = ("60m", "1d")
REQUIRED = ("id", "name", "instrument", "ticker", "provider_symbols", "asset_class",
            "session_profile", "cadence", "timezone")

# --- single runtime config file (config/engine.json) ------------------------
# One place for operator-tunable RUNTIME settings, replacing the scatter of ASSETFRAME_*/ADVISOR_*
# knobs that used to live in .env. `.env` is now reserved for SECRETS only (DATABASE_URL, R2_*,
# *_API_KEY, UPSTASH_*). The asset UNIVERSE stays in config/assets.json (synced from Neon) — this
# file is settings only. Stored keyed by the legacy env-var name so every existing os.environ.get()
# read works unchanged: apply_runtime_env() seeds the environment from this file WITHOUT overriding
# anything already set, so the real environment (systemd EnvironmentFile, a test, an explicit export)
# always WINS. The dashboard's set_config command writes here (engine_ops._cmd_set_config).
DEFAULT_ENGINE_CONFIG = Path("config/engine.json")
RUNTIME_DEFAULTS = {
    "ADVISOR_DATA_PROVIDER": "yahoo",
    "ASSETFRAME_DATA_LICENSE": "personal",  # personal | commercial. commercial -> only commercially
                                            # licensed feeds back a published report; a free-tier
                                            # fallback flags the edition license_degraded (one-knob flip).
    "ASSETFRAME_BRIEF_MODEL": "claude-sonnet-4-6",
    "ASSETFRAME_CRITIC_MODEL": "claude-haiku-4-5-20251001",  # adversarial review = cheap/fast Haiku
    "ASSETFRAME_AUTHOR_BRIEFS": "1",
    "ASSETFRAME_BRIEF_BATCH": "0",         # 1 = author/critique via Message Batches (no rate limit,
                                           # 50% cheaper, scales to N assets). Sync path is the fallback.
    "ASSETFRAME_BRIEF_CONCURRENCY": "1",   # briefs authored at once (1 = safe on Anthropic Tier 1)
    "ASSETFRAME_BRIEF_WEB_MAX_USES": "6",  # web searches per news-on brief (input-cost dial; was 8)
    "ASSETFRAME_RETENTION_DAYS": "14",
    "ASSETFRAME_RUN_TIMEOUT": "5400",
    "TWELVEDATA_RATE_PER_MIN": "8",
}


def load_runtime_config(path=DEFAULT_ENGINE_CONFIG):
    """Return the runtime settings: built-in defaults overlaid with config/engine.json (if present).
    Never raises — a missing/corrupt file just yields the defaults."""
    cfg = dict(RUNTIME_DEFAULTS)
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if not str(k).startswith("_") and v is not None:
                        cfg[str(k)] = v
        except Exception:
            pass
    return cfg


# NOTE: the canonical apply_runtime_env() lives further down (near RUNTIME_CONFIG). It seeds only
# the allow-listed keys PRESENT in engine.json (env wins) and is what every entrypoint calls. An
# earlier duplicate definition here was dead code (shadowed at import) and a refactor trap, so it
# was removed. load_runtime_config() above is still used (tests + diagnostics).


class ConfigError(ValueError):
    pass


def _validate_one(a, idx, seen_ids):
    errs = []
    aid = a.get("id")
    where = f"asset[{idx}]" + (f" '{aid}'" if aid else "")
    for f in REQUIRED:
        if not a.get(f):
            errs.append(f"{where}: missing required field '{f}'")
    if aid:
        if aid in seen_ids:
            errs.append(f"{where}: duplicate id '{aid}'")
        seen_ids.add(aid)
        if aid != str(aid).lower():
            errs.append(f"{where}: id must be lowercase")
    ps = a.get("provider_symbols") or {}
    if not isinstance(ps, dict) or not ps.get("yahoo"):
        errs.append(f"{where}: provider_symbols.yahoo is required (the engine price feed)")
    if a.get("asset_class") and a["asset_class"] not in ASSET_CLASS_KEYS:
        errs.append(f"{where}: asset_class '{a['asset_class']}' not in {list(ASSET_CLASS_KEYS)}")
    if a.get("session_profile") and a["session_profile"] not in SESSION_KEYS:
        errs.append(f"{where}: session_profile '{a['session_profile']}' not in {list(SESSION_KEYS)}")
    if a.get("cadence") and a["cadence"] not in CADENCES:
        errs.append(f"{where}: cadence '{a['cadence']}' not in {list(CADENCES)}")
    cd = a.get("cadence_day")
    if cd is not None and not isinstance(cd, bool):
        ok_cd = (isinstance(cd, int) and 0 <= cd <= 6) or \
                (isinstance(cd, str) and cd.strip().lower()[:3] in _WEEKDAY_NAMES)
        if not ok_cd:
            errs.append(f"{where}: cadence_day '{cd}' must be int 0-6 (Mon=0) or a weekday name")
    elif isinstance(cd, bool):
        errs.append(f"{where}: cadence_day must be int 0-6 or a weekday name, not a bool")
    pp = a.get("publish_policy", "approval_required")
    if pp not in PUBLISH_POLICIES:
        errs.append(f"{where}: publish_policy '{pp}' not in {list(PUBLISH_POLICIES)}")
    rt = a.get("report_tier", "official")
    if rt not in REPORT_TIERS:
        errs.append(f"{where}: report_tier '{rt}' not in {list(REPORT_TIERS)}")
    fw = a.get("forecast_window")
    if fw and fw not in FORECAST_WINDOWS:
        errs.append(f"{where}: forecast_window '{fw}' not in {list(FORECAST_WINDOWS)}")
    tfs = a.get("timeframes")
    if tfs is not None:
        if not isinstance(tfs, list) or not tfs:
            errs.append(f"{where}: timeframes must be a non-empty list of forecast windows")
        else:
            for tf in tfs:
                if tf not in FORECAST_WINDOWS:
                    errs.append(f"{where}: timeframe '{tf}' not in {list(FORECAST_WINDOWS)}")
            if len(set(tfs)) != len(tfs):
                errs.append(f"{where}: timeframes has duplicate entries {tfs}")
    civ = a.get("chart_intervals")
    if civ is not None:
        if not isinstance(civ, list) or not civ:
            errs.append(f"{where}: chart_intervals must be a non-empty list of candle intervals")
        else:
            for iv in civ:
                if iv not in CHART_INTERVALS:
                    errs.append(f"{where}: chart_interval '{iv}' not in {list(CHART_INTERVALS)}")
            if len(set(civ)) != len(civ):
                errs.append(f"{where}: chart_intervals has duplicate entries {civ}")
    for flag in ("include_fundamentals", "include_news"):
        v = a.get(flag)
        if v is not None and not isinstance(v, bool):
            errs.append(f"{where}: {flag} must be a boolean")
    fsrc = a.get("fundamentals_source")
    if fsrc is not None and fsrc not in ("auto", "twelvedata", "none"):
        errs.append(f"{where}: fundamentals_source '{fsrc}' must be auto|twelvedata|none")
    ru = a.get("roll_utc", 0)
    if not isinstance(ru, int) or isinstance(ru, bool) or not (0 <= ru <= 23):
        errs.append(f"{where}: roll_utc must be an int 0..23")
    tz = a.get("timezone")
    if tz:
        ok = tz in KNOWN_TZ
        if not ok and ZoneInfo is not None:
            try:
                ZoneInfo(tz)
                ok = True
            except Exception:
                ok = False
        if not ok:
            errs.append(f"{where}: timezone '{tz}' not recognised "
                        f"(use a valid IANA zone, or add it to config_loader.KNOWN_TZ)")
    return errs


def _normalize(a):
    a = dict(a)
    a.setdefault("enabled", True)
    a.setdefault("roll_utc", 0)
    a.setdefault("related", "")
    a.setdefault("publish_policy", "approval_required")
    a.setdefault("report_tier", "official")
    a.setdefault("forecast_window", "next_session")
    # multi-timeframe: one prediction track per entry. Default = the single forecast_window
    # (backward-compatible — one track), so existing assets are unchanged.
    tfs = a.get("timeframes") or [a["forecast_window"]]
    seen = set()
    a["timeframes"] = [t for t in tfs if not (t in seen or seen.add(t))]
    # chart_intervals: which candle intervals the engine analyses. Default = the canonical
    # 60m + 1d pair; always force-include the pair so the pipeline's inputs are guaranteed.
    civ = a.get("chart_intervals") or list(CANONICAL_INTERVALS)
    seen_iv = set()
    a["chart_intervals"] = [iv for iv in list(CANONICAL_INTERVALS) + civ
                            if not (iv in seen_iv or seen_iv.add(iv))]
    a.setdefault("include_fundamentals", a.get("asset_class") == "equity")
    a.setdefault("include_news", True)
    a.setdefault("fundamentals_source", "auto")
    return a


def load_assets(path=DEFAULT_CONFIG, enabled_only=False):
    """Load + validate the universe. Raises ConfigError aggregating ALL problems."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"asset universe not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"invalid JSON in {path}: {e}")
    assets = raw.get("assets") if isinstance(raw, dict) else raw
    if not isinstance(assets, list) or not assets:
        raise ConfigError(f"{path}: expected a non-empty 'assets' list")
    errs, seen = [], set()
    for i, a in enumerate(assets):
        if not isinstance(a, dict):
            errs.append(f"asset[{i}] is not an object")
            continue
        errs += _validate_one(a, i, seen)
    if errs:
        raise ConfigError("asset universe invalid:\n  - " + "\n  - ".join(errs))
    out = [_normalize(a) for a in assets]
    return [a for a in out if a["enabled"]] if enabled_only else out


def get_asset(asset_id, path=DEFAULT_CONFIG):
    for a in load_assets(path):
        if a["id"] == asset_id:
            return a
    raise ConfigError(f"asset id '{asset_id}' not found in {path}")


# --- single runtime config file --------------------------------------------
# config/engine.json holds the NON-SECRET runtime knobs (retention, brief authoring, run timeout,
# brief model, data provider). Secrets (DATABASE_URL, R2_*, *_API_KEY, UPSTASH_*) stay in .env. The
# JSON keys ARE the env var names the engine already reads, so applying the file just fills os.environ
# for any key not already set — env always wins, so systemd EnvironmentFile / ad-hoc overrides keep
# precedence and no read site changes. The admin "set config" command writes this same file.
RUNTIME_CONFIG = DEFAULT_ENGINE_CONFIG          # same file; one source of truth (was a duplicate literal)
# Allow-list of runtime knobs apply_runtime_env() seeds from engine.json. DERIVED from
# RUNTIME_DEFAULTS so every default is automatically settable from the file — a hardcoded subset
# silently dropped newer knobs (ASSETFRAME_BRIEF_BATCH / _CRITIC_MODEL / _BRIEF_CONCURRENCY were set
# in engine.json but never reached os.environ, so batch mode never engaged). Single source of truth.
SETTABLE_RUNTIME_KEYS = tuple(RUNTIME_DEFAULTS)
# Default feed per license mode (only used when ADVISOR_DATA_PROVIDER is not explicitly pinned).
_LICENSE_DEFAULT_PROVIDER = {"personal": "yahoo", "commercial": "twelvedata"}


def apply_runtime_env(path=RUNTIME_CONFIG):
    """Seed os.environ from config/engine.json for any allow-listed runtime knob not already set.
    Best-effort: a missing/invalid file is a silent no-op (the read sites keep their built-in
    defaults). Returns the dict of values applied. Call once at entrypoint startup."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    applied = {}
    for k in SETTABLE_RUNTIME_KEYS:
        if k in raw and raw[k] not in (None, ""):
            os.environ.setdefault(k, str(raw[k]))
            applied[k] = str(raw[k])
    # Data-license mode picks the DEFAULT feed so flipping to commercial is one knob. An explicit
    # ADVISOR_DATA_PROVIDER (env or engine.json, already setdefault-ed above) still wins, and env
    # always wins over both — preserving the env-first contract.
    if "ADVISOR_DATA_PROVIDER" not in os.environ:
        lic = os.environ.get("ASSETFRAME_DATA_LICENSE", RUNTIME_DEFAULTS["ASSETFRAME_DATA_LICENSE"])
        os.environ["ADVISOR_DATA_PROVIDER"] = _LICENSE_DEFAULT_PROVIDER.get(lic, "yahoo")
        applied["ADVISOR_DATA_PROVIDER"] = os.environ["ADVISOR_DATA_PROVIDER"]
    return applied
