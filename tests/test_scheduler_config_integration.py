"""Phase 2 INTEGRATION tests for scripts/scheduler/config (config_loader + validate_config).

Unlike test_scheduler_config_unit.py (which isolates each function on SYNTHETIC single-asset
inputs), this suite exercises the config directory WIRED TOGETHER with its REAL cross-subdir
dependencies and the REAL universe file:

  * config_loader.load_assets over the REAL config/assets.json -> normalized assets, cross-checked
    against the REAL taxonomy.ASSET_CLASS_KEYS and sessions.PROFILES enums it validates against.
  * validate_config.main() end-to-end on the REAL config (exit 0, drives _holiday_warnings ->
    calendar_rules) and on a hand-built broken universe (exit 2).
  * The CONSUMER side of the config's vocabulary: the normalized output is fed into the scheduler's
    session resolver (sessions.get_cadence_window / get_window) and the generation gate
    (calendar_rules.is_due), and the candle vocabulary is cross-checked against the marketdata
    module (intraday.SUPPORTED_INTERVALS). These assert the data CONTRACTS that only break when the
    modules combine (a cadence/interval/window value config emits that a downstream subdir can't
    consume = a silent drop, not a crash).

Stdlib + pytest only. No network / DB / subprocess. Reads the real config/assets.json read-only and
writes hand-built universes only under tmp_path. Mirrors the existing tests' import style.
"""
import datetime as dt
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_loader as CL          # scripts/scheduler/config
import validate_config as VC        # scripts/scheduler/config
import taxonomy                     # scripts/pipeline/scoring  (real dep of config_loader)
import sessions                     # scripts/scheduler/calendars (real dep + consumer)
import calendar_rules as CR         # scripts/scheduler/calendars (consumer of cadence/tz vocab)
import intraday                     # scripts/pipeline/marketdata (consumer of chart_intervals)

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = REPO_ROOT / "config" / "assets.json"
# A deterministic weekday noon (Mon 2026-06-29) so session/due resolution never depends on "now".
MON_NOON = dt.datetime(2026, 6, 29, 12, 0, tzinfo=dt.timezone.utc)


def _write(path, assets):
    path.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return path


def _run_main(path):
    """Invoke VC.main() with argv=[prog, path]; return (exit_code_or_None, stdout)."""
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["validate_config", str(path)]
    code = None
    try:
        with redirect_stdout(buf):
            try:
                VC.main()
            except SystemExit as se:
                code = se.code
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


@pytest.fixture(scope="module")
def real_assets():
    """The REAL universe, loaded+normalized through the REAL taxonomy/sessions validators."""
    assert REAL_CONFIG.exists(), f"real universe missing at {REAL_CONFIG}"
    return CL.load_assets(REAL_CONFIG)


# ===================================================================== real config + cross-module enums
def test_real_config_loads_and_is_nonempty(real_assets):
    assert len(real_assets) >= 1
    # every id is unique + lowercase (the loader's own contract, end-to-end on real data)
    ids = [a["id"] for a in real_assets]
    assert len(ids) == len(set(ids))
    assert all(i == i.lower() for i in ids)


def test_real_config_asset_classes_are_all_in_taxonomy(real_assets):
    # config_loader validates against taxonomy.ASSET_CLASS_KEYS; assert the REAL universe only
    # references classes the taxonomy actually knows (cross-module: config <-> scoring taxonomy).
    for a in real_assets:
        assert a["asset_class"] in taxonomy.ASSET_CLASS_KEYS, a["id"]


def test_real_config_session_profiles_are_all_resolvable(real_assets):
    # Every session_profile in the universe must resolve to a real sessions.PROFILES entry that
    # carries a 'type' the session-resolution logic branches on (config <-> scheduler calendars).
    for a in real_assets:
        prof = sessions.PROFILES.get(a["session_profile"])
        assert prof is not None, a["id"]
        assert "type" in prof, a["session_profile"]


