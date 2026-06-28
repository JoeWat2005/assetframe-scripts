"""Offline unit tests for scripts/scheduler/config: config_loader + validate_config.

These target the GAPS left by the existing suite (test_scheduler, test_engine_config,
test_data_license, test_chart_intervals, test_horizon_calibration), which between them already
cover: a happy load, duplicate-id, bad asset_class, missing-yahoo, bad cadence+tz, the
chart_intervals/timeframes normalize+validate, and the apply_runtime_env env-wins / license-default
paths. Here we exercise the UNCOVERED branches: every load_assets file-level error, get_asset, the
remaining _validate_one field validators (session/publish/tier/forecast/roll_utc/cadence_day/flags/
fundamentals_source + the duplicate-list rejections), the full _normalize default set, the
load_runtime_config / apply_runtime_env edge cases (None/empty/corrupt/non-dict/unknown-license), and
validate_config.main()'s exit codes.

Stdlib + pytest only; no network / DB / subprocess. apply_runtime_env mutates os.environ, so the
`clean_env` fixture snapshots and restores every key it can touch.
"""
import io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_loader as CL
import validate_config as VC

import pytest


# A minimal universe entry that passes validation (mirrors the existing suite's VALID).
VALID = {"id": "x", "name": "X", "instrument": "X", "ticker": "X",
         "provider_symbols": {"yahoo": "X=X"}, "asset_class": "fx",
         "session_profile": "fx_spot", "cadence": "weekday", "timezone": "UTC"}


def _write(path, assets, key="assets"):
    """Write a universe file ({assets: [...]} by default) and return the path."""
    path.write_text(json.dumps({key: assets}) if key else json.dumps(assets), encoding="utf-8")
    return path


def _errs(**over):
    """Validate a single asset built from VALID + overrides; return the error list."""
    return CL._validate_one({**VALID, **over}, 0, set())


@pytest.fixture
def clean_env():
    """Snapshot + restore every env var apply_runtime_env may read or write."""
    keys = set(CL.SETTABLE_RUNTIME_KEYS) | {"ADVISOR_DATA_PROVIDER"}
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ===================================================================== load_assets: file-level
def test_load_assets_missing_file_raises(tmp_path):
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(tmp_path / "nope.json")
    assert "not found" in str(ei.value)


def test_load_assets_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(p)
    assert "invalid JSON" in str(ei.value)


def test_load_assets_empty_list_raises(tmp_path):
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(_write(tmp_path / "e.json", []))
    assert "non-empty" in str(ei.value)


def test_load_assets_assets_not_a_list_raises(tmp_path):
    # {"assets": {}} -> not a list -> the non-empty-list guard fires
    p = tmp_path / "nl.json"
    p.write_text(json.dumps({"assets": {}}), encoding="utf-8")
    with pytest.raises(CL.ConfigError):
        CL.load_assets(p)


def test_load_assets_accepts_bare_top_level_list(tmp_path):
    # raw may be a bare list (not wrapped in {"assets": ...})
    p = tmp_path / "bl.json"
    p.write_text(json.dumps([VALID]), encoding="utf-8")
    assert len(CL.load_assets(p)) == 1


def test_load_assets_non_dict_element_reported(tmp_path):
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(_write(tmp_path / "nde.json", [123]))
    assert "is not an object" in str(ei.value)


def test_load_assets_aggregates_all_problems(tmp_path):
    # one asset with three independent problems -> all reported in one raise
    bad = {**VALID, "id": "a", "asset_class": "forex", "cadence": "hourly", "session_profile": "nope"}
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(_write(tmp_path / "agg.json", [bad]))
    msg = str(ei.value)
    assert "asset_class" in msg and "cadence" in msg and "session_profile" in msg
    assert msg.count("\n  - ") >= 3          # aggregated bullet list


def test_load_assets_enabled_only_filters_disabled(tmp_path):
    p = _write(tmp_path / "eo.json", [VALID, {**VALID, "id": "y", "enabled": False}])
    assert len(CL.load_assets(p)) == 2
    assert [a["id"] for a in CL.load_assets(p, enabled_only=True)] == ["x"]


