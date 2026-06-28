"""Phase 3 END-TO-END: the PUBLISH + DISTRIBUTE chain + the data-API/MCP contract, OFFLINE.

Phases 1+2 covered the delivery helpers in isolation and pairwise. This file drives the WHOLE
publish/distribute chain across the refactored subgroups against a tmp ROOT, with the network
fully faked, and asserts the SEAM between the engine and the web read-layer end to end:

  1. export_content.main()  consumes a REAL multi-cadence report tree (reports/<date>/<A>/
     metadata.json + payload files), the REAL config/assets.json universe (via the REAL
     scheduler.config.config_loader), a REAL outcome-ledger CSV and REAL data/predictions/*.json,
     and writes content/catalog.json + content/track-record.json -- the engine's contract to the
     web /api/v1 + MCP layer.
  2. publish.main()  then runs over the SAME tree through the REAL _r2.R2Store.from_env() wiring
     with the ONLY external boundary faked: boto3. A stateful in-memory R2 backend captures every
     uploaded key.
  3. CONTRACT GATE (the one just fixed): the set of R2 object keys publish actually uploads must
     EXACTLY equal the set of report-asset keys the catalog advertises (free*/preview URLs gated on
     existence + the pro keys the DB derives from hasPro). No advertised key may 404; no orphan
     upload. A partial edition (free.html only) must advertise + upload exactly one file.
  4. SHAPE: catalog.json + track-record.json carry every field scripts/sync-db.mjs / the MCP read
     (reportId, scoredCadence, byAssetClass, byCadence, hitRate, ...).
  5. AGGREGATES: the track-record hit rates per instrument / asset-class / cadence are recomputed
     from the seeded ledger by hand and asserted to match export_content's output.

Fakes ONLY: boto3 (R2) and the repo .env read. Everything else -- config_loader, taxonomy,
load_catalog, load_track_record, _build_aggregates, publish.discover, R2Store -- is the real code.

Run:  python -m pytest tests/test_e2e_publish_distribute.py -q
"""
import csv
import io
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scripts  # noqa: F401  (defensive: apply the subpackage sys.path shim if run in isolation)
import export_content as EC
import publish as PUB
import _r2 as R2

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_ASSETS_JSON = REPO_ROOT / "config" / "assets.json"
REAL_BTC_BRIEF = REPO_ROOT / "tests" / "test_fixtures" / "BTC_research_brief.json"
REAL_BTC_ANALYSIS = REPO_ROOT / "tests" / "test_fixtures" / "BTC_analysis.json"

# The REAL outcome-ledger header (byte-identical to ledger/outcome_ledger.csv).
LEDGER_FIELDS = [
    "scored_at_utc", "report_id", "instrument", "view", "confidence", "window_end_utc",
    "results", "hits", "misses", "hit_rate_pct", "setup_filled", "setup_outcome", "partial",
    "conf_version", "conf_raw", "asset_class", "pred_type", "direction", "horizon", "market_regime",
]

# Object keys export_content advertises that are NOT a free*/preview URL: the Pro pair is conveyed
# only by hasPro=true, and scripts/sync-db.mjs derives BOTH keys from it (sync-db.mjs:108):
#   e.hasPro ? `${date}/${slug}/pro.html` : null,  e.hasPro ? `${date}/${slug}/pro.pdf` : null
PRO_KEYS = ("pro.html", "pro.pdf")


# --------------------------------------------------------------------------- #
# Fixture builders (a tmp ROOT shaped like the real engine working dir)
# --------------------------------------------------------------------------- #
def _ledger_row(**over):
    base = {k: "" for k in LEDGER_FIELDS}
    base.update({"scored_at_utc": "2026-06-22T00:00:00Z", "view": "neutral", "confidence": "60",
                 "results": "", "hits": "0", "misses": "0", "hit_rate_pct": "0"})
    base.update(over)
    return base


def _write_ledger(path, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=LEDGER_FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue(), encoding="utf-8")


def _write_edition(reports_dir, date, slug, meta, files):
    d = reports_dir / date / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    for name, body in files.items():
        (d / name).write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return d


