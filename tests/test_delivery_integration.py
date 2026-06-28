"""Phase 2 INTEGRATION tests for scripts/delivery/* wired together.

Unlike tests/test_delivery_unit.py (which exercises each helper in isolation), these tests
drive the REAL cross-module delivery flow against a tmp ROOT:

  1. export_content.main() consumes a REAL report tree (reports/<date>/<ASSET>/metadata.json +
     payload files), the REAL config/assets.json universe (via the REAL config_loader), a REAL
     outcome ledger CSV and REAL data/predictions/*.json, and writes content/catalog.json +
     content/track-record.json -- exactly the files the Next.js web app reads.
  2. publish.main() and r2_purge.main() then run over the SAME report tree, through the REAL
     _r2.R2Store.from_env() wiring, with the ONLY external boundary faked: boto3. A single
     stateful in-memory R2 backend is injected as the `boto3` module, so publish UPLOADS into it
     and r2_purge LISTS/DELETES from it -- a true round-trip.

The data-contract under test is the seam between the two halves: every asset path the catalog
advertises (/api/report/<date>/<slug>/<file>) must map to an R2 object key (<date>/<slug>/<file>)
that publish actually uploads, and byInstrument ticker/assetClass in track-record.json is
backfilled by main() by JOINING the ledger aggregates with the predictions + catalog produced by
the OTHER modules -- a path the unit tests never cover.

Run:  python -m pytest tests/test_delivery_integration.py -q
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
import r2_purge as PURGE
import _r2 as R2

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_ASSETS_JSON = REPO_ROOT / "config" / "assets.json"

# The REAL outcome-ledger header (mirrors ledger/outcome_ledger.csv exactly), so the CSV the
# integration feeds export_content is byte-shaped like production, not the simplified unit header.
LEDGER_FIELDS = [
    "scored_at_utc", "report_id", "instrument", "view", "confidence", "window_end_utc",
    "results", "hits", "misses", "hit_rate_pct", "setup_filled", "setup_outcome", "partial",
    "conf_version", "conf_raw", "asset_class", "pred_type", "direction", "horizon", "market_regime",
]


# --------------------------------------------------------------------------- #
# Fixture builders (a tmp ROOT that looks like the real engine working dir)
# --------------------------------------------------------------------------- #
def _ledger_row(**over):
    base = {k: "" for k in LEDGER_FIELDS}
    base.update({
        "scored_at_utc": "2026-06-22T00:00:00Z", "view": "neutral", "confidence": "60",
        "results": "", "hits": "0", "misses": "0", "hit_rate_pct": "0",
        "pred_type": "trend", "horizon": "next_session", "market_regime": "ranging",
    })
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
    """Create reports/<date>/<slug>/ with metadata.json + the given report 'payload' files."""
    d = reports_dir / date / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    for name, body in files.items():
        (d / name).write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return d


def _write_predictions(pred_dir, report_id, payload):
    pred_dir.mkdir(parents=True, exist_ok=True)
    (pred_dir / f"{report_id}_predictions.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_root(tmp_path, assets_json_path=REAL_ASSETS_JSON):
    """Lay out a tmp ROOT: config/, reports/, data/predictions/, ledger/. Returns the ROOT."""
    root = tmp_path / "engine_root"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "assets.json").write_text(
        Path(assets_json_path).read_text(encoding="utf-8-sig"), encoding="utf-8")
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "data" / "predictions").mkdir(parents=True, exist_ok=True)
    return root


def _run_export(monkeypatch, root, *, include_dev=False, since=None):
    """Run export_content.main() against `root` and return (catalog, track) as parsed JSON."""
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
# Fake boto3: ONE stateful in-memory R2 backend shared by publish + r2_purge.
# --------------------------------------------------------------------------- #
class _FakeS3Client:
    def __init__(self, backend):
        self.backend = backend

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.backend.put_calls.append({"Bucket": Bucket, "Key": Key, "ContentType": ContentType})
        self.backend.objects[Key] = {"Body": Body, "ContentType": ContentType, "Bucket": Bucket}

    def list_objects_v2(self, **kw):
        self.backend.list_calls.append(kw)
        prefix = kw.get("Prefix", "")
        keys = sorted(k for k in self.backend.objects if k.startswith(prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}

    def delete_objects(self, *, Bucket, Delete):
        self.backend.delete_calls.append(Delete)
        for obj in Delete.get("Objects", []):
            self.backend.objects.pop(obj["Key"], None)
        return {"Errors": []}


class _FakeBoto3:
    """Stand-in for the `boto3` module: `.client("s3", ...)` -> a client over a shared backend."""
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.list_calls = []
        self.delete_calls = []
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
    """Catalog advertises /api/report/<date>/<slug>/<file>; R2 stores it under <date>/<slug>/<file>."""
    assert api_path.startswith("/api/report/"), api_path
    return api_path[len("/api/report/"):]


# --------------------------------------------------------------------------- #
# Canonical two-edition scenario shared by several tests.
# --------------------------------------------------------------------------- #
def _seed_canonical(tmp_path):
    root = _seed_root(tmp_path)
    reports = root / "reports"

    # BTC: full edition WITH a Pro pair + an extra payload.json that is NOT an upload target.
    _write_edition(reports, "2026-06-20", "BTC", {
        "instrument": "Bitcoin", "ticker": "BTC", "asset_class": "crypto",
        "status": "published", "risk_rating": "High", "primary_bias": "bearish lean",
        "last_price": 64344.26, "data_quality_score": 0.91,
        "prediction_window_end_report_tz": "2026-06-21 01:00 BST",
        "report_date": "2026-06-20", "catalyst_status": "post-FOMC",
        "report_id": "AF-20260620-BTC", "forecast_window": "rolling_24h",
        "chart_intervals": ["60m", "1d"],
        "data_provider": "yahoo", "data_license_mode": "personal", "data_license_degraded": False,
    }, files={
        "free.html": "<html>BTC free</html>", "free.pdf": b"%PDF-1.4 btc free",
        "preview.png": b"\x89PNG btc", "pro.html": "<html>BTC pro</html>",
        "pro.pdf": b"%PDF-1.4 btc pro", "payload.json": json.dumps({"canonical": "payload"}),
    })
    # ETH: free-only edition (no Pro pair).
    _write_edition(reports, "2026-06-19", "ETH", {
        "instrument": "Ethereum", "ticker": "ETH", "asset_class": "crypto",
        "status": "published", "report_id": "AF-20260619-ETH", "forecast_window": "rolling_24h",
        "data_provider": "yahoo", "data_license_mode": "commercial", "data_license_degraded": True,
    }, files={
        "free.html": "<html>ETH free</html>", "free.pdf": b"%PDF-1.4 eth free",
        "preview.png": b"\x89PNG eth",
    })

    # Ledger: BTC scored (asset_class left BLANK on purpose -> forces the catalog backfill path),
    # ETH scored (carries its own asset_class).
    _write_ledger(root / "ledger" / "outcome_ledger.csv", [
        _ledger_row(report_id="AF-20260620-BTC", instrument="Bitcoin", view="bearish",
                    confidence="70", window_end_utc="2026-06-21T00:00:00Z",
                    results="P1=Y P2=Y", hits="2", misses="0", hit_rate_pct="100",
                    asset_class="", pred_type="range_hold", market_regime="trend_down"),
        _ledger_row(report_id="AF-20260619-ETH", instrument="Ethereum", view="bullish",
                    confidence="55", window_end_utc="2026-06-20T00:00:00Z",
                    results="P1=N", hits="0", misses="1", hit_rate_pct="0",
                    asset_class="crypto", pred_type="trend", market_regime="ranging"),
    ])

    pred = root / "data" / "predictions"
    _write_predictions(pred, "AF-20260620-BTC", {
        "report_id": "AF-20260620-BTC", "instrument": "Bitcoin", "symbol": "BTC",
        "view": "bearish", "confidence": 70, "window_end_utc": "2026-06-21T00:00:00Z",
        "taxonomy": {"prediction_type": "range_hold"},
        "predictions": [
            {"id": "P1", "type": "directional", "text": "holds the band", "expect": True},
            {"id": "P2", "type": "directional", "text": "no new ATH", "expect": False},
        ],
    })
    _write_predictions(pred, "AF-20260619-ETH", {
        "report_id": "AF-20260619-ETH", "instrument": "Ethereum", "symbol": "ETH",
        "view": "bullish", "confidence": 55, "window_end_utc": "2026-06-20T00:00:00Z",
        "taxonomy": {"prediction_type": "trend"},
        "predictions": [
            {"id": "P1", "type": "directional", "text": "breaks higher", "expect": True},
        ],
    })
    return root


# --------------------------------------------------------------------------- #
# 1. export_content.main() end-to-end (catalog + track-record contracts)
# --------------------------------------------------------------------------- #
class TestExportContentEndToEnd:
    def test_catalog_shape_and_real_universe_approval_gate(self, tmp_path, monkeypatch):
        root = _seed_canonical(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)

        assert [e["slug"] for e in catalog] == ["BTC", "ETH"]  # reverse sort by (date, slug)
        by_slug = {e["slug"]: e for e in catalog}
        btc, eth = by_slug["BTC"], by_slug["ETH"]

        # _dir is an internal field that main() MUST strip before writing the web JSON.
        assert "_dir" not in btc and "_dir" not in eth

        # Real universe is ALL approval_required -> every edition lands hidden (fail-safe gate).
        assert btc["hidden"] is True and eth["hidden"] is True

        # hasPro reflects pro.html on disk (BTC has it, ETH does not).
        assert btc["hasPro"] is True and eth["hasPro"] is False

        # Provenance + cadence carried through from metadata.
        assert btc["dataLicense"] == "personal" and btc["dataLicenseDegraded"] is False
        assert eth["dataLicense"] == "commercial" and eth["dataLicenseDegraded"] is True
        assert btc["scoredCadence"] == "daily"        # AF-20260620 -> 8-digit -> daily
        assert btc["assetClass"] == "crypto"

    def test_track_record_cross_module_backfill(self, tmp_path, monkeypatch):
        # The headline integration: byInstrument ticker/assetClass are NOT in the ledger; main()
        # backfills them by joining the ledger aggregates with predictions (symbol) + catalog
        # (assetClass), keyed by instrument NAME. Unit tests call _build_aggregates directly and
        # therefore can't see this join.
        root = _seed_canonical(tmp_path)
        _, track = _run_export(monkeypatch, root)

        assert track["stats"]["reportsScored"] == 2
        assert track["stats"]["predictionsGraded"] == 3          # 2 + 0 + 0 + 1
        assert track["stats"]["hitRate"] == round(100 * 2 / 3, 1)  # 66.7

        by_inst = {r["instrument"]: r for r in track["byInstrument"]}
        # Bitcoin's ledger row had asset_class="" and the ledger never carries a ticker:
        # both are backfilled from the OTHER modules.
        assert by_inst["Bitcoin"]["ticker"] == "BTC"             # <- from predictions symbol
        assert by_inst["Bitcoin"]["assetClass"] == "crypto"      # <- from catalog (ledger was blank)
        assert by_inst["Ethereum"]["ticker"] == "ETH"
        assert by_inst["Ethereum"]["assetClass"] == "crypto"     # carried by the ledger itself

    def test_open_calls_join_ledger_verdicts_and_scored_flag(self, tmp_path, monkeypatch):
        root = _seed_canonical(tmp_path)
        _, track = _run_export(monkeypatch, root)
        oc = {c["reportId"]: c for c in track["open"]}

        btc = oc["AF-20260620-BTC"]
        assert btc["scored"] is True            # report_id present in the ledger -> scored
        assert btc["hits"] == 2                 # hits_by_id merged from the ledger
        assert btc["n"] == 2 and btc["nManual"] == 0
        # Per-prediction verdict parsed from the ledger's packed `results` string ("P1=Y P2=Y").
        verdicts = {p["id"]: p["verdict"] for p in btc["predictions"]}
        assert verdicts == {"P1": "Y", "P2": "Y"}

        eth = oc["AF-20260619-ETH"]
        assert eth["scored"] is True and eth["hits"] == 0
        assert eth["predictions"][0]["verdict"] == "N"   # ETH ledger results was "P1=N"

    def test_since_filter_scopes_catalog_only(self, tmp_path, monkeypatch):
        root = _seed_canonical(tmp_path)
        catalog, _ = _run_export(monkeypatch, root, since="2026-06-20")
        # ETH (2026-06-19) is older than the cutoff -> excluded from the catalog.
        assert [e["slug"] for e in catalog] == ["BTC"]


# --------------------------------------------------------------------------- #
# 2. publish.main() over the SAME tree, through real R2Store + fake boto3.
# --------------------------------------------------------------------------- #
class TestPublishRoundTrip:
    def test_uploaded_keys_match_catalog_asset_paths(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_canonical(tmp_path)
        catalog, _ = _run_export(monkeypatch, root)

        # publish reads the SAME reports tree export_content read.
        monkeypatch.setattr(PUB, "REPORTS", root / "reports")
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()

        uploaded = set(fake_r2.objects)
        assert uploaded == {
            "2026-06-20/BTC/free.html", "2026-06-20/BTC/free.pdf", "2026-06-20/BTC/preview.png",
            "2026-06-20/BTC/pro.html", "2026-06-20/BTC/pro.pdf",
            "2026-06-19/ETH/free.html", "2026-06-19/ETH/free.pdf", "2026-06-19/ETH/preview.png",
        }
        # The non-upload payload.json present on disk must NEVER reach R2.
        assert not any(k.endswith("payload.json") for k in uploaded)

        # CONTRACT: every asset path the catalog advertises maps to an uploaded R2 key.
        for e in catalog:
            for field in ("freeHtml", "freePdf", "preview"):
                assert _key_from_asset_path(e[field]) in uploaded, (e["slug"], field)
            pro_key = f"{e['date']}/{e['slug']}/pro.html"
            assert (pro_key in uploaded) == e["hasPro"]

        # The real from_env() wired the env -> boto3 client correctly.
        assert fake_r2.client_kwargs["endpoint_url"] == "https://acct.r2.cloudflarestorage.com"
        assert {c["Bucket"] for c in fake_r2.put_calls} == {"assetframe-pro"}

    def test_uploaded_bodies_and_content_types_round_trip(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_canonical(tmp_path)
        monkeypatch.setattr(PUB, "REPORTS", root / "reports")
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()

        # publish must stream the on-disk bytes under the UPLOAD_FILES content type.
        assert fake_r2.objects["2026-06-20/BTC/free.pdf"]["ContentType"] == "application/pdf"
        assert fake_r2.objects["2026-06-20/BTC/free.pdf"]["Body"] == b"%PDF-1.4 btc free"
        assert fake_r2.objects["2026-06-20/BTC/free.html"]["ContentType"] == "text/html; charset=utf-8"
        assert fake_r2.objects["2026-06-20/BTC/free.html"]["Body"] == b"<html>BTC free</html>"

    def test_publish_then_purge_full_round_trip(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_canonical(tmp_path)
        monkeypatch.setattr(PUB, "REPORTS", root / "reports")

        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        assert len(fake_r2.objects) == 8

        # Dry-run purge LISTS from the same backend publish populated, deletes nothing.
        monkeypatch.setattr(sys, "argv", ["r2_purge"])
        PURGE.main()
        assert len(fake_r2.objects) == 8
        assert fake_r2.delete_calls == []

        # Prefix purge removes only the BTC date.
        monkeypatch.setattr(sys, "argv", ["r2_purge", "--prefix", "2026-06-20/", "--yes"])
        PURGE.main()
        assert set(fake_r2.objects) == {
            "2026-06-19/ETH/free.html", "2026-06-19/ETH/free.pdf", "2026-06-19/ETH/preview.png"}

        # Full purge clears the rest.
        monkeypatch.setattr(sys, "argv", ["r2_purge", "--yes"])
        PURGE.main()
        assert fake_r2.objects == {}

    def test_date_filter_publishes_single_edition(self, tmp_path, monkeypatch, fake_r2):
        root = _seed_canonical(tmp_path)
        monkeypatch.setattr(PUB, "REPORTS", root / "reports")
        monkeypatch.setattr(sys, "argv", ["publish", "--date", "2026-06-19"])
        PUB.main()
        assert set(fake_r2.objects) == {
            "2026-06-19/ETH/free.html", "2026-06-19/ETH/free.pdf", "2026-06-19/ETH/preview.png"}


# --------------------------------------------------------------------------- #
# 3. Cross-module edge cases only visible when the modules combine.
# --------------------------------------------------------------------------- #
class TestCrossModuleEdges:
    def test_dev_edition_excluded_from_both_catalog_and_publish(self, tmp_path, monkeypatch,
                                                                fake_r2):
        # A "_"-prefixed dev edition: export (no --include-dev) hides it AND publish skips it,
        # so the two halves agree -- nothing dev ever reaches the web catalog or R2.
        root = _seed_root(tmp_path)
        reports = root / "reports"
        _write_edition(reports, "_dev", "XYZ", {"instrument": "Dev", "ticker": "XYZ"},
                       files={"free.html": "<html>dev</html>"})
        _write_edition(reports, "2026-06-20", "BTC", {"instrument": "Bitcoin", "ticker": "BTC"},
                       files={"free.html": "<html>btc</html>"})
        _write_ledger(root / "ledger" / "outcome_ledger.csv", [])

        catalog, _ = _run_export(monkeypatch, root)
        assert [e["slug"] for e in catalog] == ["BTC"]   # dev excluded from the catalog

        monkeypatch.setattr(PUB, "REPORTS", reports)
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        assert set(fake_r2.objects) == {"2026-06-20/BTC/free.html"}   # dev never uploaded

    def test_include_dev_catalog_lists_edition_publish_never_uploads(self, tmp_path, monkeypatch,
                                                                     fake_r2):
        # DIVERGENCE (documented, intended): with --include-dev the catalog DOES list a _dev
        # edition, but publish ALWAYS skips "_" dates -> the catalog references R2 objects that
        # are never uploaded. Acceptable because _dev is a local-inspection-only convention.
        root = _seed_root(tmp_path)
        reports = root / "reports"
        _write_edition(reports, "_dev", "XYZ", {"instrument": "Dev", "ticker": "XYZ"},
                       files={"free.html": "<html>dev</html>"})
        _write_ledger(root / "ledger" / "outcome_ledger.csv", [])

        catalog, _ = _run_export(monkeypatch, root, include_dev=True)
        assert [e["slug"] for e in catalog] == ["XYZ"]   # dev IS listed with --include-dev
        dev = catalog[0]

        monkeypatch.setattr(PUB, "REPORTS", reports)
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        # The advertised free.html key is NOT in R2 -> the web would 404 on a dev edition.
        assert _key_from_asset_path(dev["freeHtml"]) not in fake_r2.objects

    def test_catalog_advertises_only_existing_free_assets(self, tmp_path, monkeypatch, fake_r2):
        # FIXED: load_catalog now gates freeHtml/freePdf/preview on existence (mirroring hasPro), so a
        # partial edition (metadata + free.html, PDF/preview render having failed) advertises EXACTLY
        # the files publish.discover uploads — no dead R2 links / 404s.
        root = _seed_root(tmp_path)
        reports = root / "reports"
        _write_edition(reports, "2026-06-20", "PARTIAL", {
            "instrument": "Partial", "ticker": "PARTIAL", "report_id": "AF-20260620-PARTIAL",
        }, files={"free.html": "<html>only free html</html>"})   # no free.pdf / preview.png / pro
        _write_ledger(root / "ledger" / "outcome_ledger.csv", [])

        catalog, _ = _run_export(monkeypatch, root)
        e = catalog[0]
        assert e["freeHtml"].endswith("/free.html")   # exists -> advertised
        assert e["freePdf"] == ""                     # absent -> NOT advertised (no dead link)
        assert e["preview"] == ""
        assert e["hasPro"] is False

        monkeypatch.setattr(PUB, "REPORTS", reports)
        monkeypatch.setattr(sys, "argv", ["publish"])
        PUB.main()
        uploaded = set(fake_r2.objects)
        assert _key_from_asset_path(e["freeHtml"]) in uploaded         # the one real file
        # The catalog advertises nothing that publish didn't upload (empty url => not advertised).
        assert all(v == "" or _key_from_asset_path(v) in uploaded for v in (e["freePdf"], e["preview"]))

    def test_auto_publish_policy_unhides_edition_through_real_loader(self, tmp_path, monkeypatch):
        # Flip ONE asset to publish_policy "auto" in a tmp universe and confirm the whole chain
        # (config_loader validate -> _publish_policy_by_ticker -> load_catalog hidden gate) un-hides
        # exactly that edition while the rest stay approval-gated.
        raw = json.loads(REAL_ASSETS_JSON.read_text(encoding="utf-8-sig"))
        for a in raw["assets"]:
            if a["ticker"] == "BTC":
                a["publish_policy"] = "auto"
        custom = tmp_path / "custom_assets.json"
        custom.write_text(json.dumps(raw), encoding="utf-8")

        root = _seed_root(tmp_path, assets_json_path=custom)
        reports = root / "reports"
        _write_edition(reports, "2026-06-20", "BTC", {"instrument": "Bitcoin", "ticker": "BTC"},
                       files={"free.html": "x"})
        _write_edition(reports, "2026-06-20", "ETH", {"instrument": "Ethereum", "ticker": "ETH"},
                       files={"free.html": "x"})
        _write_ledger(root / "ledger" / "outcome_ledger.csv", [])

        catalog, _ = _run_export(monkeypatch, root)
        by_slug = {e["slug"]: e for e in catalog}
        assert by_slug["BTC"]["hidden"] is False    # publish_policy auto -> visible
        assert by_slug["ETH"]["hidden"] is True     # still approval_required


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
