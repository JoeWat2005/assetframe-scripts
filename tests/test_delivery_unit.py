"""Offline unit tests for scripts/delivery/* (export_content, _r2, publish, r2_purge).

These modules were only covered transitively before the subpackage refactor. Everything
here is deterministic and fully offline: no network, no Neon, no boto3, no real R2 — the
S3 client is a hand-rolled fake injected via sys.modules / monkeypatch, the filesystem is a
pytest tmp_path, and R2_* env is saved/restored by a fixture so nothing leaks.

Run:  python -m pytest tests/test_delivery_unit.py -q
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scripts  # noqa: F401  (defensive: apply the subpackage sys.path shim if run in isolation)
import export_content as EC
import publish as PUB
import r2_purge as PURGE
import _r2 as R2


# --------------------------------------------------------------------------- #
# export_content — pure helpers
# --------------------------------------------------------------------------- #
class TestCadenceOf:
    def test_weekly_from_iso_week_stamp(self):
        assert EC.cadence_of("AF-2026W22-GBPUSD") == "weekly"

    def test_weekly_is_case_insensitive(self):
        assert EC.cadence_of("AF-2026w05-BTC") == "weekly"

    def test_monthly_from_six_digit_stamp(self):
        assert EC.cadence_of("AF-202606-BTC") == "monthly"

    def test_daily_from_eight_digit_stamp(self):
        assert EC.cadence_of("AF-20260628-BTC") == "daily"

    def test_daily_from_datetime_stamp(self):
        assert EC.cadence_of("AF-202606281230-ES") == "daily"

    def test_empty_string_defaults_daily(self):
        assert EC.cadence_of("") == "daily"

    def test_none_defaults_daily(self):
        assert EC.cadence_of(None) == "daily"

    def test_no_period_segment_defaults_daily(self):
        # Only one '-'-delimited part -> no stamp -> daily.
        assert EC.cadence_of("AF") == "daily"

    def test_hyphenated_ticker_uses_period_segment_not_ticker(self):
        # parts[1] is the stamp regardless of a hyphenated ticker like BRK-B.
        assert EC.cadence_of("AF-202606-BRK-B") == "monthly"


class TestParseResults:
    def test_basic_packed_results(self):
        assert EC._parse_results("P1=Y P2=N P3=NT") == {"P1": "Y", "P2": "N", "P3": "NT"}

    def test_empty_string_is_empty_dict(self):
        assert EC._parse_results("") == {}

    def test_none_is_empty_dict(self):
        assert EC._parse_results(None) == {}

    def test_token_without_equals_is_skipped(self):
        assert EC._parse_results("garbage P1=Y") == {"P1": "Y"}

    def test_empty_value_is_kept(self):
        assert EC._parse_results("P1= P2=N") == {"P1": "", "P2": "N"}

    def test_extra_whitespace_tolerated(self):
        assert EC._parse_results("  P1=Y   P2=N  ") == {"P1": "Y", "P2": "N"}


class TestNormAssetClass:
    def test_passthrough_known_key(self):
        assert EC._norm_asset_class("crypto") == "crypto"

    def test_aliases_plural_and_synonyms(self):
        assert EC._norm_asset_class("equities") == "equity"
        assert EC._norm_asset_class("stocks") == "equity"
        assert EC._norm_asset_class("forex") == "fx"
        assert EC._norm_asset_class("indices") == "index"
        assert EC._norm_asset_class("commodities") == "commodity"
        assert EC._norm_asset_class("future") == "futures"

    def test_case_and_whitespace_normalised(self):
        assert EC._norm_asset_class("  EQUITY  ") == "equity"

    def test_empty_and_none_return_empty(self):
        assert EC._norm_asset_class("") == ""
        assert EC._norm_asset_class(None) == ""

    def test_unknown_value_passes_through_lowered(self):
        assert EC._norm_asset_class("Bond") == "bond"


class TestAggRows:
    def test_groups_and_computes_hit_rate(self):
        rows = [
            {"k": "a", "hits": 3, "misses": 1},
            {"k": "a", "hits": 1, "misses": 1},
            {"k": "b", "hits": 0, "misses": 0},
        ]
        out = EC._agg_rows(rows, lambda r: r["k"])
        by_key = {e["key"]: e for e in out}
        assert by_key["a"]["reportsScored"] == 2
        assert by_key["a"]["hits"] == 4
        assert by_key["a"]["misses"] == 2
        assert by_key["a"]["hitRate"] == round(100 * 4 / 6, 1)
        # b has no graded predictions -> hitRate None
        assert by_key["b"]["hitRate"] is None

    def test_empty_key_rows_skipped(self):
        rows = [{"k": "", "hits": 1, "misses": 0}, {"k": "a", "hits": 1, "misses": 0}]
        out = EC._agg_rows(rows, lambda r: r["k"])
        assert [e["key"] for e in out] == ["a"]

    def test_sorted_by_hit_rate_then_count_with_none_last(self):
        rows = [
            {"k": "low", "hits": 1, "misses": 3},     # 25%
            {"k": "high", "hits": 3, "misses": 1},    # 75%
            {"k": "ungraded", "hits": 0, "misses": 0},  # None
        ]
        out = EC._agg_rows(rows, lambda r: r["k"])
        assert [e["key"] for e in out] == ["high", "low", "ungraded"]

    def test_non_numeric_hit_miss_coerced_to_zero(self):
        rows = [{"k": "a", "hits": None, "misses": ""}]
        out = EC._agg_rows(rows, lambda r: r["k"])
        assert out[0]["hits"] == 0 and out[0]["misses"] == 0 and out[0]["hitRate"] is None


class TestBuildAggregates:
    def test_empty_rows_yield_empty_structure(self):
        agg = EC._build_aggregates([])
        assert agg == {
            "byInstrument": [], "byAssetClass": [], "byPredictionType": [],
            "byRegime": [], "byCadence": [], "timeline": [], "calibrationCurve": [],
            "componentVsOutcome": []}

    def _ten_rows(self):
        return [{
            "instrument": "Bitcoin", "asset_class": "crypto", "confidence": "70",
            "hits": 1, "misses": 1, "report_id": "AF-20260620-BTC",
            "market_regime": "trending", "pred_type": "trend",
            "window_end_utc": f"2026-06-2{i}T00:00:00Z",
        } for i in range(10)]

    def test_by_instrument_aggregation(self):
        agg = EC._build_aggregates(self._ten_rows())
        assert len(agg["byInstrument"]) == 1
        e = agg["byInstrument"][0]
        assert e["instrument"] == "Bitcoin"
        assert e["reportsScored"] == 10
        assert e["hits"] == 10 and e["misses"] == 10
        assert e["hitRate"] == 50.0
        assert e["assetClass"] == "crypto"
        # ticker is intentionally left blank here (backfilled by main()).
        assert e["ticker"] == ""

    def test_flat_groupings(self):
        agg = EC._build_aggregates(self._ten_rows())
        assert agg["byAssetClass"][0] == {
            "assetClass": "crypto", "reportsScored": 10, "hits": 10, "misses": 10,
            "hitRate": 50.0}
        assert agg["byCadence"][0]["cadence"] == "daily"
        assert agg["byRegime"][0]["regime"] == "trending"
        assert agg["byPredictionType"][0]["predType"] == "trend"

    def test_calibration_curve_gated_to_ten_rows(self):
        # 9 rows -> not enough -> empty curve.
        assert EC._build_aggregates(self._ten_rows()[:9])["calibrationCurve"] == []
        curve = EC._build_aggregates(self._ten_rows())["calibrationCurve"]
        assert len(curve) == 1
        assert curve[0]["bucket"] == "70-79"
        assert curve[0]["confLo"] == 70 and curve[0]["confHi"] == 79
        assert curve[0]["reports"] == 10
        assert curve[0]["hitRate"] == 50.0

    def test_calibration_curve_bins_clamped(self):
        rows = self._ten_rows()
        rows[0]["confidence"] = "100"   # clamps into the 90-99 bin
        rows[1]["confidence"] = "-5"    # clamps into the 0-9 bin
        rows[2]["confidence"] = "bad"   # non-numeric -> skipped
        buckets = {c["bucket"] for c in EC._build_aggregates(rows)["calibrationCurve"]}
        assert "90-99" in buckets
        assert "0-9" in buckets

    def test_component_vs_outcome_band(self):
        cvo = EC._build_aggregates(self._ten_rows())["componentVsOutcome"]
        assert len(cvo) == 1
        assert cvo[0]["band"] == "Elevated"   # 65 <= 70 < 80
        assert cvo[0]["avgConfidence"] == 70.0
        assert cvo[0]["hitRate"] == 50.0

    def test_timeline_cumulative_hit_rate(self):
        tl = EC._build_aggregates(self._ten_rows())["timeline"]
        assert len(tl) == 10
        assert tl[-1]["cumulativeHitRate"] == 50.0
        assert tl[0]["perReportHitRate"] == 50.0

    def test_blank_instrument_rows_excluded_from_by_instrument(self):
        rows = [{"instrument": "", "hits": 1, "misses": 0}]
        assert EC._build_aggregates(rows)["byInstrument"] == []


# --------------------------------------------------------------------------- #
# export_content.load_track_record
# --------------------------------------------------------------------------- #
LEDGER_HEADER = ("report_id,instrument,view,confidence,results,hits,misses,"
                 "hit_rate_pct,window_end_utc,asset_class,pred_type,market_regime\n")


def _write_ledger(path, rows):
    path.write_text(LEDGER_HEADER + "".join(rows), encoding="utf-8")


class TestLoadTrackRecord:
    def test_missing_ledger_returns_empty(self, tmp_path):
        ledger = tmp_path / "nope.csv"
        pred = tmp_path / "predictions"
        pred.mkdir()
        scored, open_calls, calib, agg = EC.load_track_record(ledger, pred, set())
        assert scored == []
        assert open_calls == []
        assert calib is None
        assert agg["byInstrument"] == []

    def test_empty_ledger_file_returns_empty(self, tmp_path):
        ledger = tmp_path / "outcome_ledger.csv"
        ledger.write_text("", encoding="utf-8")
        pred = tmp_path / "predictions"
        pred.mkdir()
        scored, open_calls, calib, agg = EC.load_track_record(ledger, pred, set())
        assert scored == [] and calib is None

    def test_small_ledger_has_no_calibration(self, tmp_path):
        ledger = tmp_path / "outcome_ledger.csv"
        _write_ledger(ledger, [
            "AF-20260620-BTC,Bitcoin,bullish,70,P1=Y P2=N,2,0,100,2026-06-21T00:00:00Z,crypto,trend,trending\n",
            "AF-20260620-ETH,Ether,bearish,55,P1=N,0,1,0,2026-06-21T00:00:00Z,crypto,trend,ranging\n",
        ])
        pred = tmp_path / "predictions"
        pred.mkdir()
        scored, open_calls, calib, agg = EC.load_track_record(ledger, pred, set())
        assert calib is None              # <10 rows
        assert len(scored) == 2
        first = scored[0]
        assert first["reportId"] == "AF-20260620-BTC"
        assert first["assetClass"] == "crypto"
        assert first["scoredCadence"] == "daily"
        # raw passthrough columns are strings
        assert first["hits"] == "2"

    def test_predictions_merged_with_verdicts(self, tmp_path):
        ledger = tmp_path / "outcome_ledger.csv"
        _write_ledger(ledger, [
            "AF-20260620-BTC,Bitcoin,bullish,70,P1=Y P2=N,2,0,100,2026-06-21T00:00:00Z,crypto,trend,trending\n",
        ])
        pred = tmp_path / "predictions"
        pred.mkdir()
        (pred / "AF-20260620-BTC_predictions.json").write_text(json.dumps({
            "report_id": "AF-20260620-BTC", "instrument": "Bitcoin", "symbol": "BTC",
            "view": "bullish", "confidence": 70, "window_end_utc": "2026-06-21T00:00:00Z",
            "taxonomy": {"prediction_type": "trend"},
            "predictions": [
                {"id": "P1", "type": "directional", "text": "up", "expect": True},
                {"id": "P2", "type": "manual", "note": "a manual call"},
            ],
        }), encoding="utf-8")
        scored, open_calls, calib, agg = EC.load_track_record(
            ledger, pred, {"AF-20260620-BTC"})
        assert len(open_calls) == 1
        oc = open_calls[0]
        assert oc["n"] == 2
        assert oc["nManual"] == 1
        assert oc["hits"] == 2            # from hits_by_id
        assert oc["scored"] is True
        p1, p2 = oc["predictions"]
        assert p1["verdict"] == "Y"
        assert p1["expect"] is True
        assert p1["manual"] is False
        assert p1["predType"] == "trend"
        assert p2["verdict"] == "N"
        assert p2["manual"] is True
        assert p2["text"] == "a manual call"   # falls back to note
        assert p2["expect"] is None            # non-bool coerced to None

    def test_calibration_buckets_with_ten_rows(self, tmp_path):
        ledger = tmp_path / "outcome_ledger.csv"
        rows = []
        for i in range(10):
            conf = 55 if i < 4 else (70 if i < 7 else 90)
            rows.append(f"AF-2026062{i}-BTC,Bitcoin,bullish,{conf},P1=Y,1,0,100,"
                        f"2026-06-2{i}T00:00:00Z,crypto,trend,trending\n")
        _write_ledger(ledger, rows)
        pred = tmp_path / "predictions"
        pred.mkdir()
        scored, open_calls, calib, agg = EC.load_track_record(ledger, pred, set())
        assert calib is not None
        assert calib["<=60"]["n"] == 4
        assert calib["61-75"]["n"] == 3
        assert calib[">75"]["n"] == 3
        assert calib["<=60"]["hitRate"] == 100.0
        # aggregates calibration curve is present now (>=10 rows)
        assert agg["calibrationCurve"]

    def test_corrupt_prediction_file_skipped(self, tmp_path, capsys):
        ledger = tmp_path / "outcome_ledger.csv"
        ledger.write_text("", encoding="utf-8")
        pred = tmp_path / "predictions"
        pred.mkdir()
        (pred / "bad_predictions.json").write_text("{not json", encoding="utf-8")
        (pred / "ok_predictions.json").write_text(json.dumps({
            "report_id": "AF-20260620-BTC", "instrument": "Bitcoin",
            "predictions": [], "window_end_utc": "2026-06-21T00:00:00Z"}), encoding="utf-8")
        scored, open_calls, calib, agg = EC.load_track_record(ledger, pred, set())
        assert [c["reportId"] for c in open_calls] == ["AF-20260620-BTC"]
        assert "skipped" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# export_content.load_catalog  +  _publish_policy_by_ticker
# --------------------------------------------------------------------------- #
def _write_meta(reports_dir, date, slug, meta, extra_files=()):
    d = reports_dir / date / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    for name in extra_files:
        (d / name).write_text("x", encoding="utf-8")
    return d


class TestLoadCatalog:
    def test_basic_edition_fields_and_hidden_gate(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _write_meta(reports, "2026-06-20", "BTC", {
            "instrument": "Bitcoin", "ticker": "BTC", "asset_class": "crypto",
            "status": "published", "report_id": "AF-20260620-BTC",
            "data_license_mode": "commercial", "data_license_degraded": True,
        }, extra_files=("pro.html", "free.html"))
        _write_meta(reports, "2026-06-19", "ETH", {
            "instrument": "Ether", "ticker": "ETH", "report_id": "AF-20260619-ETH"})
        monkeypatch.setattr(EC, "_publish_policy_by_ticker", lambda: {"BTC": "auto"})

        cat = EC.load_catalog(reports, include_dev=False)
        assert [e["slug"] for e in cat] == ["BTC", "ETH"]   # reverse sort by (date, slug)
        btc = cat[0]
        assert btc["hidden"] is False          # policy auto
        assert btc["hasPro"] is True
        assert btc["assetClass"] == "crypto"
        assert btc["scoredCadence"] == "daily"
        assert btc["dataLicense"] == "commercial"
        assert btc["dataLicenseDegraded"] is True
        assert btc["freeHtml"] == "/api/report/2026-06-20/BTC/free.html"
        assert "_dir" in btc                   # main() strips this later
        eth = cat[1]
        assert eth["hidden"] is True           # default approval_required
        assert eth["hasPro"] is False
        assert eth["dataLicense"] == "personal"   # default when metadata omits it

    def test_dev_editions_skipped_unless_include_dev(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _write_meta(reports, "_dev", "XYZ", {"instrument": "Dev", "ticker": "XYZ"})
        _write_meta(reports, "2026-06-20", "BTC", {"instrument": "Bitcoin", "ticker": "BTC"})
        monkeypatch.setattr(EC, "_publish_policy_by_ticker", lambda: {})

        assert [e["slug"] for e in EC.load_catalog(reports, include_dev=False)] == ["BTC"]
        slugs = {e["slug"] for e in EC.load_catalog(reports, include_dev=True)}
        assert slugs == {"BTC", "XYZ"}

    def test_since_filter_excludes_old(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _write_meta(reports, "2026-06-20", "BTC", {"ticker": "BTC"})
        _write_meta(reports, "2026-06-10", "ETH", {"ticker": "ETH"})
        monkeypatch.setattr(EC, "_publish_policy_by_ticker", lambda: {})
        cat = EC.load_catalog(reports, include_dev=False, since="2026-06-15")
        assert [e["slug"] for e in cat] == ["BTC"]

    def test_corrupt_metadata_skipped(self, tmp_path, monkeypatch, capsys):
        reports = tmp_path / "reports"
        d = reports / "2026-06-20" / "BAD"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("{bad json", encoding="utf-8")
        _write_meta(reports, "2026-06-20", "BTC", {"ticker": "BTC"})
        monkeypatch.setattr(EC, "_publish_policy_by_ticker", lambda: {})
        cat = EC.load_catalog(reports, include_dev=False)
        assert [e["slug"] for e in cat] == ["BTC"]
        assert "skipped" in capsys.readouterr().err

    def test_policy_falls_back_to_slug_when_ticker_missing(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _write_meta(reports, "2026-06-20", "GBPUSD", {"instrument": "Cable"})  # no ticker
        monkeypatch.setattr(EC, "_publish_policy_by_ticker", lambda: {"GBPUSD": "auto"})
        cat = EC.load_catalog(reports, include_dev=False)
        assert cat[0]["hidden"] is False   # matched on slug


class TestPublishPolicyByTicker:
    def test_uses_config_loader_validated_path(self, tmp_path, monkeypatch):
        import config_loader
        monkeypatch.setattr(EC, "ROOT", tmp_path)
        monkeypatch.setattr(config_loader, "load_assets", lambda cfg: [
            {"ticker": "btc", "publish_policy": "auto"},
            {"ticker": "ETH", "publish_policy": "approval_required"},
            {"ticker": "", "publish_policy": "auto"},   # blank ticker skipped
        ])
        out = EC._publish_policy_by_ticker()
        assert out == {"BTC": "auto", "ETH": "approval_required"}

    def test_default_policy_when_missing(self, tmp_path, monkeypatch):
        import config_loader
        monkeypatch.setattr(EC, "ROOT", tmp_path)
        monkeypatch.setattr(config_loader, "load_assets", lambda cfg: [{"ticker": "BTC"}])
        assert EC._publish_policy_by_ticker() == {"BTC": "approval_required"}

    def test_raw_json_fallback_when_loader_raises(self, tmp_path, monkeypatch):
        import config_loader
        monkeypatch.setattr(EC, "ROOT", tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "assets.json").write_text(json.dumps({
            "assets": [{"ticker": "BTC", "publish_policy": "auto"}]}), encoding="utf-8")

        def _boom(cfg):
            raise RuntimeError("validation hiccup")
        monkeypatch.setattr(config_loader, "load_assets", _boom)
        assert EC._publish_policy_by_ticker() == {"BTC": "auto"}

    def test_returns_empty_when_no_config_at_all(self, tmp_path, monkeypatch):
        import config_loader
        monkeypatch.setattr(EC, "ROOT", tmp_path)   # no config/assets.json on disk

        def _boom(cfg):
            raise RuntimeError("missing")
        monkeypatch.setattr(config_loader, "load_assets", _boom)
        assert EC._publish_policy_by_ticker() == {}

    def test_raw_json_dict_without_assets_currently_raises(self, tmp_path, monkeypatch):
        # BUG PIN (reported, not a fix): the raw-JSON fallback iterates `assets` assuming a
        # list of dicts, but a JSON object that isn't the {"assets":[...]} wrapper (e.g. a
        # dict keyed by ticker) makes `assets = raw` and the loop hits str.get -> AttributeError,
        # which propagates and aborts the whole export — contradicting the docstring's
        # "a transient validation hiccup can't break the export". Asserting CURRENT behaviour.
        import config_loader
        monkeypatch.setattr(EC, "ROOT", tmp_path)
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "assets.json").write_text(
            json.dumps({"BTC": {"publish_policy": "auto"}}), encoding="utf-8")
        monkeypatch.setattr(config_loader, "load_assets",
                            lambda cfg: (_ for _ in ()).throw(RuntimeError("invalid")))
        with pytest.raises(AttributeError):
            EC._publish_policy_by_ticker()


# --------------------------------------------------------------------------- #
# _r2 — env loading + R2Store
# --------------------------------------------------------------------------- #
R2_KEYS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


@pytest.fixture
def clean_r2_env():
    """Save/clear/restore every R2_* var so _load_local_env's direct os.environ writes
    (which bypass monkeypatch) never leak between tests."""
    saved = {k: os.environ.get(k) for k in R2_KEYS}
    for k in R2_KEYS:
        os.environ.pop(k, None)
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class _FakeS3:
    def __init__(self, list_responses=None, delete_responses=None):
        self.put_calls = []
        self.list_calls = []
        self.delete_calls = []
        self._list = list(list_responses or [])
        self._delete = list(delete_responses or [])

    def put_object(self, **kw):
        self.put_calls.append(kw)

    def list_objects_v2(self, **kw):
        self.list_calls.append(kw)
        return self._list.pop(0) if self._list else {}

    def delete_objects(self, **kw):
        self.delete_calls.append(kw)
        return self._delete.pop(0) if self._delete else {}


class TestLoadLocalEnv:
    def test_populates_only_r2_vars(self, tmp_path, monkeypatch, clean_r2_env):
        (tmp_path / ".env").write_text(
            "# a comment\n"
            "\n"
            "R2_ACCOUNT_ID=acct\n"
            "R2_BUCKET=bkt\n"
            "OTHER_SECRET=nope\n"
            "MALFORMED_LINE_NO_EQUALS\n",
            encoding="utf-8")
        monkeypatch.setattr(R2, "ROOT", tmp_path)
        R2._load_local_env()
        assert os.environ["R2_ACCOUNT_ID"] == "acct"
        assert os.environ["R2_BUCKET"] == "bkt"
        assert "OTHER_SECRET" not in os.environ

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch, clean_r2_env):
        os.environ["R2_BUCKET"] = "already-set"
        (tmp_path / ".env").write_text("R2_BUCKET=from-file\nR2_ACCOUNT_ID=acct\n",
                                       encoding="utf-8")
        monkeypatch.setattr(R2, "ROOT", tmp_path)
        R2._load_local_env()
        assert os.environ["R2_BUCKET"] == "already-set"   # not overwritten
        assert os.environ["R2_ACCOUNT_ID"] == "acct"

    def test_missing_env_file_is_noop(self, tmp_path, monkeypatch, clean_r2_env):
        monkeypatch.setattr(R2, "ROOT", tmp_path)   # no .env present
        R2._load_local_env()   # must not raise
        assert "R2_ACCOUNT_ID" not in os.environ

    def test_value_with_equals_sign_preserved(self, tmp_path, monkeypatch, clean_r2_env):
        (tmp_path / ".env").write_text("R2_SECRET_ACCESS_KEY=ab=cd=ef\n", encoding="utf-8")
        monkeypatch.setattr(R2, "ROOT", tmp_path)
        R2._load_local_env()
        assert os.environ["R2_SECRET_ACCESS_KEY"] == "ab=cd=ef"


class TestR2StoreFromEnv:
    def test_missing_vars_prints_hint_and_returns_none(self, tmp_path, monkeypatch,
                                                       capsys, clean_r2_env):
        monkeypatch.setattr(R2, "ROOT", tmp_path)   # no .env -> stays missing
        store = R2.R2Store.from_env("HINT-TEXT")
        assert store is None
        err = capsys.readouterr().err
        assert "Missing environment variables" in err
        assert "HINT-TEXT" in err

    def test_boto3_missing_returns_none(self, tmp_path, monkeypatch, capsys, clean_r2_env):
        for k in R2_KEYS:
            os.environ[k] = "v"
        monkeypatch.setattr(R2, "ROOT", tmp_path)
        monkeypatch.setitem(sys.modules, "boto3", None)   # force ImportError on `import boto3`
        store = R2.R2Store.from_env("hint")
        assert store is None
        assert "boto3 is required" in capsys.readouterr().err

    def test_builds_client_when_boto3_present(self, tmp_path, monkeypatch, clean_r2_env):
        os.environ["R2_ACCOUNT_ID"] = "acct"
        os.environ["R2_ACCESS_KEY_ID"] = "akid"
        os.environ["R2_SECRET_ACCESS_KEY"] = "secret"
        os.environ["R2_BUCKET"] = "bkt"
        monkeypatch.setattr(R2, "ROOT", tmp_path)

        sentinel = object()
        calls = []
        fake_boto3 = types.SimpleNamespace(
            client=lambda service, **kw: (calls.append((service, kw)), sentinel)[1])
        monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

        store = R2.R2Store.from_env("hint")
        assert store is not None
        assert store.client is sentinel
        assert store.bucket == "bkt"
        service, kw = calls[0]
        assert service == "s3"
        assert kw["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"
        assert kw["aws_access_key_id"] == "akid"
        assert kw["aws_secret_access_key"] == "secret"
        assert kw["region_name"] == "auto"


class TestR2StorePutListDelete:
    def test_put_passes_through_to_client(self):
        c = _FakeS3()
        R2.R2Store(c, "bkt").put("k/x.html", b"body", "text/html")
        assert c.put_calls == [
            {"Bucket": "bkt", "Key": "k/x.html", "Body": b"body", "ContentType": "text/html"}]

    def test_list_keys_pages_through_continuation(self):
        c = _FakeS3(list_responses=[
            {"Contents": [{"Key": "a"}, {"Key": "b"}], "IsTruncated": True,
             "NextContinuationToken": "tok1"},
            {"Contents": [{"Key": "c"}], "IsTruncated": False},
        ])
        keys = R2.R2Store(c, "bkt").list_keys()
        assert keys == ["a", "b", "c"]
        # second page forwarded the continuation token
        assert c.list_calls[1].get("ContinuationToken") == "tok1"
        assert "ContinuationToken" not in c.list_calls[0]

    def test_list_keys_forwards_prefix(self):
        c = _FakeS3(list_responses=[{"Contents": [{"Key": "p/a"}], "IsTruncated": False}])
        keys = R2.R2Store(c, "bkt").list_keys("p/")
        assert keys == ["p/a"]
        assert c.list_calls[0]["Prefix"] == "p/"

    def test_list_keys_empty_bucket(self):
        c = _FakeS3(list_responses=[{"IsTruncated": False}])   # no Contents key
        assert R2.R2Store(c, "bkt").list_keys() == []

    def test_delete_empty_keys_is_noop(self):
        c = _FakeS3()
        assert R2.R2Store(c, "bkt").delete([]) == (0, [])
        assert c.delete_calls == []

    def test_delete_batches_over_1000(self):
        c = _FakeS3()   # default {} responses -> no Errors
        keys = [f"k{i}" for i in range(2500)]
        deleted, failed = R2.R2Store(c, "bkt").delete(keys)
        assert deleted == 2500
        assert failed == []
        assert len(c.delete_calls) == 3   # 1000 + 1000 + 500

    def test_delete_reports_failures(self):
        c = _FakeS3(delete_responses=[
            {"Errors": [{"Key": "k1", "Message": "boom"}]},
        ])
        deleted, failed = R2.R2Store(c, "bkt").delete(["k1", "k2"])
        assert deleted == 1            # 2 in batch - 1 error
        assert failed == ["k1: boom"]


# --------------------------------------------------------------------------- #
# publish.discover + publish.main
# --------------------------------------------------------------------------- #
def _make_report(reports, date, slug, files):
    d = reports / date / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text("{}", encoding="utf-8")
    for name in files:
        (d / name).write_text("x", encoding="utf-8")
    return d


class TestDiscover:
    def test_discovers_only_known_upload_files(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC",
                     ["free.html", "free.pdf", "preview.png", "ignored.txt"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        items = PUB.discover(None)
        keys = {key for _, key, _ in items}
        assert keys == {
            "2026-06-20/BTC/free.html", "2026-06-20/BTC/free.pdf",
            "2026-06-20/BTC/preview.png"}
        # content types come from UPLOAD_FILES
        ctypes = {key: ct for _, key, ct in items}
        assert ctypes["2026-06-20/BTC/free.pdf"] == "application/pdf"

    def test_skips_underscore_dev_dirs(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _make_report(reports, "_dev", "XYZ", ["free.html"])
        _make_report(reports, "2026-06-20", "BTC", ["free.html"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        keys = {key for _, key, _ in PUB.discover(None)}
        assert keys == {"2026-06-20/BTC/free.html"}

    def test_date_filter(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC", ["free.html"])
        _make_report(reports, "2026-06-19", "ETH", ["free.html"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        keys = {key for _, key, _ in PUB.discover("2026-06-20")}
        assert keys == {"2026-06-20/BTC/free.html"}

    def test_no_reports_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(PUB, "REPORTS", tmp_path / "reports")
        assert PUB.discover(None) == []


class _RecordingStore:
    def __init__(self, put_error=None):
        self.bucket = "test-bucket"
        self.puts = []
        self.put_error = put_error

    def put(self, key, body, ctype):
        self.puts.append((key, body, ctype))
        if self.put_error is not None:
            raise self.put_error


class TestPublishMain:
    def test_no_files_message(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(PUB, "REPORTS", tmp_path / "reports")
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        assert "No report files found" in capsys.readouterr().out

    def test_dry_run_does_not_touch_r2(self, tmp_path, monkeypatch, capsys):
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC", ["free.html", "free.pdf"])
        monkeypatch.setattr(PUB, "REPORTS", reports)

        def _boom(hint):
            raise AssertionError("from_env must not be called in --dry-run")
        monkeypatch.setattr(PUB, "R2Store", types.SimpleNamespace(from_env=_boom))
        monkeypatch.setattr(sys, "argv", ["publish", "--dry-run"])
        PUB.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "2026-06-20/BTC/free.html" in out

    def test_uploads_each_file(self, tmp_path, monkeypatch, capsys):
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC", ["free.html", "free.pdf", "preview.png"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        store = _RecordingStore()
        monkeypatch.setattr(PUB, "R2Store", types.SimpleNamespace(from_env=lambda h: store))
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        out = capsys.readouterr().out
        assert len(store.puts) == 3
        assert "Done - 3 uploaded" in out
        assert "test-bucket" in out

    def test_store_none_exits_2(self, tmp_path, monkeypatch):
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC", ["free.html"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        monkeypatch.setattr(PUB, "R2Store", types.SimpleNamespace(from_env=lambda h: None))
        monkeypatch.setattr(sys, "argv", ["publish"])
        with pytest.raises(SystemExit) as exc:
            PUB.main()
        assert exc.value.code == 2

    def test_upload_failure_exits_1_after_retries(self, tmp_path, monkeypatch, capsys):
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *a, **k: None)   # no real backoff sleeps
        reports = tmp_path / "reports"
        _make_report(reports, "2026-06-20", "BTC", ["free.html"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        store = _RecordingStore(put_error=RuntimeError("network boom"))
        monkeypatch.setattr(PUB, "R2Store", types.SimpleNamespace(from_env=lambda h: store))
        monkeypatch.setattr(sys, "argv", ["publish"])
        with pytest.raises(SystemExit) as exc:
            PUB.main()
        assert exc.value.code == 1
        assert len(store.puts) == 3        # 3 attempts before giving up
        assert "FAILED" in capsys.readouterr().err

    def test_vanished_file_skipped_not_failed(self, tmp_path, monkeypatch, capsys):
        reports = tmp_path / "reports"
        d = _make_report(reports, "2026-06-20", "BTC", ["free.html"])
        monkeypatch.setattr(PUB, "REPORTS", reports)
        store = _RecordingStore()
        monkeypatch.setattr(PUB, "R2Store", types.SimpleNamespace(from_env=lambda h: store))
        monkeypatch.setattr(sys, "argv", ["publish"])
        # Remove the file AFTER discover() but BEFORE the upload loop reads it.
        real_discover = PUB.discover

        def _discover_then_delete(date_filter):
            items = real_discover(date_filter)
            (d / "free.html").unlink()
            return items
        monkeypatch.setattr(PUB, "discover", _discover_then_delete)
        PUB.main()
        out = capsys.readouterr().out
        assert store.puts == []            # nothing uploaded
        assert "vanished" in out


# --------------------------------------------------------------------------- #
# r2_purge.main
# --------------------------------------------------------------------------- #
class _PurgeStore:
    def __init__(self, keys, delete_result=(0, [])):
        self.bucket = "bkt"
        self._keys = keys
        self._delete_result = delete_result
        self.list_prefix = "__unset__"
        self.delete_called = False
        self.deleted_keys = None

    def list_keys(self, prefix=""):
        self.list_prefix = prefix
        return self._keys

    def delete(self, keys):
        self.delete_called = True
        self.deleted_keys = keys
        return self._delete_result


def _patch_purge_store(monkeypatch, store):
    monkeypatch.setattr(PURGE, "R2Store",
                        types.SimpleNamespace(from_env=lambda hint: store))


class TestR2PurgeMain:
    def test_store_none_exits_2(self, monkeypatch):
        _patch_purge_store(monkeypatch, None)
        monkeypatch.setattr(sys, "argv", ["r2_purge"])
        with pytest.raises(SystemExit) as exc:
            PURGE.main()
        assert exc.value.code == 2

    def test_no_keys_nothing_to_do(self, monkeypatch, capsys):
        store = _PurgeStore(keys=[])
        _patch_purge_store(monkeypatch, store)
        monkeypatch.setattr(sys, "argv", ["r2_purge"])
        PURGE.main()
        assert "Nothing to do" in capsys.readouterr().out
        assert store.delete_called is False

    def test_dry_run_lists_but_does_not_delete(self, monkeypatch, capsys):
        store = _PurgeStore(keys=["a", "b", "c"])
        _patch_purge_store(monkeypatch, store)
        monkeypatch.setattr(sys, "argv", ["r2_purge"])
        PURGE.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "3 object(s)" in out
        assert store.delete_called is False

    def test_yes_deletes(self, monkeypatch, capsys):
        store = _PurgeStore(keys=["a", "b"], delete_result=(2, []))
        _patch_purge_store(monkeypatch, store)
        monkeypatch.setattr(sys, "argv", ["r2_purge", "--yes"])
        PURGE.main()
        out = capsys.readouterr().out
        assert store.delete_called is True
        assert store.deleted_keys == ["a", "b"]
        assert "Deleted 2/2" in out

    def test_yes_with_failures_exits_1(self, monkeypatch, capsys):
        store = _PurgeStore(keys=["a", "b"], delete_result=(1, ["a: boom"]))
        _patch_purge_store(monkeypatch, store)
        monkeypatch.setattr(sys, "argv", ["r2_purge", "--yes"])
        with pytest.raises(SystemExit) as exc:
            PURGE.main()
        assert exc.value.code == 1
        assert "FAILED" in capsys.readouterr().err

    def test_prefix_forwarded_to_list_keys(self, monkeypatch, capsys):
        store = _PurgeStore(keys=["2026-06-22/BTC/free.html"])
        _patch_purge_store(monkeypatch, store)
        monkeypatch.setattr(sys, "argv", ["r2_purge", "--prefix", "2026-06-22/"])
        PURGE.main()
        assert store.list_prefix == "2026-06-22/"
        assert "prefix '2026-06-22/'" in capsys.readouterr().out