def test_load_assets_rejects_uppercase_id(tmp_path):
    with pytest.raises(CL.ConfigError) as ei:
        CL.load_assets(_write(tmp_path / "u.json", [{**VALID, "id": "ABC"}]))
    assert "lowercase" in str(ei.value)


def test_load_assets_tolerates_utf8_bom(tmp_path):
    # config files are read utf-8-sig; a leading BOM must not break parsing
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"assets": [VALID]}).encode("utf-8"))
    assert len(CL.load_assets(p)) == 1


def test_load_assets_known_tz_allowlisted(tmp_path):
    # Europe/London is in KNOWN_TZ -> validates even on a box without the IANA tz DB
    assert "Europe/London" in CL.KNOWN_TZ
    p = _write(tmp_path / "k.json", [{**VALID, "timezone": "Europe/London"}])
    assert len(CL.load_assets(p)) == 1


def test_load_assets_normalizes_defaults_endtoend(tmp_path):
    # the public loader returns NORMALIZED assets (defaults filled in)
    a = CL.load_assets(_write(tmp_path / "n.json", [VALID]))[0]
    assert a["enabled"] is True
    assert a["publish_policy"] == "approval_required"
    assert a["report_tier"] == "official"
    assert a["timeframes"] == ["next_session"]
    assert a["chart_intervals"] == ["60m", "1d"]


# ===================================================================== _validate_one: field branches
def test_validate_one_lists_every_missing_required_field():
    errs = CL._validate_one({}, 3, set())
    for f in CL.REQUIRED:
        assert any(f"missing required field '{f}'" in e for e in errs)


def test_validate_one_bad_session_profile():
    assert any("session_profile 'nope'" in e for e in _errs(session_profile="nope"))


def test_validate_one_bad_publish_policy():
    assert any("publish_policy 'nope'" in e for e in _errs(publish_policy="nope"))


def test_validate_one_bad_report_tier():
    assert any("report_tier 'nope'" in e for e in _errs(report_tier="nope"))


def test_validate_one_bad_forecast_window():
    assert any("forecast_window 'nope'" in e for e in _errs(forecast_window="nope"))


def test_validate_one_bad_fundamentals_source():
    assert any("fundamentals_source 'nope'" in e for e in _errs(fundamentals_source="nope"))


@pytest.mark.parametrize("flag", ["include_fundamentals", "include_news"])
def test_validate_one_fetch_flags_must_be_bool(flag):
    assert any(f"{flag} must be a boolean" in e for e in _errs(**{flag: "yes"}))


@pytest.mark.parametrize("bad", [24, -1, "0", 23.5])
def test_validate_one_roll_utc_out_of_range_or_wrong_type(bad):
    assert any("roll_utc must be an int" in e for e in _errs(roll_utc=bad))


def test_validate_one_roll_utc_bool_rejected():
    # True is an int subclass but must be rejected explicitly
    assert any("roll_utc must be an int" in e for e in _errs(roll_utc=True))


@pytest.mark.parametrize("ru", [0, 23])
def test_validate_one_roll_utc_boundaries_ok(ru):
    assert not any("roll_utc" in e for e in _errs(roll_utc=ru))


@pytest.mark.parametrize("cd", [0, 6, "mon", "Sunday"])
def test_validate_one_cadence_day_valid(cd):
    assert not any("cadence_day" in e for e in _errs(cadence_day=cd))


@pytest.mark.parametrize("cd", [7, -1, "notaday"])
def test_validate_one_cadence_day_invalid(cd):
    assert any("cadence_day" in e for e in _errs(cadence_day=cd))


def test_validate_one_cadence_day_bool_rejected():
    assert any("not a bool" in e for e in _errs(cadence_day=True))


def test_validate_one_timeframes_must_be_nonempty_list():
    assert any("non-empty list" in e for e in _errs(timeframes="next_session"))
    assert any("non-empty list" in e for e in _errs(timeframes=[]))


def test_validate_one_timeframes_duplicate_rejected():
    errs = _errs(timeframes=["next_session", "next_session"])
    assert any("timeframes has duplicate entries" in e for e in errs)


def test_validate_one_chart_intervals_duplicate_rejected():
    errs = _errs(chart_intervals=["4h", "4h"])
    assert any("chart_intervals has duplicate entries" in e for e in errs)


