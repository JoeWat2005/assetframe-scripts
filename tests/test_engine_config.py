"""Tests for the single runtime-config file (config/engine.json) + the engine.sqlite aux tables."""
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config_loader as CL
import ledger_db as L


def test_load_runtime_config_defaults_and_overlay():
    d = Path(tempfile.mkdtemp())
    p = d / "engine.json"
    # missing file -> built-in defaults
    cfg = CL.load_runtime_config(p)
    assert cfg["ADVISOR_DATA_PROVIDER"] == "yahoo"
    assert cfg["ASSETFRAME_RETENTION_DAYS"] == "14"
    # file overlays defaults; underscore keys (e.g. _README) ignored
    p.write_text(json.dumps({"_README": "x", "ADVISOR_DATA_PROVIDER": "twelvedata",
                             "ASSETFRAME_RETENTION_DAYS": "30"}), encoding="utf-8")
    cfg2 = CL.load_runtime_config(p)
    assert cfg2["ADVISOR_DATA_PROVIDER"] == "twelvedata"
    assert cfg2["ASSETFRAME_RETENTION_DAYS"] == "30"
    assert "_README" not in cfg2


def test_apply_runtime_env_env_wins():
    d = Path(tempfile.mkdtemp())
    p = d / "engine.json"
    p.write_text(json.dumps({"ADVISOR_DATA_PROVIDER": "twelvedata",
                             "ASSETFRAME_BRIEF_MODEL": "claude-from-file"}), encoding="utf-8")
    # a value already in the environment is NOT overridden (env wins)...
    os.environ["ADVISOR_DATA_PROVIDER"] = "eodhd"
    os.environ.pop("ASSETFRAME_BRIEF_MODEL", None)
    try:
        CL.apply_runtime_env(p)
        assert os.environ["ADVISOR_DATA_PROVIDER"] == "eodhd"          # env preserved
        assert os.environ["ASSETFRAME_BRIEF_MODEL"] == "claude-from-file"  # file seeded the gap
    finally:
        os.environ.pop("ADVISOR_DATA_PROVIDER", None)
        os.environ.pop("ASSETFRAME_BRIEF_MODEL", None)


def test_engine_sqlite_aux_tables_roundtrip():
    d = Path(tempfile.mkdtemp())
    db = d / "engine.sqlite"
    assert L.record_run("AF-run-1", "production", "2026-06-24", "ok", generated=3,
                        manifest={"x": 1}, db_path=db)
    assert L.record_calibration("2", 42, {"version": 1}, db_path=db)
    assert L.cache_assets([{"id": "btc"}, {"id": "gold"}], db_path=db)
    con = sqlite3.connect(str(db))
    try:
        runs = con.execute("SELECT run_id, status, generated FROM runs").fetchall()
        assert runs == [("AF-run-1", "ok", 3)]
        cal = con.execute("SELECT conf_version, n_rows FROM calibration_history").fetchall()
        assert cal == [("2", 42)]
        ac = con.execute("SELECT id, n_assets FROM asset_cache").fetchall()
        assert ac == [(1, 2)]
    finally:
        con.close()


def test_rebuild_preserves_aux_tables():
    # rebuild() DROP+CREATEs only `ledger`; the aux tables (+ their rows) must survive.
    d = Path(tempfile.mkdtemp())
    db = d / "engine.sqlite"
    L.record_run("AF-run-2", "production", "2026-06-24", "ok", db_path=db)
    L.rebuild(csv_path=d / "nope.csv", db_path=db)   # missing CSV -> empty ledger, aux untouched
    con = sqlite3.connect(str(db))
    try:
        assert con.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM ledger").fetchone()[0] == 0
    finally:
        con.close()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
    print("all engine_config tests passed")
