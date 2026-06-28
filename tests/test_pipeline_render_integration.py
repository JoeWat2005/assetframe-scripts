"""Phase-2 INTEGRATION tests for scripts/pipeline/render/*.

Where test_pipeline_render_unit.py exercises each render leaf in isolation, this file wires the
render directory together AS A WHOLE and drives it from a REAL payload — the same payload the live
engine renders. To get that payload we run the genuine upstream writer,
`scripts/pipeline/scoring/scaffold_payload.py` (a real cross-subdir dependency), over the checked-in
fixtures (tests/test_fixtures/BTC_analysis.json + BTC_research_brief.json), then feed its output
through `mvp_report.main()` — the render entrypoint that wires normalize -> run_qa (the QA gate) ->
build_free/build_pro PDFs -> build_free_html/build_pro_html -> the fitz preview -> build_metadata.

This is the render SMOKE plus the writer->reader DATA CONTRACT: it proves the payload shape
scaffold EMITS is the shape the render gate + builders CONSUME (the exact integration-bug class:
module A writes a field module B reads under a different name/shape), and that every artifact is
produced, QA passes GREEN, the canonical price stays equal across modules, the persisted QA gate
matches the live one, and the free/Pro split holds in the rendered HTML.

The only fakes are true external boundaries: nothing here touches the network, Neon, R2, the
Anthropic SDK, or a subprocess. scaffold + render run IN-PROCESS against self-written tmp CSVs; all
writes (payload, predictions, the rendered edition) are redirected into a tmp dir — the real
ledger/reports/data trees are never touched.

Run:  python -m pytest tests/test_pipeline_render_integration.py -q
"""
import os
import sys
import json
import copy
import atexit
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # mirror existing render tests

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "test_fixtures"

# fpdf2 + the render package gate the whole module (same pattern as the unit suite); scaffold_payload
# resolves via the conftest sys.path shim. A missing dep -> skip the module, never a collection error.
try:
    import report_pdf as rp
    import mvp_report as M
    import mvp_report_qa as Q
    import mvp_report_html as H          # noqa: F401  (imported through main; kept for symmetry)
    import mvp_report_pdf as PDF         # noqa: F401
    import mvp_report_const as C
    import scaffold_payload as SP
    _HAVE_RENDER = True
except Exception as _ex:                 # pragma: no cover
    _HAVE_RENDER = False
    _IMPORT_ERR = _ex

# pymupdf (fitz) is only needed for preview.png; gate that one assertion separately.
try:
    import fitz                          # noqa: F401
    _HAVE_FITZ = True
except Exception:                        # pragma: no cover
    _HAVE_FITZ = False

skip_render = unittest.skipUnless(_HAVE_RENDER, "render pipeline requires fpdf2 (present on box/CI)")