def _write_predictions(pred_dir, report_id, payload):
    pred_dir.mkdir(parents=True, exist_ok=True)
    (pred_dir / f"{report_id}_predictions.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_root(tmp_path):
    root = tmp_path / "engine_root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "assets.json").write_text(
        REAL_ASSETS_JSON.read_text(encoding="utf-8-sig"), encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "data" / "predictions").mkdir(parents=True, exist_ok=True)
    return root


def _run_export(monkeypatch, root, *, include_dev=False, since=None):
    monkeypatch.setattr(EC, "ROOT", root)
    argv = ["export_content"]
    if include_dev:
        argv.append("--include-dev")
    if since:
        argv += ["--since", since]
    monkeypatch.setattr(sys, "argv", argv)
    EC.main()
    content = root / "content"
    catalog = json.loads((content / "catalog.json").read_text(encoding="utf-8"))
    track = json.loads((content / "track-record.json").read_text(encoding="utf-8"))
    return catalog, track


# --------------------------------------------------------------------------- #
# Fake boto3: ONE stateful in-memory R2 backend (publish uploads into it).
# --------------------------------------------------------------------------- #
class _FakeS3Client:
    def __init__(self, backend):
        self.backend = backend

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.backend.put_calls.append({"Bucket": Bucket, "Key": Key, "ContentType": ContentType})
        self.backend.objects[Key] = {"Body": Body, "ContentType": ContentType, "Bucket": Bucket}


class _FakeBoto3:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.client_kwargs = None

    def client(self, service, **kw):
        assert service == "s3"
        self.client_kwargs = kw
        return _FakeS3Client(self)


@pytest.fixture
def fake_r2(monkeypatch, tmp_path):
    """Inject a fake boto3 + R2_* env so the REAL R2Store.from_env() builds a working store.
    _r2.ROOT is pointed at an empty tmp dir so the real repo .env is never read (hermetic)."""
    backend = _FakeBoto3()
    monkeypatch.setitem(sys.modules, "boto3", backend)
    monkeypatch.setattr(R2, "ROOT", tmp_path / "no_env_here")
    monkeypatch.setenv("R2_ACCOUNT_ID", "acct")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "akid")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "assetframe-pro")
    return backend


def _key_from_asset_path(api_path):
    """Catalog advertises /api/report/<date>/<slug>/<file>; R2 stores <date>/<slug>/<file>."""
    assert api_path.startswith("/api/report/"), api_path
    return api_path[len("/api/report/"):]


def _advertised_keys(catalog):
    """Every R2 object key the web read-layer would request from a catalog edition: the existing
    free*/preview URLs PLUS the Pro pair derived from hasPro (mirrors scripts/sync-db.mjs)."""
    keys = set()
    for e in catalog:
        for field in ("freeHtml", "freePdf", "preview"):
            if e[field]:
                keys.add(_key_from_asset_path(e[field]))
        if e["hasPro"]:
            for name in PRO_KEYS:
                keys.add(f"{e['date']}/{e['slug']}/{name}")
    return keys


