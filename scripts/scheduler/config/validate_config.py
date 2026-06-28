"""Validate config/assets.json and print the universe. Exit 2 on any error, 0 on OK.

Usage:
  python scripts/validate_config.py [config/assets.json]
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config_loader import load_assets, ConfigError, DEFAULT_CONFIG


def _holiday_warnings(assets):
    """Holidays are COMPUTED for any year (calendar_rules.computed_holidays), so the calendar
    never lapses and needs no annual maintenance. This is just a sanity check that the
    computation is producing dates for the calendars enabled assets actually use."""
    try:
        import calendar_rules as CR
    except Exception:
        return
    needed = set()
    for a in assets:
        if a.get("enabled", True) and a.get("asset_class") not in ("fx", "crypto"):
            key = CR._TZ_CALENDAR.get(a.get("timezone"))
            if key:
                needed.add(key)
    this_year = datetime.now(timezone.utc).year
    for key in sorted(needed):
        if not CR.computed_holidays(key, this_year):
            print(f"WARNING: computed holiday set for '{key}' {this_year} is empty "
                  f"(unexpected — check calendar_rules.computed_holidays)", file=sys.stderr)


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    try:
        assets = load_assets(path)
    except ConfigError as e:
        print(f"INVALID: {e}")
        sys.exit(2)
    _holiday_warnings(assets)
    enabled = [a for a in assets if a["enabled"]]
    print(f"OK: {len(assets)} assets ({len(enabled)} enabled) in {path}\n")
    hdr = f"{'id':10} {'class':10} {'session':14} {'cadence':24} {'publish':18} {'yahoo':10} en"
    print(hdr)
    print("-" * len(hdr))
    for a in assets:
        print(f"{a['id']:10} {a['asset_class']:10} {a['session_profile']:14} "
              f"{a['cadence']:24} {a['publish_policy']:18} {a['provider_symbols']['yahoo']:10} "
              f"{'Y' if a['enabled'] else 'n'}")


if __name__ == "__main__":
    main()