def test_real_config_cadences_are_all_in_vocab(real_assets):
    for a in real_assets:
        assert a["cadence"] in CL.CADENCES, a["id"]


# ===================================================================== normalization matches schema
def test_real_config_normalization_fills_schema_defaults(real_assets):
    # Defaults documented in the config_loader schema, asserted end-to-end on the real universe.
    for a in real_assets:
        assert a["enabled"] is True
        assert a["publish_policy"] in CL.PUBLISH_POLICIES
        assert a["report_tier"] in CL.REPORT_TIERS
        # timeframes default to [forecast_window]; always a non-empty, dup-free list of valid windows
        tfs = a["timeframes"]
        assert isinstance(tfs, list) and tfs and len(set(tfs)) == len(tfs)
        assert all(t in CL.FORECAST_WINDOWS for t in tfs)
        # chart_intervals always lead with the canonical pair and are dup-free + valid
        civ = a["chart_intervals"]
        assert civ[:len(CL.CANONICAL_INTERVALS)] == list(CL.CANONICAL_INTERVALS)
        assert len(set(civ)) == len(civ)
        assert all(iv in CL.CHART_INTERVALS for iv in civ)
        # include_fundamentals defaults to equity-only
        assert a["include_fundamentals"] is (a["asset_class"] == "equity")


def test_real_config_default_timeframes_equal_forecast_window(real_assets):
    # The real universe sets no explicit `timeframes`, so each must normalize to exactly its window.
    for a in real_assets:
        assert a["timeframes"] == [a["forecast_window"]], a["id"]


def test_real_config_enabled_only_is_subset(real_assets):
    enabled = CL.load_assets(REAL_CONFIG, enabled_only=True)
    all_ids = {a["id"] for a in real_assets}
    en_ids = {a["id"] for a in enabled}
    assert en_ids <= all_ids
    assert en_ids == {a["id"] for a in real_assets if a["enabled"]}


def test_get_asset_roundtrips_real_config(real_assets):
    first = real_assets[0]["id"]
    got = CL.get_asset(first, REAL_CONFIG)
    assert got["id"] == first
    with pytest.raises(CL.ConfigError):
        CL.get_asset("definitely-not-an-asset", REAL_CONFIG)


# ===================================================================== validate_config end-to-end
def test_validate_config_main_exit0_on_real_config(real_assets):
    code, out = _run_main(REAL_CONFIG)
    assert code is None                                  # success: returns, no sys.exit
    assert out.startswith("OK:")
    n = len(real_assets)
    en = len([a for a in real_assets if a["enabled"]])
    assert f"{n} assets ({en} enabled)" in out
    # the printed table reads normalized fields under their canonical names; every id must appear
    for a in real_assets:
        assert a["id"] in out
        assert a["provider_symbols"]["yahoo"] in out


def test_validate_config_main_exit2_on_broken_universe(tmp_path):
    # Two independent, cross-validator problems aggregated into one INVALID exit.
    bad = {"id": "broken", "name": "B", "instrument": "B", "ticker": "B",
           "provider_symbols": {"yahoo": "B"}, "asset_class": "forex",   # not in taxonomy
           "session_profile": "fx_spot", "cadence": "hourly",            # not in CADENCES
           "timezone": "UTC"}
    code, out = _run_main(_write(tmp_path / "broken.json", [bad]))
    assert code == 2
    assert "INVALID" in out
    assert "asset_class" in out and "cadence" in out


def test_validate_config_main_exit2_on_missing_file(tmp_path):
    code, out = _run_main(tmp_path / "nope.json")
    assert code == 2
    assert "INVALID" in out