# --------------------------------------------------------------------------- #
# Canonical multi-cadence / multi-class pipeline seed (4 scored editions).
#
# Hand-computed truth (asserted below):
#   hits/misses per report: BTC 2/0, ETH 1/1, GBPUSD 3/1, GOLD 0/2
#   totals: reportsScored=4, predictionsGraded=10, hits=6 -> hitRate 60.0
# --------------------------------------------------------------------------- #
def _seed_pipeline(tmp_path):
    root = _seed_root(tmp_path)
    reports = root / "reports"
    pred = root / "data" / "predictions"

    # BTC metadata is sourced from the COMMITTED fixtures (honour the real brief/analysis shape).
    brief = json.loads(REAL_BTC_BRIEF.read_text(encoding="utf-8"))
    analysis = json.loads(REAL_BTC_ANALYSIS.read_text(encoding="utf-8"))
    btc_instrument = brief["instrument"]                 # "Bitcoin / US Dollar"
    btc_class = brief["asset_class_key"]                 # "crypto"

    # ---- BTC: daily, crypto, full edition WITH a Pro pair + a non-upload analysis.json payload ----
    _write_edition(reports, "2026-06-20", "BTC", {
        "instrument": btc_instrument, "ticker": brief["ticker"], "asset_class": btc_class,
        "status": brief["status"], "risk_rating": brief["risk"], "primary_bias": brief["primary_bias"],
        "last_price": analysis["last_price"], "data_quality_score": 0.91,
        "prediction_window_end_report_tz": "2026-06-21 01:00 BST",
        "report_date": "2026-06-20", "catalyst_status": "post-FOMC",
        "report_id": "AF-20260620-BTC", "forecast_window": brief["forecast_window"]
        if brief.get("forecast_window") else "rolling_24h",
        "chart_intervals": ["60m", "1d"],
        "data_provider": "yahoo", "data_license_mode": "personal", "data_license_degraded": False,
    }, files={
        "free.html": "<html>BTC free</html>", "free.pdf": b"%PDF-1.4 btc free",
        "preview.png": b"\x89PNG btc", "pro.html": "<html>BTC pro</html>",
        "pro.pdf": b"%PDF-1.4 btc pro",
        # a canonical analysis payload that is NOT an UPLOAD_FILES target -> must never reach R2.
        "analysis.json": REAL_BTC_ANALYSIS.read_text(encoding="utf-8"),
    })
    _write_predictions(pred, "AF-20260620-BTC", {
        "report_id": "AF-20260620-BTC", "instrument": btc_instrument, "symbol": "BTC",
        "view": "bearish", "confidence": 70, "window_end_utc": "2026-06-21T00:00:00Z",
        "taxonomy": {"prediction_type": "range_hold"},
        "predictions": [{"id": "P1", "type": "directional", "text": "holds band", "expect": True},
                        {"id": "P2", "type": "directional", "text": "no new ATH", "expect": False}],
    })

    # ---- ETH: daily, crypto, free-only (no Pro) ----
    _write_edition(reports, "2026-06-19", "ETH", {
        "instrument": "Ethereum", "ticker": "ETH", "asset_class": "crypto",
        "status": "published", "report_id": "AF-20260619-ETH", "forecast_window": "rolling_24h",
        "data_provider": "yahoo", "data_license_mode": "commercial", "data_license_degraded": True,
    }, files={"free.html": "<html>ETH free</html>", "free.pdf": b"%PDF-1.4 eth free",
              "preview.png": b"\x89PNG eth"})
    _write_predictions(pred, "AF-20260619-ETH", {
        "report_id": "AF-20260619-ETH", "instrument": "Ethereum", "symbol": "ETH",
        "view": "bullish", "confidence": 55, "window_end_utc": "2026-06-20T00:00:00Z",
        "taxonomy": {"prediction_type": "trend"},
        "predictions": [{"id": "P1", "type": "directional", "text": "breaks up", "expect": True},
                        {"id": "P2", "type": "directional", "text": "no breakdown", "expect": False}],
    })

    # ---- GBPUSD: WEEKLY, fx, PARTIAL edition (free.html only -> dead-link gate under test) ----
    _write_edition(reports, "2026-06-18", "GBPUSD", {
        "instrument": "British Pound / US Dollar", "ticker": "GBPUSD", "asset_class": "fx",
        "status": "published", "report_id": "AF-2026W25-GBPUSD",
        "forecast_window": "next_liquid_session",
        "data_provider": "yahoo", "data_license_mode": "personal", "data_license_degraded": False,
    }, files={"free.html": "<html>GBPUSD free</html>"})   # no free.pdf / preview / pro
    _write_predictions(pred, "AF-2026W25-GBPUSD", {
        "report_id": "AF-2026W25-GBPUSD", "instrument": "British Pound / US Dollar", "symbol": "GBPUSD",
        "view": "bullish", "confidence": 65, "window_end_utc": "2026-06-19T00:00:00Z",
        "taxonomy": {"prediction_type": "trend"},
        "predictions": [{"id": "P1", "type": "directional", "text": "a", "expect": True},
                        {"id": "P2", "type": "directional", "text": "b", "expect": True},
                        {"id": "P3", "type": "directional", "text": "c", "expect": True},
                        {"id": "P4", "type": "directional", "text": "d", "expect": False}],
    })

    # ---- GOLD: MONTHLY, commodity, full edition WITH a Pro pair ----
    _write_edition(reports, "2026-06-15", "GOLD", {
        "instrument": "Gold", "ticker": "GOLD", "asset_class": "commodity",
        "status": "published", "report_id": "AF-202606-GOLD",
        "forecast_window": "next_liquid_session",
        "data_provider": "yahoo", "data_license_mode": "personal", "data_license_degraded": False,
    }, files={"free.html": "<html>GOLD free</html>", "free.pdf": b"%PDF gold free",
              "preview.png": b"\x89PNG gold", "pro.html": "<html>GOLD pro</html>",
              "pro.pdf": b"%PDF gold pro"})
    _write_predictions(pred, "AF-202606-GOLD", {
        "report_id": "AF-202606-GOLD", "instrument": "Gold", "symbol": "GOLD",
        "view": "bearish", "confidence": 60, "window_end_utc": "2026-06-17T00:00:00Z",
        "taxonomy": {"prediction_type": "range_hold"},
        "predictions": [{"id": "P1", "type": "directional", "text": "x", "expect": False},
                        {"id": "P2", "type": "directional", "text": "y", "expect": False}],
    })

    # ---- Ledger: one scored row per edition (asset_class populated so byAssetClass aggregates) ----
    _write_ledger(root / "ledger" / "outcome_ledger.csv", [
        _ledger_row(report_id="AF-20260620-BTC", instrument=btc_instrument, view="bearish",
                    confidence="70", window_end_utc="2026-06-21T00:00:00Z",
                    results="P1=Y P2=Y", hits="2", misses="0", hit_rate_pct="100",
                    asset_class="crypto", pred_type="range_hold", market_regime="trend_down"),
        _ledger_row(report_id="AF-20260619-ETH", instrument="Ethereum", view="bullish",
                    confidence="55", window_end_utc="2026-06-20T00:00:00Z",
                    results="P1=Y P2=N", hits="1", misses="1", hit_rate_pct="50",
                    asset_class="crypto", pred_type="trend", market_regime="ranging"),
        _ledger_row(report_id="AF-2026W25-GBPUSD", instrument="British Pound / US Dollar",
                    view="bullish", confidence="65", window_end_utc="2026-06-19T00:00:00Z",
                    results="P1=Y P2=Y P3=Y P4=N", hits="3", misses="1", hit_rate_pct="75",
                    asset_class="fx", pred_type="trend", market_regime="breakout"),
        _ledger_row(report_id="AF-202606-GOLD", instrument="Gold", view="bearish",
                    confidence="60", window_end_utc="2026-06-17T00:00:00Z",
                    results="P1=N P2=N", hits="0", misses="2", hit_rate_pct="0",
                    asset_class="commodity", pred_type="range_hold", market_regime="ranging"),
    ])
    return root


