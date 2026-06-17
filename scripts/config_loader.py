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
  timezone          IANA tz (zoneinfo-resolvable)           (required)
  roll_utc          int 0..23 (intraday --roll-utc)         (default 0)
  related           str comma list for intraday --related   (default "")
  forecast_window   FORECAST_WINDOWS                        (default "next_session")
  publish_policy    PUBLISH_POLICIES                        (default "approval_required")
  report_tier       REPORT_TIERS                            (default "official")
  enabled           bool                                    (default true)

Usage:
  from config_loader import load_assets, get_asset, ConfigError
  assets = load_assets()                 # all, validated
  assets = load_assets(enabled_only=True)
"""
import json
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
CADENCES = ("daily", "weekday", "trading_day", "weekday_or_market_open")
PUBLISH_POLICIES = ("approval_required", "auto")
REPORT_TIERS = ("official", "watchlist", "staged", "backtest")
FORECAST_WINDOWS = ("next_liquid_session", "next_regular_session", "rolling_24h", "next_session")
REQUIRED = ("id", "name", "instrument", "ticker", "provider_symbols", "asset_class",
            "session_profile", "cadence", "timezone")


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
    pp = a.get("publish_policy", "approval_required")
    if pp not in PUBLISH_POLICIES:
        errs.append(f"{where}: publish_policy '{pp}' not in {list(PUBLISH_POLICIES)}")
    rt = a.get("report_tier", "official")
    if rt not in REPORT_TIERS:
        errs.append(f"{where}: report_tier '{rt}' not in {list(REPORT_TIERS)}")
    fw = a.get("forecast_window")
    if fw and fw not in FORECAST_WINDOWS:
        errs.append(f"{where}: forecast_window '{fw}' not in {list(FORECAST_WINDOWS)}")
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