def test_validate_config_runs_holiday_check_for_real_equity(real_assets):
    # If the universe has an equity/index/commodity asset, validate_config drives
    # _holiday_warnings -> calendar_rules.computed_holidays for its tz calendar without raising.
    calendared = [a for a in real_assets
                  if a["asset_class"] not in ("fx", "crypto")
                  and CR._TZ_CALENDAR.get(a["timezone"])]
    if not calendared:
        pytest.skip("real universe has no exchange-calendar asset to exercise the holiday check")
    # the calendar each such asset needs must actually compute a non-empty holiday set this year
    year = dt.datetime.now(dt.timezone.utc).year
    for a in calendared:
        key = CR._TZ_CALENDAR[a["timezone"]]
        assert CR.computed_holidays(key, year), (a["id"], key)
    code, _ = _run_main(REAL_CONFIG)
    assert code is None


# ===================================================================== contract: chart_intervals <-> intraday
def test_chart_intervals_mirror_intraday_supported_intervals():
    # config_loader.CHART_INTERVALS is documented as a "mirror of intraday.SUPPORTED_INTERVALS".
    # Drift here means the engine would fetch an interval the config rejects (or vice-versa).
    assert CL.CHART_INTERVALS == intraday.SUPPORTED_INTERVALS


def test_canonical_intervals_are_supported_by_marketdata():
    assert set(CL.CANONICAL_INTERVALS) <= set(intraday.SUPPORTED_INTERVALS)


def test_real_config_chart_intervals_consumable_by_marketdata(real_assets):
    for a in real_assets:
        assert set(a["chart_intervals"]) <= set(intraday.SUPPORTED_INTERVALS), a["id"]


# ===================================================================== contract: cadence <-> scheduler gate
def test_cadence_vocab_matches_calendar_rules_consumer_set():
    # calendar_rules.is_due() understands _OPEN_CADENCES + {weekly, monthly}; any cadence config
    # accepts beyond that falls through to "unknown cadence" -> the asset would NEVER generate.
    consumer = set(CR._OPEN_CADENCES) | {"weekly", "monthly"}
    assert set(CL.CADENCES) == consumer


def test_real_config_every_asset_is_understood_by_due_gate(real_assets):
    # The config's cadence output must be consumable by the generation gate: a deterministic
    # (bool, reason) with reason NEVER the 'unknown cadence' fall-through.
    for a in real_assets:
        due, reason = CR.is_due(a, now=MON_NOON, holidays={})
        assert isinstance(due, bool)
        assert "unknown cadence" not in reason, (a["id"], reason)


def test_disabled_asset_is_filtered_and_gated(tmp_path):
    # An enabled=False asset is dropped by enabled_only AND reported disabled by the gate -
    # the two layers (config filter + scheduler gate) agree.
    on = {"id": "on", "name": "On", "instrument": "On", "ticker": "ON",
          "provider_symbols": {"yahoo": "ON"}, "asset_class": "crypto",
          "session_profile": "crypto_24_7", "cadence": "daily", "timezone": "UTC"}
    off = {**on, "id": "off", "ticker": "OFF", "enabled": False}
    p = _write(tmp_path / "mix.json", [on, off])
    assert {a["id"] for a in CL.load_assets(p, enabled_only=True)} == {"on"}
    off_norm = next(a for a in CL.load_assets(p) if a["id"] == "off")
    due, reason = CR.is_due(off_norm, now=MON_NOON, holidays={})
    assert due is False and "disabled" in reason


# ===================================================================== contract: forecast windows <-> session resolver
def test_real_config_windows_resolvable_end_to_end(real_assets):
    # The normalized cadence + each declared timeframe must drive the scheduler's session resolver
    # to a well-formed, non-degenerate window (config output -> sessions.get_cadence_window/get_window).
    for a in real_assets:
        cw = sessions.get_cadence_window(a["session_profile"], a["cadence"], now=MON_NOON)
        _assert_window_ok(cw, a["id"])
        for tf in a["timeframes"]:
            w = sessions.get_window(a["session_profile"], now=MON_NOON, forecast_window=tf)
            _assert_window_ok(w, f"{a['id']}:{tf}")


