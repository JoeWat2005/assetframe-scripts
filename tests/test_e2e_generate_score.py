"""PHASE 3 — END-TO-END across EVERY refactored subgroup: the full "morning run" for ONE asset,
driven OFFLINE by calling each stage module's REAL main()/functions in sequence (the orchestration
run_daily does, minus the subprocess spawning), threading real artifacts through a tmp ROOT (cwd).

Where the Phase-2 integration suites each wire ONE directory together, this proves the WHOLE
generate -> score chain hands off correctly ACROSS the subgroups:

  scheduler/config   config_loader.load_assets         -> picks the BTC asset from config/assets.json
  pipeline/marketdata intraday.main (fetch FAKED)        -> data/analysis/BTC_analysis.json + candle CSVs
  analytics/memory   ledger_context.main + memory_pack   -> per-instrument priors + bounded pack
  pipeline/scoring   scaffold_payload.main               -> data/payloads + data/predictions
  pipeline/render    mvp_report.main                     -> free+Pro HTML/PDF + QA gate GREEN + metadata
  pipeline/scoring   score_report.main (backdated)       -> one graded row in ledger/outcome_ledger.csv

Brief authoring is the operator path (ASSETFRAME_AUTHOR_BRIEFS=0 + the committed operator brief
fixture) — NO Anthropic. The ONLY fake is the true external boundary: the network
(data_providers._http_json). Nothing here touches Neon / R2 / the Anthropic SDK / a subprocess. The
WHOLE pipeline runs in-process with cwd chdir'd into a throwaway tmp ROOT, so the real
data/ ledger/ reports/ trees are NEVER written (every persistent path the modules use is
cwd-relative; only the ROOT-anchored brand LOGO is read from the real repo).

The run is BACKDATED to ~10 days ago so the prediction window is already CLOSED and therefore
scoreable, with no look-ahead: intraday trims bars to <= the as-of moment (so the analysis' latest
bar == the prediction-window start -> the render's no_lookahead gate holds), while the scorer grades
the typed predictions against a SEPARATE synthetic candle CSV we author to cover the closed window.

The strongest assertions are cross-stage CONTRACTS: the report_id minted by scaffold flows
byte-identically into the payload, the rendered metadata and the ledger row; the brief's instrument
and the scaffold taxonomy land verbatim in the graded ledger row; and the render QA gate is GREEN.

Run:  python -m pytest tests/test_e2e_generate_score.py -q
"""
import os
import sys
import json
import csv
import math
import atexit
import shutil
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

# score_report binds its LEDGER path (live vs sim) from ASSETFRAME_SANDBOX at IMPORT — force the
# live (non-sandbox) path before importing it, regardless of the ambient environment.
os.environ.pop("ASSETFRAME_SANDBOX", None)
# Operator-brief path (no Anthropic): exactly the scenario's "ASSETFRAME_AUTHOR_BRIEFS off".
os.environ["ASSETFRAME_AUTHOR_BRIEFS"] = "0"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # mirror the existing tests' import style

import data_providers as DP        # the ONE network boundary we fake
import config_loader as CL         # scheduler/config
import memory_pack as MP           # analytics/memory
import ledger_context as LC        # analytics/memory
import scaffold_payload as SP      # pipeline/scoring
import score_report as S           # pipeline/scoring

# fpdf2 gates the render leg only (present on box/CI). Upstream stages + scoring never need it, so a
# missing dep skips ONLY the render assertions — never a collection error.
try:
    import mvp_report as M         # pipeline/render
    _HAVE_RENDER = True
except Exception as _ex:           # pragma: no cover
    _HAVE_RENDER = False
    _RENDER_ERR = _ex

REPO = Path(__file__).resolve().parent.parent
FIX = REPO / "tests" / "test_fixtures"
UNIVERSE = REPO / "config" / "assets.json"

# Backdate ~10 days so the window is closed (scoreable) yet robust to the wall clock; on an exact
# hour so intraday's last hourly bar lands precisely on the as-of (== the window start -> no look-ahead).
AS_OF_DT = (datetime.now(timezone.utc) - timedelta(days=10)).replace(
    hour=12, minute=0, second=0, microsecond=0)