def test_validate_one_duplicate_id_tracked_via_seen_set():
    seen = set()
    assert not any("duplicate id" in e for e in CL._validate_one(VALID, 0, seen))
    assert any("duplicate id" in e for e in CL._validate_one(VALID, 1, seen))


def test_validate_one_clean_asset_has_no_errors():
    assert CL._validate_one(VALID, 0, set()) == []


# ===================================================================== _normalize
def test_normalize_applies_full_default_set():
    n = CL._normalize(VALID)
    assert n["enabled"] is True
    assert n["roll_utc"] == 0
    assert n["related"] == ""
    assert n["publish_policy"] == "approval_required"
    assert n["report_tier"] == "official"
    assert n["forecast_window"] == "next_session"
    assert n["fundamentals_source"] == "auto"
    assert n["include_news"] is True


def test_normalize_include_fundamentals_only_for_equity():
    assert CL._normalize({**VALID, "asset_class": "fx"})["include_fundamentals"] is False
    eq = CL._normalize({**VALID, "asset_class": "equity", "session_profile": "us_equity_rth"})
    assert eq["include_fundamentals"] is True


def test_normalize_does_not_mutate_input():
    src = dict(VALID)
    CL._normalize(VALID)
    assert VALID == src                      # _normalize copies; caller's dict untouched


def test_normalize_chart_intervals_dedup_canonical_when_user_repeats_it():
    # user passes the canonical pair in reverse order -> still exactly one of each, canonical-first
    out = CL._normalize({**VALID, "chart_intervals": ["1d", "60m"]})["chart_intervals"]
    assert out == ["60m", "1d"]


def test_normalize_timeframes_default_dedup_from_list():
    out = CL._normalize({**VALID, "timeframes": ["next_week", "next_week", "next_session"]})
    assert out["timeframes"] == ["next_week", "next_session"]


# ===================================================================== load_runtime_config
def test_load_runtime_config_missing_file_returns_defaults(tmp_path):
    cfg = CL.load_runtime_config(tmp_path / "absent.json")
    assert cfg["ADVISOR_DATA_PROVIDER"] == "yahoo"
    assert cfg["ASSETFRAME_RETENTION_DAYS"] == "14"


def test_load_runtime_config_skips_none_values(tmp_path):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ADVISOR_DATA_PROVIDER": None, "TWELVEDATA_RATE_PER_MIN": "99"}),
                 encoding="utf-8")
    cfg = CL.load_runtime_config(p)
    assert cfg["ADVISOR_DATA_PROVIDER"] == "yahoo"        # None ignored -> default kept
    assert cfg["TWELVEDATA_RATE_PER_MIN"] == "99"         # real override applied


def test_load_runtime_config_corrupt_file_returns_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert CL.load_runtime_config(p)["ADVISOR_DATA_PROVIDER"] == "yahoo"


