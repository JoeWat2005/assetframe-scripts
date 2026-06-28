"""Commercial-license-readiness: the provider registry, the ASSETFRAME_DATA_LICENSE one-knob switch,
the dashboard validators, and the license-provenance surfaced in the analysis block + source-audit.

Policy implemented (owner's choice): a licensed-feed failure in 'commercial' mode still PUBLISHES but
flags the edition `license_degraded` (mark-degraded, not hard-skip). Default mode is 'personal' =
today's behaviour verbatim. Run:  python -m pytest tests/test_data_license.py -q
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import intraday as I
import config_loader as CL
import engine_ops as E
import scaffold_payload as SP


# ------------------------------------------------------------------ registry
def test_registry_shape_and_commercial_flags():
    for p, spec in I.PROVIDER_REGISTRY.items():
        assert set(spec) >= {"commercial", "needs_key"}
        assert isinstance(spec["commercial"], bool) and isinstance(spec["needs_key"], bool)
    # the free tiers can NEVER back a paid report; the paid feeds can (operator-asserted).
    assert I.provider_is_commercial("twelvedata") and I.provider_is_commercial("eodhd")
    assert not I.provider_is_commercial("yahoo") and not I.provider_is_commercial("coingecko")
    assert not I.provider_is_commercial("typo-feed")          # unknown -> not commercial (safe)
    assert I.series_license("twelvedata") == "commercial"
    assert I.series_license("yahoo") == "non_commercial"


# ------------------------------------------------------ license -> degraded logic
def test_license_fields_personal_never_degrades():
    f = I.license_fields("twelvedata", "yahoo", mode="personal")
    assert f["license_mode"] == "personal" and f["license_degraded"] is False
    assert f["daily_license"] == "non_commercial"            # tag still computed, just not a problem


def test_license_fields_commercial_flags_yahoo_fallback():
    # hourly TD (licensed) but daily fell back to Yahoo -> degraded in commercial mode.
    f = I.license_fields("twelvedata", "yahoo", mode="commercial")
    assert f["license_degraded"] is True
    # both licensed -> clean.
    assert I.license_fields("twelvedata", "twelvedata", mode="commercial")["license_degraded"] is False
    # a missing series (None) cannot degrade anything.
    assert I.license_fields("twelvedata", None, mode="commercial")["license_degraded"] is False


# ------------------------------------------------------ one-knob provider default
def test_commercial_mode_defaults_to_licensed_feed():
    p = Path(tempfile.mkdtemp()) / "engine.json"
    p.write_text(json.dumps({"ASSETFRAME_DATA_LICENSE": "commercial"}), encoding="utf-8")
    os.environ.pop("ADVISOR_DATA_PROVIDER", None)
    os.environ.pop("ASSETFRAME_DATA_LICENSE", None)
    try:
        CL.apply_runtime_env(p)
        assert os.environ["ASSETFRAME_DATA_LICENSE"] == "commercial"
        assert os.environ["ADVISOR_DATA_PROVIDER"] == "twelvedata"   # license picked the feed
    finally:
        os.environ.pop("ADVISOR_DATA_PROVIDER", None)
        os.environ.pop("ASSETFRAME_DATA_LICENSE", None)


def test_personal_mode_defaults_to_yahoo():
    p = Path(tempfile.mkdtemp()) / "engine.json"
    p.write_text(json.dumps({"ASSETFRAME_DATA_LICENSE": "personal"}), encoding="utf-8")
    os.environ.pop("ADVISOR_DATA_PROVIDER", None)
    os.environ.pop("ASSETFRAME_DATA_LICENSE", None)
    try:
        CL.apply_runtime_env(p)
        assert os.environ["ADVISOR_DATA_PROVIDER"] == "yahoo"
    finally:
        os.environ.pop("ADVISOR_DATA_PROVIDER", None)
        os.environ.pop("ASSETFRAME_DATA_LICENSE", None)


def test_explicit_provider_wins_over_license_mode():
    # An explicit feed (env) is never overridden by the license default — env-first contract holds.
    p = Path(tempfile.mkdtemp()) / "engine.json"
    p.write_text(json.dumps({"ASSETFRAME_DATA_LICENSE": "commercial"}), encoding="utf-8")
    os.environ["ADVISOR_DATA_PROVIDER"] = "eodhd"
    os.environ.pop("ASSETFRAME_DATA_LICENSE", None)
    try:
        CL.apply_runtime_env(p)
        assert os.environ["ADVISOR_DATA_PROVIDER"] == "eodhd"
    finally:
        os.environ.pop("ADVISOR_DATA_PROVIDER", None)
        os.environ.pop("ASSETFRAME_DATA_LICENSE", None)


def test_license_is_settable_from_engine_json():
    assert "ASSETFRAME_DATA_LICENSE" in CL.SETTABLE_RUNTIME_KEYS    # else set_config silently drops it


# ------------------------------------------------------ dashboard validators
def test_set_config_validators():
    v = E._CONFIG_VALUE_VALIDATORS
    assert "ASSETFRAME_DATA_LICENSE" in E._SETTABLE_CONFIG_KEYS
    assert v["ASSETFRAME_DATA_LICENSE"]("commercial") and v["ASSETFRAME_DATA_LICENSE"]("personal")
    assert not v["ASSETFRAME_DATA_LICENSE"]("free")
    # G5: ADVISOR_DATA_PROVIDER now validated -> a typo can't silently drop you to Yahoo.
    assert v["ADVISOR_DATA_PROVIDER"]("twelvedata")
    assert not v["ADVISOR_DATA_PROVIDER"]("twelvedta")


# ------------------------------------------------------ source-audit provenance
def _audit(provider_block):
    return SP._source_audit_html({"cross_check": "single-source this run"},
                                 {"provider": provider_block}, 8)

def test_source_audit_degraded_warning_commercial():
    html = _audit({"hourly": "twelvedata", "daily": "yahoo",
                   "license_mode": "commercial", "license_degraded": True})
    assert "not for redistribution" in html

def test_source_audit_clean_commercial():
    html = _audit({"hourly": "twelvedata", "daily": "twelvedata",
                   "license_mode": "commercial", "license_degraded": False})
    assert "commercially-licensed feed" in html
    assert "not for redistribution" not in html

def test_source_audit_personal_has_no_license_line():
    html = _audit({"hourly": "yahoo", "daily": "yahoo", "license_mode": "personal"})
    assert "licensing" not in html.lower()

def test_source_audit_shows_split_source():
    # G6: a TD-hourly + Yahoo-daily report must not hide the daily source.
    html = _audit({"hourly": "twelvedata", "daily": "yahoo", "license_mode": "personal"})
    assert "twelvedata (hourly)" in html and "yahoo (daily)" in html


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all data_license tests passed")