def _assert_window_ok(w, where):
    assert "window_start_utc" in w and "window_end_utc" in w, where
    s = dt.datetime.strptime(w["window_start_utc"], "%Y-%m-%d %H:%M")
    e = dt.datetime.strptime(w["window_end_utc"], "%Y-%m-%d %H:%M")
    assert e > s, (where, w["window_start_utc"], w["window_end_utc"])


# ===================================================================== hand-built universe round-trips through consumers
def test_handbuilt_universe_with_explicit_overrides_flows_through_consumers(tmp_path):
    # A realistic small universe that EXERCISES the non-default normalize paths (explicit
    # timeframes incl. the multi-session LONG_WINDOWS, explicit chart_intervals), then flows the
    # normalized result through the session resolver + due gate (config -> scheduler, end-to-end).
    eq = {"id": "msft", "name": "MSFT", "instrument": "Microsoft", "ticker": "MSFT",
          "provider_symbols": {"yahoo": "MSFT"}, "asset_class": "equity",
          "session_profile": "us_equity_rth", "cadence": "trading_day",
          "timezone": "America/New_York", "roll_utc": 0,
          "timeframes": ["next_regular_session", "next_week", "next_5_sessions"],
          "chart_intervals": ["4h", "1d"]}
    fx = {"id": "audusd", "name": "AUD/USD", "instrument": "Aussie", "ticker": "AUDUSD",
          "provider_symbols": {"yahoo": "AUDUSD=X"}, "asset_class": "fx",
          "session_profile": "fx_spot", "cadence": "weekly", "cadence_day": "wed",
          "timezone": "Europe/London", "roll_utc": 22, "forecast_window": "next_week"}
    assets = CL.load_assets(_write(tmp_path / "hand.json", [eq, fx]))
    a = {x["id"]: x for x in assets}

    # explicit timeframes preserved (deduped, order-preserving); all valid windows
    assert a["msft"]["timeframes"] == ["next_regular_session", "next_week", "next_5_sessions"]
    # canonical pair force-prepended ahead of the user's intervals, deduped
    assert a["msft"]["chart_intervals"] == ["60m", "1d", "4h"]
    assert a["msft"]["include_fundamentals"] is True          # equity -> fundamentals on
    assert a["audusd"]["include_fundamentals"] is False        # fx -> fundamentals off
    assert a["audusd"]["timeframes"] == ["next_week"]          # default == its forecast_window

    # the normalized output is consumable by both downstream subdirs
    for x in assets:
        for tf in x["timeframes"]:
            _assert_window_ok(
                sessions.get_window(x["session_profile"], now=MON_NOON, forecast_window=tf),
                f"{x['id']}:{tf}")
        due, reason = CR.is_due(x, now=MON_NOON, holidays={})
        assert isinstance(due, bool)
        assert "unknown cadence" not in reason, (x["id"], reason)


def test_handbuilt_weekly_cadence_day_honoured_by_gate(tmp_path):
    # cadence_day is validated by config_loader and READ by calendar_rules._cadence_weekday;
    # assert the two agree end-to-end: a 'wed' weekly asset is due on Wed, not on Mon.
    wed = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)   # a Wednesday
    mon = MON_NOON                                                  # a Monday
    fx = {"id": "audusd", "name": "AUD/USD", "instrument": "Aussie", "ticker": "AUDUSD",
          "provider_symbols": {"yahoo": "AUDUSD=X"}, "asset_class": "fx",
          "session_profile": "fx_spot", "cadence": "weekly", "cadence_day": "wed",
          "timezone": "Europe/London"}
    asset = CL.load_assets(_write(tmp_path / "wk.json", [fx]))[0]
    assert asset["cadence_day"] == "wed"                            # preserved through normalize
    assert CR.is_due(asset, now=wed, holidays={})[0] is True
    assert CR.is_due(asset, now=mon, holidays={})[0] is False