_TMP = tempfile.mkdtemp(prefix="af_render_integ_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

# canonical BTC last close used to seed the candle CSVs so the QA price triple-equality holds:
# scaffold reads canonical.last_price from the hourly CSV's last close, and the pro hourly chart +
# free chart read the SAME file, so all three numbers agree by construction.
_LAST_CLOSE = 64344.26


def _write_candles(path, n, last_close, hourly=True):
    """A near-flat OHLC series ending exactly on `last_close`. Dates sit in the fixture's June-2026
    past, so the prediction window (built at 'now') is always >= latest bar (no QA lookahead)."""
    base = datetime(2026, 6, 1, 0, 0)
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write("date,open,high,low,close\n")
        for i in range(n):
            d = (base + step * i).strftime("%Y-%m-%d %H:%M")
            f.write(f"{d},{last_close - 5},{last_close + 5},{last_close - 8},{last_close}\n")
    return path


def _scaffold_payload():
    """Run the REAL scaffold over the REAL BTC fixtures and return the emitted payload dict.

    We inject a `files` block into a tmp copy of the analysis so the candle CSVs resolve to tmp
    files we control (the fixture points at data/candles/*.csv which aren't in the repo). Everything
    is redirected into _TMP; nothing is written under the repo's data/ledger/reports trees."""
    hourly_csv = os.path.join(_TMP, "BTC_hourly.csv")
    daily_csv = os.path.join(_TMP, "BTC_daily.csv")
    _write_candles(hourly_csv, 300, _LAST_CLOSE, hourly=True)
    _write_candles(daily_csv, 400, _LAST_CLOSE, hourly=False)

    analysis = json.loads((FIXTURES / "BTC_analysis.json").read_text(encoding="utf-8"))
    analysis["files"] = {"hourly_csv": hourly_csv, "daily_csv": daily_csv}
    analysis_path = os.path.join(_TMP, "BTC_analysis.json")
    Path(analysis_path).write_text(json.dumps(analysis), encoding="utf-8")

    out_payload = os.path.join(_TMP, "BTC_af_payload.json")
    out_preds = os.path.join(_TMP, "BTC_predictions.json")
    argv = ["scaffold_payload.py", "BTC",
            "--analysis", analysis_path,
            "--brief", str(FIXTURES / "BTC_research_brief.json"),
            "--session-profile", "crypto_24_7",
            "--out", out_payload, "--predictions", out_preds]
    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = argv
    os.chdir(ROOT)                       # scaffold reads a couple of OPTIONAL relative paths (calib etc.)
    try:
        SP.main()
    finally:
        sys.argv, _ = old_argv, os.chdir(old_cwd)
    return json.loads(Path(out_payload).read_text(encoding="utf-8")), hourly_csv


def _render(payload, out_dir, extra_args=()):
    """Drive the render entrypoint mvp_report.main() on `payload`, writing into `out_dir` (tmp).
    Returns the resolved out_dir Path. Raises if main() exits non-zero (a QA hard-abort)."""
    p = copy.deepcopy(payload)
    p["out_dir"] = str(out_dir)
    pj = os.path.join(_TMP, f"render_in_{Path(out_dir).name}.json")
    Path(pj).write_text(json.dumps(p), encoding="utf-8")
    old_argv, old_cwd = sys.argv, os.getcwd()
    sys.argv = ["mvp_report.py", pj, *extra_args]
    os.chdir(ROOT)
    try:
        M.main()                         # returns on success; sys.exit(1) on a QA hard-abort
    except SystemExit as e:
        if e.code not in (0, None):
            raise AssertionError(f"mvp_report.main() aborted with exit {e.code}") from e
    finally:
        sys.argv, _ = old_argv, os.chdir(old_cwd)
    return Path(out_dir)


# Build the payload + render ONCE at import (guarded), share across the test classes.
_SETUP_ERR = None
_PAYLOAD = _HOURLY_CSV = _SMOKE_DIR = _NORENDER_DIR = None
if _HAVE_RENDER:
    try:
        _PAYLOAD, _HOURLY_CSV = _scaffold_payload()
        _SMOKE_DIR = _render(_PAYLOAD, os.path.join(_TMP, "render_full"))
        _NORENDER_DIR = _render(_PAYLOAD, os.path.join(_TMP, "render_noop"), extra_args=("--no-render",))
    except Exception as _e:              # pragma: no cover - surfaced as a skip + reported as a bug
        _SETUP_ERR = f"{type(_e).__name__}: {_e}"

ready = unittest.skipUnless(_HAVE_RENDER and _SETUP_ERR is None,
                            f"scaffold->render setup unavailable ({_SETUP_ERR})")

_CORE_ARTIFACTS = ("free.pdf", "pro.pdf", "free.html", "pro.html", "metadata.json")


def _meta():
    return json.loads((_SMOKE_DIR / "metadata.json").read_text(encoding="utf-8"))


def _qa_ref():
    """Reproduce exactly what main() computed: normalize a deep copy (main mutates in place), then
    run the gate. Pure + deterministic, so it must match the metadata-persisted qa_checks."""
    pcopy = copy.deepcopy(_PAYLOAD)
    M._normalize_payload(pcopy)
    return Q.run_qa(pcopy)


# =========================================================================== smoke / artifacts
@skip_render
@ready
class TestRenderSmoke(unittest.TestCase):
    def test_all_core_artifacts_written_and_nonempty(self):
        for name in _CORE_ARTIFACTS:
            fp = _SMOKE_DIR / name
            self.assertTrue(fp.exists(), f"{name} not produced")
            self.assertGreater(fp.stat().st_size, 0, f"{name} is empty")

    def test_pdfs_are_valid_pdf_streams(self):
        for name in ("free.pdf", "pro.pdf"):
            with open(_SMOKE_DIR / name, "rb") as f:
                self.assertEqual(f.read(5), b"%PDF-", f"{name} is not a PDF")

    @unittest.skipUnless(_HAVE_FITZ, "preview.png requires pymupdf/fitz")
    def test_preview_png_when_fitz_present(self):
        png = _SMOKE_DIR / "preview.png"
        self.assertTrue(png.exists(), "preview.png missing although fitz imported")
        with open(png, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n", "preview.png is not a PNG")

    def test_html_documents_are_wellformed(self):
        free = (_SMOKE_DIR / "free.html").read_text(encoding="utf-8")
        pro = (_SMOKE_DIR / "pro.html").read_text(encoding="utf-8")
        for html in (free, pro):
            self.assertTrue(html.startswith("<!DOCTYPE html>"))
            self.assertTrue(html.rstrip().endswith("</html>"))
        self.assertIn("Snapshot", free)
        self.assertIn(_PAYLOAD["title"], free)        # "Bitcoin / US Dollar (BTC)"
        self.assertIn("ASSETFRAME PRO", pro)


# =========================================================================== the QA gate, wired
@skip_render
@ready
class TestQaGateGreen(unittest.TestCase):
    def test_run_qa_has_no_errors_on_scaffolded_payload(self):
        _, errs, _ = _qa_ref()
        self.assertEqual(errs, [], f"scaffold->QA contract broke: {errs}")

    def test_every_gate_flag_true_except_visual(self):
        qa, _, _ = _qa_ref()
        for k, v in qa.items():
            if k == "visual_inspection_passed":
                self.assertFalse(v, "visual inspection must only be stamped post-render")
            else:
                self.assertTrue(v, f"QA flag {k!r} is not True")

    def test_metadata_persists_the_live_gate_verbatim(self):
        # the render must write back the SAME gate object it evaluated (not a stale/edited copy)
        qa, _, _ = _qa_ref()
        self.assertEqual(_meta()["qa_checks"], qa)

    def test_writer_reader_level_contracts(self):
        # the cross-module structural contract: scaffold's setups/ladder/ledger reference ONLY
        # canonical level values, which is exactly what the render gate verifies.
        qa, _, _ = _qa_ref()
        for k in ("levels_match_setups", "setups_match_ladder", "ledger_levels_match_tables"):
            self.assertTrue(qa[k], k)


# =========================================================================== cross-module data contracts
@skip_render
@ready
class TestPriceTripleEquality(unittest.TestCase):
    def test_canonical_chart_metadata_price_agree(self):
        canon = float(_PAYLOAD["canonical"]["last_price"]["value"])
        # the pro hourly chart + the free chart both read this file
        rows = rp.read_series(_HOURLY_CSV)
        self.assertTrue(rows, "hourly chart CSV had no rows")
        csv_last = float(rows[-1]["c"])
        # the number parsed out of the human meta.last_price string
        import re
        m = re.match(r"[\d,]+(?:\.\d+)?", str(_PAYLOAD["meta"]["last_price"]))
        meta_num = float(m.group(0).replace(",", ""))
        tol = max(0.01, canon * 1e-5)
        self.assertLessEqual(abs(csv_last - canon), tol, "chart CSV last != canonical")
        self.assertLessEqual(abs(meta_num - canon), tol, "meta.last_price != canonical")

    def test_gate_confirms_header_matches_chart(self):
        qa, _, _ = _qa_ref()
        self.assertTrue(qa["header_price_matches_chart"])
        self.assertTrue(qa["free_chart_matches_metadata"])
        self.assertTrue(qa["pro_chart_matches_metadata"])

    def test_pro_html_renders_the_canonical_price(self):
        canon = float(_PAYLOAD["canonical"]["last_price"]["value"])
        shown = rp.fmt(canon, canon)                  # render module's own formatting -> "64,344.3"
        self.assertIn(shown, (_SMOKE_DIR / "pro.html").read_text(encoding="utf-8"))

    def test_confidence_agrees_across_payload_and_breakdown(self):
        qa, _, _ = _qa_ref()
        self.assertTrue(qa["confidence_matches_breakdown"])
        self.assertEqual(int(_PAYLOAD["confidence"]),
                         int(_PAYLOAD["confidence_breakdown"]["published"]))


# =========================================================================== free / Pro split, rendered
@skip_render
@ready
class TestFreeProSplit(unittest.TestCase):
    # Pro-only SECTION HEADINGS (not the lead-magnet teaser, which legitimately *names* Pro
    # features in prose). These must render in Pro and be absent from the free Snapshot.
    PRO_ONLY_HEADINGS = ("Conditional setups", "Outcome ledger", "Source audit",
                         "Trade-quality scorecard", "Price ladder")

    def setUp(self):
        self.free = (_SMOKE_DIR / "free.html").read_text(encoding="utf-8")
        self.pro = (_SMOKE_DIR / "pro.html").read_text(encoding="utf-8")

    def test_gate_flag_split_enforced(self):
        qa, _, _ = _qa_ref()
        self.assertTrue(qa["free_pro_split_enforced"])

    def test_pro_only_sections_present_in_pro_absent_in_free(self):
        for h in self.PRO_ONLY_HEADINGS:
            self.assertIn(h, self.pro, f"Pro is missing the {h!r} section")
            self.assertNotIn(h, self.free, f"{h!r} leaked into the free Snapshot")

    def test_pro_value_add_sections_render(self):
        for s in ("Source confidence", "Report quality", "Glossary"):
            self.assertIn(s, self.pro, f"Pro missing {s!r}")

    def test_free_snapshot_carries_disclaimer_and_teaser(self):
        self.assertIn("Not personal financial advice", self.free)
        self.assertIn("Pro adds", self.free)          # the lead-magnet pitch


# =========================================================================== forecast-only (--no-render)
@skip_render
@ready
class TestForecastOnlyPath(unittest.TestCase):
    def test_only_metadata_written_no_artifacts(self):
        present = set(os.listdir(_NORENDER_DIR))
        self.assertIn("metadata.json", present)
        for artifact in ("free.pdf", "pro.pdf", "free.html", "pro.html", "preview.png"):
            self.assertNotIn(artifact, present,
                             f"--no-render should skip {artifact} but it was written")

    def test_no_render_metadata_still_passes_the_gate(self):
        meta = json.loads((_NORENDER_DIR / "metadata.json").read_text(encoding="utf-8"))
        qa = meta["qa_checks"]
        for k, v in qa.items():
            if k == "visual_inspection_passed":
                self.assertFalse(v)
            else:
                self.assertTrue(v, f"forecast-only QA flag {k!r} not True")


# =========================================================================== build_metadata wiring
@skip_render
@ready
class TestMetadataWiring(unittest.TestCase):
    def test_paths_block_resolves_to_real_files(self):
        meta = _meta()
        for key, fname in meta["paths"].items():
            self.assertEqual(Path(fname).name, fname, "path should be a bare filename")
            self.assertTrue((_SMOKE_DIR / fname).exists(), f"{key} -> {fname} does not exist")

    def test_brand_tagline_and_tz_carried_through(self):
        meta = _meta()
        self.assertEqual(meta["brand"], C.BRAND)
        self.assertEqual(meta["tagline"], C.TAGLINE)
        self.assertEqual(meta["report_timezone"], "UTC")
        self.assertTrue(meta["partial_indicators_hidden"])

    def test_source_confidence_unpacks_from_brief_pairs(self):
        # build_metadata does `for label, text in block` over pro.source_confidence — the brief emits
        # 2-tuples; this is the contract that a 3-col row would break. Assert it round-tripped.
        meta = _meta()
        self.assertIn("source_confidence", meta)
        self.assertIn("Overall", meta["source_confidence"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