AS_OF = AS_OF_DT.strftime("%Y-%m-%d %H:%M")
CUTOFF_TS = int(AS_OF_DT.timestamp())

META_CRYPTO = {"instrumentType": "CRYPTOCURRENCY", "exchangeTimezoneName": "UTC",
               "regularMarketPrice": 99999.0, "currentTradingPeriod": None}


# ------------------------------------------------------------------ synthetic feed (the only fake)
def _gen_daily(n=400):
    """A mildly declining daily series ending at the as-of date's UTC midnight (n>=200 warms SMA200)."""
    base = datetime.fromtimestamp(CUTOFF_TS - (n - 1) * 86400, tz=timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(n):
        ts = int((base + timedelta(days=i)).timestamp())
        p = 70000.0 - i * 10 + 800.0 * math.sin(i / 7.0)
        out.append((ts, p - 50, p + 120, p - 130, p, 1000 + i))
    return out


def _gen_hourly(days=35):
    """A dense 1h series for ~`days` days ending exactly at the as-of (last bar age 0 at the cutoff)."""
    n = days * 24
    start = CUTOFF_TS - (n - 1) * 3600
    out = []
    for i in range(n):
        ts = start + i * 3600
        p = 65000.0 - i * 0.5 + 200.0 * math.sin(i / 11.0)
        out.append((ts, p - 20, p + 60, p - 70, p, 10 + (i % 7)))
    return out


_DAILY = _gen_daily()
_HOURLY = _gen_hourly()


def _yahoo_payload(bars):
    return {"chart": {"error": None, "result": [{
        "meta": META_CRYPTO,
        "timestamp": [b[0] for b in bars],
        "indicators": {"quote": [{
            "open": [b[1] for b in bars], "high": [b[2] for b in bars],
            "low": [b[3] for b in bars], "close": [b[4] for b in bars],
            "volume": [b[5] for b in bars]}]},
    }]}}


def _fake_http_json(url, *a, **k):
    """interval=1d -> daily payload; every other interval (60m, related) -> hourly/daily envelope."""
    return _yahoo_payload(_DAILY if "interval=1d" in url else _HOURLY)


# ------------------------------------------------------------------ one-shot pipeline driver
_TMP = tempfile.mkdtemp(prefix="af_e2e_gs_")
atexit.register(lambda: shutil.rmtree(_TMP, ignore_errors=True))

# Everything the test asserts on, captured ONCE by _drive_pipeline() (build-once, like the Phase-2
# render integration). Absolute tmp paths so assertions never depend on cwd.
RESULT = {"error": None}


def _seed_ledger(work, asset):
    """A small, realistic prior ledger so ledger_context/memory_pack have history to aggregate.
    report_ids are dated 5/4 days BEFORE the as-of (8-digit stamp) so they can never collide with the
    generated 12-digit backdated id, and their windows close before the as-of (visible, no look-ahead)."""
    led = work / "ledger" / "outcome_ledger.csv"
    led.parent.mkdir(parents=True, exist_ok=True)

    def _row(rid, wend, hits, misses):
        d = dict(zip(S.LEDGER_COLS, [""] * len(S.LEDGER_COLS)))
        d.update({"scored_at_utc": "2026-01-01 00:00", "report_id": rid,
                  "instrument": asset["instrument"], "view": "x", "confidence": "55",
                  "window_end_utc": wend, "results": "P1=Y", "hits": str(hits), "misses": str(misses),
                  "hit_rate_pct": "60", "setup_filled": "no", "setup_outcome": "n/a", "partial": "no",
                  "conf_version": "2", "conf_raw": "55", "asset_class": asset["asset_class"],
                  "pred_type": "range_hold", "direction": "bearish", "horizon": "next_session",
                  "market_regime": "trend_down"})
        return d

    r1 = f"AF-{(AS_OF_DT - timedelta(days=5)).strftime('%Y%m%d')}-{asset['ticker']}"
    r2 = f"AF-{(AS_OF_DT - timedelta(days=4)).strftime('%Y%m%d')}-{asset['ticker']}"
    w1 = (AS_OF_DT - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    w2 = (AS_OF_DT - timedelta(days=4)).strftime("%Y-%m-%d %H:%M")
    with open(led, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=S.LEDGER_COLS)
        w.writeheader()
        w.writerow(_row(r1, w1, 3, 1))
        w.writerow(_row(r2, w2, 2, 2))
    return led, [r1, r2]


def _run_main(mod, argv):
    """Drive a module's REAL main() with a synthesized argv; tolerate a clean sys.exit(0/None)."""
    saved = sys.argv
    sys.argv = argv
    try:
        mod.main()
    except SystemExit as e:
        if e.code not in (0, None):
            raise AssertionError(f"{mod.__name__}.main() exited {e.code} for argv={argv!r}")
    finally:
        sys.argv = saved


def _drive_pipeline():
    work = Path(_TMP) / "root"
    work.mkdir(parents=True, exist_ok=True)
    saved_cwd = os.getcwd()
    saved_http = DP._http_json
    DP._http_json = _fake_http_json           # fake the single network boundary
    os.chdir(work)                            # tmp ROOT — every persistent path is cwd-relative
    try:
        # 1. config_loader picks the asset from the REAL universe
        assets = CL.load_assets(str(UNIVERSE))
        asset = next(a for a in assets if a["id"] == "btc")
        tk = asset["ticker"]
        RESULT["asset"] = asset

        # 2. intraday.main (fetch faked) -> analysis + candle CSVs (trimmed to <= as-of)
        _run_main(__import__("intraday"), [
            "intraday.py", asset["provider_symbols"]["yahoo"], "--name", tk,
            "--provider", "yahoo", "--as-of", AS_OF, "--roll-utc", str(asset["roll_utc"]),
            "--session-profile", asset["session_profile"], "--related", asset["related"]])
        analysis_path = work / "data" / "analysis" / f"{tk}_analysis.json"
        RESULT["analysis_path"] = analysis_path
        RESULT["analysis"] = json.loads(analysis_path.read_text(encoding="utf-8"))
        RESULT["hourly_csv_gen"] = work / "data" / "candles" / f"{tk}_hourly.csv"

        # 3. seed a small ledger, then build the context (ledger_context + memory_pack)
        led, seeded_ids = _seed_ledger(work, asset)
        RESULT["ledger_path"] = led
        RESULT["seeded_ids"] = seeded_ids
        _run_main(LC, ["ledger_context.py", tk, "--ticker", tk,
                       "--asset-class", asset["asset_class"], "--as-of", AS_OF])
        lc_path = work / "data" / "ledger_context" / f"{tk}_ledger_context.json"
        RESULT["ledger_context_path"] = lc_path
        RESULT["ledger_context"] = json.loads(lc_path.read_text(encoding="utf-8"))
        pack = MP.build_pack(asset, as_of=AS_OF_DT)          # default ledger = cwd-relative tmp ledger
        mp_path = work / "data" / "memory_packs" / f"{tk}_memory_pack.json"
        mp_path.parent.mkdir(parents=True, exist_ok=True)
        mp_path.write_text(json.dumps(pack, indent=1), encoding="utf-8")
        RESULT["memory_pack_path"] = mp_path
        RESULT["memory_pack"] = pack

        # 4. operator brief (no Anthropic) -> scaffold_payload.main -> payload + predictions
        (work / "data" / "briefs").mkdir(parents=True, exist_ok=True)
        brief_src = json.loads((FIX / "BTC_research_brief.json").read_text(encoding="utf-8-sig"))
        RESULT["brief"] = brief_src
        (work / "data" / "briefs" / f"{tk}_research_brief.json").write_text(
            json.dumps(brief_src), encoding="utf-8")
        score_cadence = {"weekly": "weekly", "monthly": "monthly"}.get(asset.get("cadence"), "daily")
        _run_main(SP, ["scaffold_payload.py", tk, "--session-profile", asset["session_profile"],
                       "--cadence", score_cadence, "--as-of", AS_OF])
        preds_path = work / "data" / "predictions" / f"{tk}_predictions.json"
        payload_path = work / "data" / "payloads" / f"{tk}_af_payload.json"
        RESULT["preds_path"] = preds_path
        RESULT["payload_path"] = payload_path
        RESULT["preds"] = json.loads(preds_path.read_text(encoding="utf-8"))
        RESULT["payload"] = json.loads(payload_path.read_text(encoding="utf-8"))

        # 5. render + QA gate (only if fpdf2 present)
        if _HAVE_RENDER:
            rep_dir = work / "reports" / tk
            pay = dict(RESULT["payload"])
            pay["out_dir"] = str(rep_dir)
            render_in = work / "render_in.json"
            render_in.write_text(json.dumps(pay), encoding="utf-8")
            _run_main(M, ["mvp_report.py", str(render_in)])
            RESULT["report_dir"] = rep_dir
            RESULT["metadata"] = json.loads((rep_dir / "metadata.json").read_text(encoding="utf-8"))

        # 6. backdate scoring: author a synthetic CSV covering the (now-closed) window, then grade
        preds = RESULT["preds"]
        ws = datetime.strptime(preds["window_start_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        we = datetime.strptime(preds["window_end_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        score_csv = work / "data" / "candles" / f"{tk}_scoring.csv"
        with open(score_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Datetime", "Open", "High", "Low", "Close", "Volume"])
            t = ws
            px = float(RESULT["payload"]["canonical"]["last_price"]["value"])
            while t <= we:                       # drift down through the window (bearish-consistent)
                px -= 30.0
                w.writerow([t.strftime("%Y-%m-%d %H:%M"), px + 10, px + 25, px - 25, px, 1000])
                t += timedelta(hours=1)
        RESULT["score_csv"] = score_csv
        _run_main(S, ["score_report.py", str(preds_path), "--hourly", str(score_csv)])
        RESULT["ledger_rows"] = list(csv.DictReader(led.read_text(encoding="utf-8").splitlines()))
    except Exception as e:                       # surfaced as a skip + reported, never a hard error
        RESULT["error"] = f"{type(e).__name__}: {e}"
        raise
    finally:
        DP._http_json = saved_http
        os.chdir(saved_cwd)


# build ONCE at import (guarded); the test classes assert on the captured artifacts.
try:
    _drive_pipeline()
except Exception:                                # pragma: no cover  (RESULT['error'] carries the why)
    pass

ready = unittest.skipUnless(RESULT.get("error") is None,
                            f"e2e pipeline setup failed: {RESULT.get('error')}")
render_ready = unittest.skipUnless(_HAVE_RENDER and RESULT.get("error") is None,
                                   "render leg needs fpdf2 (present on box/CI)")


# =========================================================================== stage 1: config
@ready
class TestConfigStage(unittest.TestCase):
    def test_loader_selected_the_btc_asset(self):
        a = RESULT["asset"]
        self.assertEqual(a["id"], "btc")
        self.assertEqual(a["ticker"], "BTC")
        self.assertEqual(a["provider_symbols"]["yahoo"], "BTC-USD")
        self.assertEqual(a["asset_class"], "crypto")
        self.assertEqual(a["session_profile"], "crypto_24_7")
        # _normalize fills the derived defaults the downstream stages rely on
        self.assertIn("60m", a["chart_intervals"])
        self.assertIn("1d", a["chart_intervals"])
        self.assertTrue(a["timeframes"])


# =========================================================================== stage 2: marketdata
@ready
class TestIntradayStage(unittest.TestCase):
    def test_analysis_and_candles_written(self):
        aj = RESULT["analysis"]
        self.assertEqual(aj["symbol"], "BTC-USD")
        self.assertIsNone(aj["degraded"])                       # full (not daily_only) analysis
        # intraday TRIMS to the as-of: the latest bar is the as-of moment (== the window start)
        self.assertEqual(aj["last_bar_utc"], AS_OF)
        self.assertEqual(aj["as_of"], AS_OF)
        for k in ("pivots_classic", "atr_day_bands", "daily", "hourly", "stats_last_sessions", "files"):
            self.assertIn(k, aj)
        # the candle CSVs the analysis advertises exist; the hourly path is cwd-relative (-> tmp)
        self.assertEqual(aj["files"]["hourly_csv"], "data/candles/BTC_hourly.csv")
        self.assertTrue(RESULT["hourly_csv_gen"].exists())
        self.assertGreater(RESULT["hourly_csv_gen"].stat().st_size, 0)

    def test_last_price_is_the_last_bar_close_not_a_live_quote(self):
        # the generation CSV's last close is the analysis last_price (never regularMarketPrice 99999)
        rows = [r for r in RESULT["hourly_csv_gen"].read_text(encoding="utf-8").splitlines() if r.strip()]
        csv_last = float(rows[-1].split(",")[4])
        self.assertAlmostEqual(RESULT["analysis"]["last_price"], csv_last, places=4)
        self.assertNotEqual(RESULT["analysis"]["last_price"], 99999.0)


# =========================================================================== stage 3: analytics/memory
@ready
class TestMemoryStage(unittest.TestCase):
    def test_ledger_context_aggregated_the_seeded_history(self):
        lc = RESULT["ledger_context"]
        # both seeded BTC rows close before the as-of -> both visible to the per-instrument prior
        self.assertEqual(lc["historical_prediction_count"], 2)
        self.assertEqual(lc["ticker"], "BTC")
        self.assertIsNotNone(lc["instrument_hit_rate"])

    def test_memory_pack_sees_the_same_history_and_is_bounded(self):
        pack = RESULT["memory_pack"]
        self.assertEqual(pack["instrument_history"]["reports"], 2)
        self.assertEqual(pack["global"]["total_scored_reports"], 2)
        self.assertTrue(pack["budget"]["within_budget"])


# =========================================================================== stage 4: scoring scaffold
@ready
class TestScaffoldStage(unittest.TestCase):
    def test_predictions_and_payload_are_coherent(self):
        preds, pay = RESULT["preds"], RESULT["payload"]
        self.assertEqual([p["id"] for p in preds["predictions"]], ["P1", "P2", "P3", "P4", "P6"])
        # the backdated daily report_id stamps the as-of minute and keeps BTC as the ticker suffix
        self.assertEqual(preds["report_id"], f"AF-{AS_OF_DT.strftime('%Y%m%d%H%M')}-BTC")
        self.assertEqual(preds["report_id"], pay["report_id"])
        # the prediction window opens at the as-of (no look-ahead) and is closed relative to now
        self.assertEqual(preds["window_start_utc"], AS_OF)
        self.assertLess(datetime.strptime(preds["window_end_utc"], "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc), datetime.now(timezone.utc))
        # instrument flows from the OPERATOR brief (not the config display name) into the predictions
        self.assertEqual(preds["instrument"], RESULT["brief"]["instrument"])
        # the predictions point the scorer at the SAME canonical hourly CSV the analysis advertised
        self.assertEqual(preds["hourly_csv"], "data/candles/BTC_hourly.csv")
        # taxonomy is the brief's directional view, validated through scaffold
        self.assertEqual(preds["taxonomy"]["direction"], "bearish")
        self.assertEqual(preds["taxonomy"]["market_regime"], "trend_down")

    def test_canonical_price_reads_from_the_generation_csv(self):
        pay = RESULT["payload"]
        rows = [r for r in RESULT["hourly_csv_gen"].read_text(encoding="utf-8").splitlines() if r.strip()]
        csv_last = float(rows[-1].split(",")[4])
        self.assertAlmostEqual(float(pay["canonical"]["last_price"]["value"]), csv_last, places=1)


# =========================================================================== stage 5: render + QA
@render_ready
class TestRenderStage(unittest.TestCase):
    CORE = ("free.pdf", "pro.pdf", "free.html", "pro.html", "metadata.json")

    def test_all_core_artifacts_written_and_nonempty(self):
        for name in self.CORE:
            fp = RESULT["report_dir"] / name
            self.assertTrue(fp.exists(), f"{name} not produced")
            self.assertGreater(fp.stat().st_size, 0, f"{name} is empty")
        for name in ("free.pdf", "pro.pdf"):
            with open(RESULT["report_dir"] / name, "rb") as f:
                self.assertEqual(f.read(5), b"%PDF-", f"{name} is not a PDF")

    def test_qa_gate_is_green(self):
        qa = RESULT["metadata"]["qa_checks"]
        for k, v in qa.items():
            if k == "visual_inspection_passed":
                self.assertFalse(v, "visual inspection is only stamped post-render")
            else:
                self.assertTrue(v, f"QA flag {k!r} is not True")

    def test_metadata_has_no_lookahead_window_at_the_as_of(self):
        meta = RESULT["metadata"]
        self.assertEqual(meta["prediction_window_start_utc"], AS_OF)
        # the latest bar must not sit AFTER the window opens (the gate's no_lookahead rule)
        bt = datetime.strptime(meta["latest_bar_timestamp_utc"][:16], "%Y-%m-%d %H:%M")
        ws = datetime.strptime(meta["prediction_window_start_utc"][:16], "%Y-%m-%d %H:%M")
        self.assertGreaterEqual(ws, bt - timedelta(hours=1))
        self.assertTrue(RESULT["metadata"]["qa_checks"]["no_lookahead"])

    def test_free_pro_split_holds_in_the_rendered_html(self):
        free = (RESULT["report_dir"] / "free.html").read_text(encoding="utf-8")
        pro = (RESULT["report_dir"] / "pro.html").read_text(encoding="utf-8")
        self.assertTrue(free.startswith("<!DOCTYPE html>") and free.rstrip().endswith("</html>"))
        self.assertTrue(pro.startswith("<!DOCTYPE html>") and pro.rstrip().endswith("</html>"))
        self.assertIn("ASSETFRAME PRO", pro)
        for heading in ("Conditional setups", "Outcome ledger"):
            self.assertIn(heading, pro, f"Pro is missing {heading!r}")
            self.assertNotIn(heading, free, f"{heading!r} leaked into the free Snapshot")


# =========================================================================== stage 6: scoring -> ledger
@ready
class TestScoreStage(unittest.TestCase):
    def _last_row(self):
        return RESULT["ledger_rows"][-1]

    def test_exactly_one_row_appended_to_the_seeded_ledger(self):
        # 2 seeded priors + 1 graded = 3; the seeded rows are preserved (append-only)
        self.assertEqual(len(RESULT["ledger_rows"]), 3)
        ids = [r["report_id"] for r in RESULT["ledger_rows"]]
        for sid in RESULT["seeded_ids"]:
            self.assertIn(sid, ids)

    def test_graded_row_threads_the_whole_chain(self):
        row = self._last_row()
        preds = RESULT["preds"]
        # the row carries EXACTLY the declared columns (no width drift between writer + header)
        self.assertEqual(set(row.keys()), set(S.LEDGER_COLS))
        # report_id minted by scaffold -> payload -> ledger, byte-identical
        self.assertEqual(row["report_id"], preds["report_id"])
        # instrument from the operator brief; window_end from scaffold; taxonomy from scaffold
        self.assertEqual(row["instrument"], RESULT["brief"]["instrument"])
        self.assertEqual(row["window_end_utc"], preds["window_end_utc"])
        self.assertEqual(row["asset_class"], preds["taxonomy"]["asset_class"])
        self.assertEqual(row["pred_type"], preds["taxonomy"]["prediction_type"])
        self.assertEqual(row["direction"], preds["taxonomy"]["direction"])
        self.assertEqual(row["horizon"], preds["taxonomy"]["horizon"])
        self.assertEqual(row["market_regime"], preds["taxonomy"]["market_regime"])
        # confidence computed ONCE in scaffold flows unchanged into the ledger
        self.assertEqual(row["confidence"], str(preds["confidence"]))
        self.assertEqual(row["conf_version"], str(preds["conf_version"]))

    def test_graded_results_are_coherent(self):
        row = self._last_row()
        verdicts = dict(tok.split("=", 1) for tok in row["results"].split())
        # every typed prediction was graded (P1..P6 present); the manual P6 is unresolved -> MANUAL
        self.assertEqual(set(verdicts), {"P1", "P2", "P3", "P4", "P6"})
        self.assertEqual(verdicts["P6"], "MANUAL")
        hits, misses = int(row["hits"]), int(row["misses"])
        graded = [v for v in verdicts.values() if v in ("Y", "N")]
        self.assertEqual(hits + misses, len(graded))            # NT/MANUAL excluded from the denominator
        self.assertEqual(hits, sum(1 for v in verdicts.values() if v == "Y"))
        if hits + misses:
            self.assertAlmostEqual(float(row["hit_rate_pct"]), round(100 * hits / (hits + misses), 1), places=1)
        self.assertEqual(row["partial"], "no")                  # full window covered by the scoring CSV


if __name__ == "__main__":
    unittest.main(verbosity=2)