# Expected R2 keys publish should upload from the seed above (free*/preview/pro that EXIST on disk).
EXPECTED_UPLOADS = {
    "2026-06-20/BTC/free.html", "2026-06-20/BTC/free.pdf", "2026-06-20/BTC/preview.png",
    "2026-06-20/BTC/pro.html", "2026-06-20/BTC/pro.pdf",
    "2026-06-19/ETH/free.html", "2026-06-19/ETH/free.pdf", "2026-06-19/ETH/preview.png",
    "2026-06-18/GBPUSD/free.html",
    "2026-06-15/GOLD/free.html", "2026-06-15/GOLD/free.pdf", "2026-06-15/GOLD/preview.png",
    "2026-06-15/GOLD/pro.html", "2026-06-15/GOLD/pro.pdf",
}


def _publish(monkeypatch, root, argv=("publish",)):
    monkeypatch.setattr(PUB, "REPORTS", root / "reports")
    monkeypatch.setattr(sys, "argv", list(argv))
    PUB.main()


# =========================================================================== #
# 1. The full chain wired together + the no-dead-links contract GATE.
# =========================================================================== #
class TestFullChainPublishGate:
    def test_export_writes_both_contract_files(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        catalog, track = _run_export(monkeypatch, root)
        # The two JSON files the web /api/v1 + MCP layer read.
        assert (root / "content" / "catalog.json").exists()
        assert (root / "content" / "track-record.json").exists()
        assert isinstance(catalog, list) and len(catalog) == 4
        assert {e["slug"] for e in catalog} == {"BTC", "ETH", "GBPUSD", "GOLD"}

    def test_uploaded_keys_exactly_match_catalog_advertised(self, tmp_path, monkeypatch, fake_r2):
        # THE GATE: every key publish uploads == every key the catalog advertises. No 404, no orphan.
        root = _seed_pipeline(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)
        _publish(monkeypatch, root)

        uploaded = set(fake_r2.objects)
        assert uploaded == EXPECTED_UPLOADS
        # Both directions of the contract in one equality:
        assert uploaded == _advertised_keys(catalog)

        # And spell out the per-edition existence gates the equality encodes.
        by_slug = {e["slug"]: e for e in catalog}
        for field in ("freeHtml", "freePdf", "preview"):
            for e in catalog:
                if e[field]:
                    assert _key_from_asset_path(e[field]) in uploaded, (e["slug"], field)
        # GBPUSD partial: only free.html advertised; the would-be pdf/preview keys are NOT in R2.
        gbp = by_slug["GBPUSD"]
        assert gbp["freePdf"] == "" and gbp["preview"] == "" and gbp["hasPro"] is False
        assert "2026-06-18/GBPUSD/free.pdf" not in uploaded
        assert "2026-06-18/GBPUSD/preview.png" not in uploaded
        # Pro pair present only where hasPro.
        for slug in ("BTC", "GOLD"):
            d = by_slug[slug]["date"]
            assert {f"{d}/{slug}/pro.html", f"{d}/{slug}/pro.pdf"} <= uploaded
        assert not any(k.endswith("/ETH/pro.html") for k in uploaded)

        # Real from_env() wired env -> boto3 client; everything landed in the private bucket.
        assert fake_r2.client_kwargs["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"
        assert {c["Bucket"] for c in fake_r2.put_calls} == {"assetframe-pro"}

    def test_non_upload_payload_never_reaches_r2(self, tmp_path, monkeypatch, fake_r2):
        # BTC's analysis.json sits in the edition dir but is not an UPLOAD_FILES target.
        root = _seed_pipeline(tmp_path)
        _run_export(monkeypatch, root)
        _publish(monkeypatch, root)
        assert not any(k.endswith("analysis.json") for k in fake_r2.objects)
        assert not any(k.endswith("metadata.json") for k in fake_r2.objects)

    def test_uploaded_bytes_and_content_types_round_trip(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_pipeline(tmp_path)
        _publish(monkeypatch, root)
        o = fake_r2.objects
        assert o["2026-06-20/BTC/free.pdf"]["Body"] == b"%PDF-1.4 btc free"
        assert o["2026-06-20/BTC/free.pdf"]["ContentType"] == "application/pdf"
        assert o["2026-06-20/BTC/free.html"]["Body"] == b"<html>BTC free</html>"
        assert o["2026-06-20/BTC/free.html"]["ContentType"] == "text/html; charset=utf-8"
        assert o["2026-06-20/BTC/preview.png"]["ContentType"] == "image/png"

    def test_date_filter_publishes_single_edition_consistently(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_pipeline(tmp_path)
        _publish(monkeypatch, root, argv=("publish", "--date", "2026-06-19"))
        assert set(fake_r2.objects) == {
            "2026-06-19/ETH/free.html", "2026-06-19/ETH/free.pdf", "2026-06-19/ETH/preview.png"}


# =========================================================================== #
# 2. catalog.json SHAPE -- every field scripts/sync-db.mjs / the MCP consume.
# =========================================================================== #
CATALOG_REQUIRED_FIELDS = {
    "date", "slug", "instrument", "ticker", "assetClass", "status", "risk", "bias", "lastPrice",
    "dataQuality", "windowEnd", "reportDate", "catalystStatus", "reportId", "scoredCadence",
    "chartIntervals", "forecastWindow", "dataProvider", "dataLicense", "dataLicenseDegraded",
    "hidden", "freeHtml", "freePdf", "preview", "hasPro",
}


class TestCatalogShapeContract:
    def test_every_edition_has_full_contract_field_set(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)
        for e in catalog:
            missing = CATALOG_REQUIRED_FIELDS - set(e)
            assert not missing, (e["slug"], missing)
            # internal-only join field MUST be stripped before the web JSON is written.
            assert "_dir" not in e

    def test_cadence_join_keys_and_provenance(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)
        by_slug = {e["slug"]: e for e in catalog}
        # scoredCadence is the cadence-aware join key the web groups the track record by.
        assert by_slug["BTC"]["scoredCadence"] == "daily"        # AF-20260620 (8-digit)
        assert by_slug["GBPUSD"]["scoredCadence"] == "weekly"    # AF-2026W25 (ISO week)
        assert by_slug["GOLD"]["scoredCadence"] == "monthly"     # AF-202606 (6-digit)
        assert by_slug["BTC"]["reportId"] == "AF-20260620-BTC"
        # Provenance badge fields carried straight from metadata.
        assert by_slug["ETH"]["dataLicense"] == "commercial" and by_slug["ETH"]["dataLicenseDegraded"] is True
        assert by_slug["BTC"]["dataLicense"] == "personal" and by_slug["BTC"]["dataLicenseDegraded"] is False

    def test_fixture_sourced_metadata_flows_into_catalog(self, tmp_path, monkeypatch):
        # The committed BTC brief/analysis values survive the export unchanged.
        brief = json.loads(REAL_BTC_BRIEF.read_text(encoding="utf-8"))
        analysis = json.loads(REAL_BTC_ANALYSIS.read_text(encoding="utf-8"))
        root = _seed_pipeline(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)
        btc = {e["slug"]: e for e in catalog}["BTC"]
        assert btc["instrument"] == brief["instrument"]
        assert btc["assetClass"] == brief["asset_class_key"] == "crypto"
        assert btc["lastPrice"] == analysis["last_price"]
        assert btc["bias"] == brief["primary_bias"]

    def test_real_universe_is_approval_gated(self, tmp_path, monkeypatch):
        # Every asset in the REAL config/assets.json is approval_required -> all editions hidden.
        root = _seed_pipeline(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)
        assert all(e["hidden"] is True for e in catalog)


# =========================================================================== #
# 3. track-record.json SHAPE -- the analytics contract the MCP/track-record API read.
# =========================================================================== #
TRACK_TOP_KEYS = {
    "stats", "open", "scored", "calibration", "byInstrument", "byAssetClass", "byPredictionType",
    "byRegime", "byCadence", "timeline", "calibrationCurve", "componentVsOutcome",
}
SCORED_ROW_KEYS = {
    "reportId", "instrument", "view", "confidence", "results", "hits", "misses", "hitRate",
    "windowEnd", "assetClass", "predType", "scoredCadence",
}
OPEN_CALL_KEYS = {
    "reportId", "instrument", "symbol", "view", "confidence", "windowEnd", "n", "nManual",
    "hits", "scored", "predictions",
}
PRED_KEYS = {"id", "type", "text", "manual", "expect", "verdict", "predType"}


class TestTrackRecordShapeContract:
    def test_top_level_and_stats_keys(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        assert TRACK_TOP_KEYS <= set(track)
        assert set(track["stats"]) == {"reportsScored", "openCalls", "predictionsGraded", "hitRate"}

    def test_scored_row_shape(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        assert len(track["scored"]) == 4
        for r in track["scored"]:
            assert SCORED_ROW_KEYS <= set(r), set(r)
        btc = {r["reportId"]: r for r in track["scored"]}["AF-20260620-BTC"]
        assert btc["scoredCadence"] == "daily" and btc["assetClass"] == "crypto"
        assert btc["predType"] == "range_hold"

    def test_open_call_and_prediction_shape_with_verdicts(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        oc = {c["reportId"]: c for c in track["open"]}
        assert set(oc) == {"AF-20260620-BTC", "AF-20260619-ETH",
                           "AF-2026W25-GBPUSD", "AF-202606-GOLD"}
        for c in track["open"]:
            assert OPEN_CALL_KEYS <= set(c), set(c)
            for p in c["predictions"]:
                assert PRED_KEYS <= set(p), set(p)
        # Per-prediction verdicts parsed from the ledger's packed `results` string.
        btc = oc["AF-20260620-BTC"]
        assert btc["scored"] is True and btc["hits"] == 2 and btc["n"] == 2
        assert {p["id"]: p["verdict"] for p in btc["predictions"]} == {"P1": "Y", "P2": "Y"}
        gbp = oc["AF-2026W25-GBPUSD"]
        assert {p["id"]: p["verdict"] for p in gbp["predictions"]} == {
            "P1": "Y", "P2": "Y", "P3": "Y", "P4": "N"}


# =========================================================================== #
# 4. AGGREGATES recomputed from the seeded ledger must match export_content.
# =========================================================================== #
class TestAggregatesMatchLedger:
    def test_headline_stats(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        s = track["stats"]
        assert s["reportsScored"] == 4
        assert s["openCalls"] == 4
        assert s["predictionsGraded"] == 10            # 2 + 2 + 4 + 2 graded
        assert s["hitRate"] == 60.0                    # 6 hits / 10 graded

    def test_by_instrument_hit_rates_and_backfilled_ticker(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        bi = {r["instrument"]: r for r in track["byInstrument"]}
        assert bi["Bitcoin / US Dollar"]["hitRate"] == 100.0
        assert bi["Ethereum"]["hitRate"] == 50.0
        assert bi["British Pound / US Dollar"]["hitRate"] == 75.0
        assert bi["Gold"]["hitRate"] == 0.0
        # ticker is NOT in the ledger -> backfilled from the predictions `symbol` (cross-module join).
        assert bi["Bitcoin / US Dollar"]["ticker"] == "BTC"
        assert bi["British Pound / US Dollar"]["ticker"] == "GBPUSD"
        assert bi["Gold"]["ticker"] == "GOLD"
        assert bi["Bitcoin / US Dollar"]["assetClass"] == "crypto"

    def test_by_asset_class_hit_rates(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        ac = {r["assetClass"]: r for r in track["byAssetClass"]}
        assert ac["crypto"]["reportsScored"] == 2 and ac["crypto"]["hitRate"] == 75.0   # (2+1)/(2+2)
        assert ac["fx"]["reportsScored"] == 1 and ac["fx"]["hitRate"] == 75.0           # 3/4
        assert ac["commodity"]["reportsScored"] == 1 and ac["commodity"]["hitRate"] == 0.0

    def test_by_cadence_hit_rates(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        cad = {r["cadence"]: r for r in track["byCadence"]}
        assert set(cad) == {"daily", "weekly", "monthly"}
        assert cad["daily"]["reportsScored"] == 2 and cad["daily"]["hitRate"] == 75.0    # (2+1)/(2+2)
        assert cad["weekly"]["reportsScored"] == 1 and cad["weekly"]["hitRate"] == 75.0  # 3/4
        assert cad["monthly"]["reportsScored"] == 1 and cad["monthly"]["hitRate"] == 0.0

    def test_by_prediction_type_and_regime(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        pt = {r["predType"]: r for r in track["byPredictionType"]}
        assert pt["range_hold"]["hitRate"] == 50.0     # BTC 2/0 + GOLD 0/2 -> 2/4
        assert pt["trend"]["hitRate"] == 66.7          # ETH 1/1 + GBPUSD 3/1 -> 4/6
        rg = {r["regime"]: r for r in track["byRegime"]}
        assert rg["trend_down"]["hitRate"] == 100.0
        assert rg["ranging"]["hitRate"] == 25.0        # ETH 1/1 + GOLD 0/2 -> 1/4
        assert rg["breakout"]["hitRate"] == 75.0

    def test_component_vs_outcome_bands(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        cvo = {b["band"]: b for b in track["componentVsOutcome"]}
        # conf 70/65 -> Elevated band; conf 55/60 -> Moderate band.
        assert cvo["Elevated"]["hitRate"] == 83.3 and cvo["Elevated"]["avgConfidence"] == 67.5
        assert cvo["Moderate"]["hitRate"] == 25.0 and cvo["Moderate"]["avgConfidence"] == 57.5

    def test_timeline_cumulative_converges_to_headline(self, tmp_path, monkeypatch):
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        tl = track["timeline"]
        assert len(tl) == 4
        # chronological by window_end_utc: GOLD(06-17), GBPUSD(06-19), ETH(06-20), BTC(06-21)
        assert [t["reportId"] for t in tl] == [
            "AF-202606-GOLD", "AF-2026W25-GBPUSD", "AF-20260619-ETH", "AF-20260620-BTC"]
        assert tl[-1]["cumulativeHitRate"] == 60.0     # equals the headline hitRate
        assert tl[0]["perReportHitRate"] == 0.0 and tl[-1]["perReportHitRate"] == 100.0

    def test_calibration_gated_below_ten_rows(self, tmp_path, monkeypatch):
        # Only 4 scored rows (< 10) -> calibration + the fine curve stay empty (n-gate).
        root = _seed_pipeline(tmp_path)
        _, track = _run_export(monkeypatch, root)
        assert track["calibration"] is None
        assert track["calibrationCurve"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