def test_load_runtime_config_non_dict_json_returns_defaults(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert CL.load_runtime_config(p)["ASSETFRAME_RETENTION_DAYS"] == "14"


def test_load_runtime_config_returns_independent_copy(tmp_path):
    cfg = CL.load_runtime_config(tmp_path / "absent.json")
    cfg["ADVISOR_DATA_PROVIDER"] = "MUTATED"
    assert CL.RUNTIME_DEFAULTS["ADVISOR_DATA_PROVIDER"] == "yahoo"   # module defaults untouched


def test_load_runtime_config_tolerates_utf8_bom(tmp_path):
    p = tmp_path / "bom.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"ASSETFRAME_RETENTION_DAYS": "7"}).encode("utf-8"))
    assert CL.load_runtime_config(p)["ASSETFRAME_RETENTION_DAYS"] == "7"


# ===================================================================== apply_runtime_env
def test_apply_runtime_env_missing_file_is_noop(tmp_path, clean_env):
    assert CL.apply_runtime_env(tmp_path / "absent.json") == {}
    assert "ADVISOR_DATA_PROVIDER" not in os.environ      # no provider default on missing file


def test_apply_runtime_env_non_dict_file_is_noop(tmp_path, clean_env):
    p = tmp_path / "arr.json"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert CL.apply_runtime_env(p) == {}
    assert "ADVISOR_DATA_PROVIDER" not in os.environ


def test_apply_runtime_env_seeds_keys_and_returns_applied(tmp_path, clean_env):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ASSETFRAME_RETENTION_DAYS": "30"}), encoding="utf-8")
    applied = CL.apply_runtime_env(p)
    assert applied["ASSETFRAME_RETENTION_DAYS"] == "30"
    assert os.environ["ASSETFRAME_RETENTION_DAYS"] == "30"


def test_apply_runtime_env_skips_empty_string_value(tmp_path, clean_env):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ASSETFRAME_BRIEF_MODEL": ""}), encoding="utf-8")
    applied = CL.apply_runtime_env(p)
    assert "ASSETFRAME_BRIEF_MODEL" not in os.environ
    assert "ASSETFRAME_BRIEF_MODEL" not in applied


def test_apply_runtime_env_skips_none_value(tmp_path, clean_env):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ASSETFRAME_BRIEF_MODEL": None}), encoding="utf-8")
    applied = CL.apply_runtime_env(p)
    assert "ASSETFRAME_BRIEF_MODEL" not in os.environ
    assert "ASSETFRAME_BRIEF_MODEL" not in applied


def test_apply_runtime_env_ignores_non_allowlisted_key(tmp_path, clean_env):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"RANDOM_UNLISTED_KEY": "x"}), encoding="utf-8")
    CL.apply_runtime_env(p)
    assert "RANDOM_UNLISTED_KEY" not in os.environ


def test_apply_runtime_env_unknown_license_defaults_to_yahoo(tmp_path, clean_env):
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ASSETFRAME_DATA_LICENSE": "weird-mode"}), encoding="utf-8")
    CL.apply_runtime_env(p)
    assert os.environ["ADVISOR_DATA_PROVIDER"] == "yahoo"  # unknown license -> safe default feed


def test_apply_runtime_env_provider_default_filled_when_file_omits_it(tmp_path, clean_env):
    # a present-but-providerless file still gets the license-derived default provider
    p = tmp_path / "e.json"
    p.write_text(json.dumps({"ASSETFRAME_RETENTION_DAYS": "9"}), encoding="utf-8")
    applied = CL.apply_runtime_env(p)
    assert os.environ["ADVISOR_DATA_PROVIDER"] == "yahoo"
    assert applied["ADVISOR_DATA_PROVIDER"] == "yahoo"


def test_settable_keys_match_runtime_defaults():
    # the allow-list is DERIVED from RUNTIME_DEFAULTS (regression: a hardcoded subset dropped knobs)
    assert set(CL.SETTABLE_RUNTIME_KEYS) == set(CL.RUNTIME_DEFAULTS)


# ===================================================================== validate_config.main
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


def test_main_valid_config_prints_ok_and_does_not_exit(tmp_path):
    code, out = _run_main(_write(tmp_path / "ok.json", [VALID]))
    assert code is None                       # success path returns; no sys.exit
    assert out.startswith("OK:")
    assert "1 assets (1 enabled)" in out


def test_main_valid_config_with_equity_runs_holiday_check(tmp_path):
    # equity on an exchange calendar exercises _holiday_warnings without raising
    eq = {**VALID, "id": "e", "asset_class": "equity", "session_profile": "us_equity_rth",
          "cadence": "trading_day", "timezone": "America/New_York"}
    code, out = _run_main(_write(tmp_path / "eq.json", [eq]))
    assert code is None
    assert "OK:" in out


def test_main_invalid_config_exits_2(tmp_path):
    bad = {**VALID, "id": "a", "asset_class": "forex"}
    code, out = _run_main(_write(tmp_path / "bad.json", [bad]))
    assert code == 2
    assert "INVALID" in out


def test_main_missing_file_exits_2(tmp_path):
    code, out = _run_main(tmp_path / "does_not_exist.json")
    assert code == 2
    assert "INVALID" in out


def test_validate_config_reexports_loader_symbols():
    # validate_config imports these from config_loader; a rename would break the module at import
    assert VC.DEFAULT_CONFIG == CL.DEFAULT_CONFIG
    assert VC.ConfigError is CL.ConfigError
    assert VC.load_assets is CL.load_assets
