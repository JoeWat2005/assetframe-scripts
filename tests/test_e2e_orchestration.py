"""Phase-3 END-TO-END orchestration-contract tests — the WHOLE pipeline across the refactored
subgroups, fully OFFLINE.

Phases 1+2 covered the helpers in isolation (units) and the per-directory wiring (integration). This
suite instead drives the REAL top-level orchestrators across the FULL CHAIN and asserts the one thing
a per-directory test can't see: that every stage the orchestrator spawns references the correct
POST-REFACTOR module path. The argv handed to the (faked) child launcher is captured and EVERY `-m`
module string is resolved with importlib.util.find_spec — so a stale `-m scripts.<old.path>` left
behind by the Phase-5 subpackage move would fail here even though every unit/integration test passes.

Two orchestrators are exercised:

  A. scripts.scheduler.run.run_daily.main()  — the daily batch across --mode dry_run / score_only /
     generate_only / production, with the subprocess launchers RD._run / RD._run_rc monkeypatched to
     canned results that ALSO reproduce each child's on-disk side effects (intraday -> analysis,
     scaffold -> payload, brief_writer -> brief). Asserts: the run manifest/plan is built; the per-asset
     status transitions are correct; the stage hand-off ORDER is intraday -> brief_writer -> critic ->
     ledger_context -> scaffold_payload -> mvp_report; the report_id round-trips through the
     scaffold->payload->_finish_asset file contract; and EVERY spawned module path resolves.

  B. scripts.coordination.runner.run_and_record + run_backtest_batch — the engine_runs run-lifecycle,
     driven on a FakeConn (the only Neon boundary) with subprocess.Popen/run faked. Asserts the
     engine_runs INSERT('running')->UPDATE(terminal) envelope, the scope->argv handoff that launches
     `-m scripts.scheduler.run.run_daily`, the publish chain (export_content/publish/sync-db) and the
     backtest sync (`-m scripts.analytics.store.sync_backtest`) — all post-refactor paths, all resolved.

ONLY true external boundaries are faked: the spawning of child processes (RD._run/_run_rc, and
runner.subprocess.Popen/run), the Neon psycopg conn (FakeConn) and the memory_pack network builder.
Everything else — config_loader, calendar_rules, the run_daily orchestration, manifest read/summarize,
RunRecorder, the run lock — is the REAL code. ROOT + data dirs are repointed at tmp so nothing touches
the real ledger/reports/runs.

Run:  python -m pytest tests/test_e2e_orchestration.py -q
"""
import importlib.util
import json
import os
import sys
from datetime import timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import scripts  # noqa: F401  (side-effect: apply the subpackage sys.path shim used by find_spec)
import run_daily as RD

# coordination side (Part B) — flat imports, resolved by the package shim above.
import db
import runner
import manifest as MF

UTC = timezone.utc

# Genuine repo root, captured BEFORE any monkeypatch repoints RD.ROOT at tmp. Points the orchestrator
# at the REAL config/assets.json universe (a committed fixture, per the task rules).
REPO_ROOT = RD.ROOT
REAL_UNIVERSE = str(REPO_ROOT / "config" / "assets.json")

# The canonical POST-REFACTOR module paths the daily pipeline must spawn. If the Phase-5 move renamed a
# subgroup, the matching `-m` string in run_daily.py would drift and these tests would catch it.
CANON = {
    "intraday": "scripts.pipeline.marketdata.intraday",
    "scaffold": "scripts.pipeline.scoring.scaffold_payload",
    "render": "scripts.pipeline.render.mvp_report",
    "brief_writer": "scripts.pipeline.authoring.brief_writer",
    "critic": "scripts.pipeline.authoring.critic",
    "ledger_context": "scripts.analytics.memory.ledger_context",
    "calibrate": "scripts.analytics.store.calibrate",
    "research_memory": "scripts.analytics.memory.research_memory",
    "ledger_db": "scripts.analytics.store.ledger_db",
    "score_report": "scripts.pipeline.scoring.score_report",
    "run_daily": "scripts.scheduler.run.run_daily",
    "sync_backtest": "scripts.analytics.store.sync_backtest",
    "export_content": "scripts.delivery.export_content",
    "publish": "scripts.delivery.publish",
}


# ============================================================ module-path resolution (the crux)
def _module_of(cmd):
    """Extract the module string after a `-m` flag in a captured argv, or None (e.g. a `node` cmd)."""
    parts = [str(c) for c in cmd]
    if "-m" in parts:
        i = parts.index("-m")
        if i + 1 < len(parts):
            return parts[i + 1]
    return None


def _modules(cmds):
    return [m for m in (_module_of(c) for c in cmds) if m]


def _assert_resolves(mod):
    """A spawned `-m` module string MUST resolve via importlib — the post-refactor path is real."""
    try:
        spec = importlib.util.find_spec(mod)
    except (ImportError, AttributeError) as ex:
        pytest.fail(f"stale -m module path {mod!r}: find_spec raised {type(ex).__name__}: {ex} "
                    f"(a Phase-5 subpackage move likely left this path behind)")
    assert spec is not None, (
        f"stale -m module path {mod!r} does not resolve via importlib — a child stage references a "
        f"module that no longer exists at that dotted path (post-refactor drift)")


def test_canonical_paths_are_self_consistent():
    """Sanity guard for the test itself: the expected post-refactor paths all resolve TODAY, so a
    later failure unambiguously means run_daily/runner drifted, not that this list is stale."""
    for mod in CANON.values():
        _assert_resolves(mod)


# ============================================================ Part A — run_daily fakes / driver
def _name_after(cmd, flag):
    cmd = [str(c) for c in cmd]
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _make_children(root, *, verdict, writer_tele=None, intraday_ok=True, scaffold_ok=True,
                   render_ok=True, report_id="daily-2026-06-26-BTC", calls=None):
    """(_run, _run_rc) stand-ins that reproduce the REAL children's on-disk side effects so the
    cross-stage file hand-off is genuinely exercised, and append every raw argv to `calls` (in spawn
    order) so the module-path + ordering contracts can be asserted."""
    writer_tele = writer_tele or {"input_tokens": 120, "output_tokens": 30,
                                  "web_searches": 2, "est_cost_usd": 0.15}

    def _rec(cmd):
        if calls is not None:
            calls.append(list(cmd))

    def fake_run(cmd, timeout=180):
        _rec(cmd)
        j = " ".join(str(c) for c in cmd)
        if "marketdata.intraday" in j:
            tk = _name_after(cmd, "--name")
            if intraday_ok and tk:
                p = root / "data" / "analysis" / f"{tk}_analysis.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("{}", encoding="utf-8")
            return (intraday_ok, "{}", "" if intraday_ok else "intraday boom")
        if "scoring.scaffold_payload" in j:
            tk = [str(c) for c in cmd][2]            # positional NAME right after `-m <module>`
            if scaffold_ok:
                p = root / "data" / "payloads" / f"{tk}_af_payload.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"report_id": report_id}), encoding="utf-8")
            return (scaffold_ok, "{}", "" if scaffold_ok else "scaffold boom")
        if "render.mvp_report" in j:
            return (render_ok, "{}", "" if render_ok else "qa boom")
        # ledger_context / calibrate / research_memory / ledger_db / scoring score_report+intraday
        return (True, "{}", "")

    def fake_run_rc(cmd, timeout=180):
        _rec(cmd)
        j = " ".join(str(c) for c in cmd)
        if "authoring.brief_writer" in j:
            out = _name_after(cmd, "--out")
            if out:
                p = root / out
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(json.dumps({"thesis": "x"}), encoding="utf-8")
            return (True, 0, json.dumps(writer_tele), "")
        if "authoring.critic" in j:
            return (True, 0, json.dumps(verdict), "")
        return (True, 0, "{}", "")

    return fake_run, fake_run_rc


def _raiser(*_a, **_k):
    raise AssertionError("a child subprocess was spawned on a path that must not spawn one")


def _wire(monkeypatch, root):
    monkeypatch.setattr(RD, "ROOT", root)
    monkeypatch.setattr(RD, "BRIEF_DIR", root / "data" / "briefs")
    monkeypatch.setattr(RD, "MEMPACK_DIR", root / "data" / "memory_packs")
    monkeypatch.setattr(RD, "RESEARCH_DIR", root / "data" / "research")
    monkeypatch.setattr(RD, "SOCIAL_DIR", root / "data" / "social")
    monkeypatch.setattr(RD, "PRED_DIR", root / "data" / "predictions")
    monkeypatch.setattr(RD, "BRIEF_AUTHORING", True)
    monkeypatch.setattr(RD, "BRIEF_BATCH", False)
    monkeypatch.setattr(RD.mp, "build_pack", lambda a, as_of=None: {"budget": {"approx_tokens": 222}})
    monkeypatch.delenv("ASSETFRAME_SANDBOX", raising=False)


def _drive(monkeypatch, root, argv, fake_run, fake_run_rc):
    _wire(monkeypatch, root)
    monkeypatch.setattr(RD, "_run", fake_run)
    monkeypatch.setattr(RD, "_run_rc", fake_run_rc)
    monkeypatch.setattr(sys, "argv", ["run_daily"] + argv)
    RD.main()
    return _read_manifest(root)


def _read_manifest(root):
    files = list((root / "runs").glob("*/run_manifest.json"))
    assert files, "main() wrote no runs/<date>/run_manifest.json"
    return json.loads(files[0].read_text(encoding="utf-8"))


def _job(manifest, ticker):
    return next(j for j in manifest["jobs"] if j["ticker"] == ticker)


def _write_prediction(pred_dir, report_id, wstart, wend):
    """A synthetic closed-window prediction so score_step actually spawns score_report (+ candle
    refresh) — exercises the SCORING leg of the chain end-to-end."""
    pred_dir.mkdir(parents=True, exist_ok=True)
    (pred_dir / f"{report_id.rsplit('-', 1)[-1]}_predictions.json").write_text(
        json.dumps({"report_id": report_id, "window_start_utc": wstart, "window_end_utc": wend}),
        encoding="utf-8")


# ============================================================ Part A — tests
def test_dry_run_builds_plan_only_and_spawns_nothing(monkeypatch, tmp_path):
    # A Saturday: crypto due 24/7, every other class weekend-skipped — both plan branches present.
    as_of = "2026-06-27 06:00"
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "dry_run", "--universe", REAL_UNIVERSE, "--as-of", as_of],
                      _raiser, _raiser)   # _raiser PROVES no child is spawned on a dry run

    assets = RD.config_loader.load_assets(REAL_UNIVERSE)
    holidays = RD.calendar_rules.load_holidays()
    now = RD.resolve_now({"as_of": as_of, "date": None})
    assert manifest["mode"] == "dry_run"
    assert manifest["assets_selected"] == len(assets)
    plan_by_id = {p["asset_id"]: p for p in manifest["plan"]}
    # the manifest plan IS calendar_rules.is_due over the REAL universe (scheduler<->calendar contract).
    for a in assets:
        due, reason = RD.calendar_rules.is_due(a, now, holidays)
        assert plan_by_id[a["id"]]["decision"] == ("generate" if due else "skip"), a["id"]
        assert plan_by_id[a["id"]]["reason"] == reason, a["id"]
    assert plan_by_id["btc"]["decision"] == "generate"
    assert {p["decision"] for p in manifest["plan"]} == {"generate", "skip"}
    # dry_run writes the plan ONLY — no generation/scoring artefacts.
    for k in ("jobs", "generated", "score", "retention"):
        assert k not in manifest, f"dry_run manifest must not carry {k!r}"


def test_generate_only_full_stage_chain_paths_and_order(monkeypatch, tmp_path):
    verdict = {"decision": "approve", "summary": "solid setup",
               "_telemetry": {"input_tokens": 40, "output_tokens": 8, "est_cost_usd": 0.02}}
    calls = []
    fr, frc = _make_children(tmp_path, verdict=verdict, report_id="daily-2026-06-26-BTC", calls=calls)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)

    job = _job(manifest, "BTC")
    assert job["status"] == "generated"
    assert job["brief_source"] == "authored"
    assert job["critic_decision"] == "approve"
    assert job["stages"] == {"intraday": "ok", "memory_pack": "ok", "brief": "authored",
                             "ledger_context": "ok", "scaffold": "ok", "mvp_report": "ok"}

    # STAGE HAND-OFF ORDER end-to-end across the refactored subgroups: the exact ordered sequence of
    # spawned modules (memory_pack is in-process so absent). A reordered/renamed stage fails here.
    assert _modules(calls) == [
        CANON["intraday"], CANON["brief_writer"], CANON["critic"],
        CANON["ledger_context"], CANON["scaffold"], CANON["render"]]
    for mod in _modules(calls):
        _assert_resolves(mod)

    # FILE CONTRACT: the report_id the orchestrator surfaced == what the scaffold child wrote to the
    # payload _finish_asset reads back. Proves data/payloads/<TK>_af_payload.json is the shared seam.
    assert job["report_id"] == "daily-2026-06-26-BTC"
    payload = tmp_path / "data" / "payloads" / "BTC_af_payload.json"
    assert json.loads(payload.read_text())["report_id"] == job["report_id"]
    assert (tmp_path / "data" / "analysis" / "BTC_analysis.json").exists()
    assert manifest["generated"] == 1


def test_generate_only_intraday_failure_short_circuits_before_authoring(monkeypatch, tmp_path):
    # A broken FIRST stage must stop the chain: no brief/scaffold/render spawned (whole-chain guard).
    calls = []
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"}, intraday_ok=False, calls=calls)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "generate_only", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)
    job = _job(manifest, "BTC")
    assert job["status"] == "data_error"
    mods = _modules(calls)
    assert mods == [CANON["intraday"]], "only intraday should have been attempted"
    assert CANON["brief_writer"] not in mods and CANON["scaffold"] not in mods
    assert manifest["generated"] == 0


def test_score_only_spawns_scoring_leg_paths(monkeypatch, tmp_path):
    # closed-window prediction (yesterday) + as-of today -> the scoring leg actually runs.
    _write_prediction(tmp_path / "data" / "predictions",
                      "daily-2026-06-26-BTC", "2026-06-26 06:00", "2026-06-26 21:00")
    calls = []
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"}, calls=calls)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "score_only", "--asset", "btc",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-27 06:00"],
                      fr, frc)

    # the manifest carries the score block (no generation in score_only).
    assert "score" in manifest and "jobs" not in manifest
    s = manifest["score"]
    assert len(s["scored"]) == 1 and s["scored"][0]["report_id"] == "daily-2026-06-26-BTC"

    mods = set(_modules(calls))
    # candle refresh for the closed window + the scorer + the three memory-refresh children.
    for key in ("intraday", "score_report", "calibrate", "research_memory", "ledger_db"):
        assert CANON[key] in mods, f"score_only must spawn {key} ({CANON[key]})"
    # no generation stages leaked into a score-only run.
    assert CANON["scaffold"] not in mods and CANON["render"] not in mods
    for mod in mods:
        _assert_resolves(mod)


def test_production_carries_both_score_and_generation_paths(monkeypatch, tmp_path):
    _write_prediction(tmp_path / "data" / "predictions",
                      "daily-2026-06-25-BTC", "2026-06-25 06:00", "2026-06-25 21:00")
    calls = []
    fr, frc = _make_children(tmp_path, verdict={"decision": "approve"},
                             report_id="daily-2026-06-26-BTC", calls=calls)
    manifest = _drive(monkeypatch, tmp_path,
                      ["--mode", "production", "--asset", "btc", "--workers", "1",
                       "--universe", REAL_UNIVERSE, "--as-of", "2026-06-26 06:00"],
                      fr, frc)

    # BOTH legs in ONE manifest: scoring + generation.
    assert manifest["score"]["scored"][0]["report_id"] == "daily-2026-06-25-BTC"
    assert manifest["generated"] == 1 and _job(manifest, "BTC")["status"] == "generated"
    assert "retention" in manifest      # non-dry run prunes

    mods = set(_modules(calls))
    # the UNION of the scoring + generation module paths must be present AND resolvable.
    for key in ("intraday", "score_report", "calibrate", "research_memory", "ledger_db",
                "brief_writer", "critic", "ledger_context", "scaffold", "render"):
        assert CANON[key] in mods, f"production must spawn {key} ({CANON[key]})"
        _assert_resolves(CANON[key])


def test_no_spawned_path_is_a_stale_module(monkeypatch, tmp_path_factory):
    """The catch-all: drive EVERY generating/scoring mode, union ALL spawned `-m` module strings, and
    assert each resolves. This is the single assertion that would go red for ANY stale post-refactor
    path anywhere in run_daily's chain (no per-stage allow-listing — it validates whatever is spawned)."""
    all_cmds = []
    monkeypatch_modes = (
        ("score_only", "daily-2026-06-26-BTC", "2026-06-26 21:00", "2026-06-27 06:00"),
        ("generate_only", None, None, "2026-06-26 06:00"),
        ("production", "daily-2026-06-25-BTC", "2026-06-25 21:00", "2026-06-26 06:00"),
    )
    for mode, rid, wend, as_of in monkeypatch_modes:
        root = tmp_path_factory.mktemp(f"e2e-{mode}")
        with pytest.MonkeyPatch.context() as mp:
            if rid:
                _write_prediction(root / "data" / "predictions", rid,
                                  as_of.split()[0] + " 06:00", wend)
            calls = []
            fr, frc = _make_children(root, verdict={"decision": "approve"}, calls=calls)
            _drive(mp, root,
                   ["--mode", mode, "--asset", "btc", "--workers", "1",
                    "--universe", REAL_UNIVERSE, "--as-of", as_of],
                   fr, frc)
            all_cmds.extend(calls)

    spawned = set(_modules(all_cmds))
    assert spawned, "no child stages were captured — the driver is mis-wired"
    # every spawned module is a real, importable, scripts.* dotted path (no legacy/typo'd path slipped
    # through the Phase-5 move).
    for mod in spawned:
        assert mod.startswith("scripts."), f"a non-package module path was spawned: {mod!r}"
        _assert_resolves(mod)


# ============================================================ Part B — coordination runner fakes
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows if rows is not None else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    """Records executed SQL; the ONLY Neon boundary. Every coordination module under test is real."""

    def __init__(self, results=None):
        self.results = results or {}
        self.executed = []
        self.tx_depth = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = None
        for key, val in self.results.items():
            if key in sql.lower():
                rows = val(params) if callable(val) else val
                break
        return FakeCursor(rows)

    def transaction(self):
        outer = self

        class _Tx:
            def __enter__(self_):
                outer.tx_depth += 1
                return self_

            def __exit__(self_, *exc):
                outer.tx_depth -= 1
                return False
        return _Tx()

    def find(self, needle):
        return [(s, p) for s, p in self.executed if needle in s.lower()]

    def first(self, needle):
        hits = self.find(needle)
        return hits[0] if hits else None


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeProc:
    """subprocess.Popen stand-in: poll() returns 0 at once so _exec_run_daily completes immediately."""

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def _write_run_manifest(root, payload, date="2026-06-28"):
    """A real runs/<date>/run_manifest.json with a far-future mtime so _read_run_manifest's `since`
    filter (captured at Popen time) always accepts it."""
    sub = root / "runs" / date
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / "run_manifest.json"
    f.write_text(json.dumps(payload), encoding="utf-8")
    future = 2_000_000_000          # year 2033 — newer than any test's `start`
    os.utime(f, (future, future))
    return f


# ============================================================ Part B — tests
def test_run_and_record_lifecycle_launches_post_refactor_paths(monkeypatch, tmp_path):
    payload = {"run_id": "daily-x", "mode": "production", "run_date": "2026-06-28", "generated": 1,
               "jobs": [{"asset_id": "btc", "ticker": "BTC", "status": "generated",
                         "report_id": "AF-202606281200-BTC", "errors": None}]}
    _write_run_manifest(tmp_path, payload)
    conn = FakeConn()
    popen_cmds, run_cmds = [], []

    def _fake_popen(cmd, **kw):
        popen_cmds.append(cmd)
        return FakeProc()

    def _fake_run(cmd, **kw):
        run_cmds.append(cmd)
        return FakeCompleted(0, "ok")

    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / ".run.lock")
    monkeypatch.setattr(MF, "ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(runner.subprocess, "run", _fake_run)

    run_id = runner.run_and_record(conn, trigger="manual", scope={"assets": ["btc"]})

    # the run_daily child was launched via `-m scripts.scheduler.run.run_daily` with the scoped argv.
    assert len(popen_cmds) == 1
    argv = popen_cmds[0]
    assert _module_of(argv) == CANON["run_daily"]
    _assert_resolves(_module_of(argv))
    assert "--asset" in argv and "btc" in argv

    # generated -> the publish chain ran export -> publish -> sync IN ORDER, on post-refactor paths.
    pub_mods = _modules(run_cmds)                # node sync-db has no `-m`, so it drops out here
    assert pub_mods == [CANON["export_content"], CANON["publish"]]
    for mod in pub_mods:
        _assert_resolves(mod)
    joined = [" ".join(str(x) for x in c) for c in run_cmds]
    assert "sync-db" in joined[-1], "sync-db.mjs must be the final publish step"

    # engine_runs lifecycle: INSERT('running') then terminal UPDATE('done'); current_run_id set+cleared.
    ins = conn.first("insert into engine_runs")
    assert ins is not None and ins[1][0] == run_id
    fin = conn.first("update engine_runs set status = %s")
    assert fin[1][0] == "done", f"run should finish done (errors={fin[1][2]!r})"
    results = json.loads(fin[1][1])
    assert results.get("generated") == 1 and results.get("publish") == "ok"
    crun = [p for s, p in conn.executed if "set current_run_id" in s.lower()]
    assert crun[0][0] == run_id and crun[-1][0] is None     # claimed then cleared (load-bearing order)


def test_run_backtest_batch_lifecycle_and_sync_path(monkeypatch, tmp_path):
    # leftover sim state that _wipe_sandbox_state must clear before the run starts.
    for sub in runner.SANDBOX_DIRS:
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
        (tmp_path / sub / "old.json").write_text("{}", encoding="utf-8")
    _write_run_manifest(tmp_path, {"run_id": "bt", "generated": 1,
                                   "score": {"scored": ["btc"], "skipped": [], "errors": []},
                                   "jobs": []})
    conn = FakeConn()
    popen_cmds, run_cmds = [], []

    def _fake_popen(cmd, **kw):
        popen_cmds.append(cmd)
        return FakeProc()

    def _fake_run(cmd, **kw):
        run_cmds.append(cmd)
        return FakeCompleted(0, "synced")

    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "LOCK_PATH", tmp_path / ".run.lock")
    monkeypatch.setattr(MF, "ROOT", tmp_path)
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(runner.subprocess, "run", _fake_run)

    run_id = runner.run_backtest_batch(conn, ["btc"], "2026-06-10 12:00", days=2)

    # two backdated days, each launching run_daily --sandbox via the post-refactor module path.
    assert len(popen_cmds) == 2
    for argv in popen_cmds:
        assert _module_of(argv) == CANON["run_daily"]
        assert "--sandbox" in argv and "btc" in argv
    _assert_resolves(CANON["run_daily"])

    # the batch sync pushes ledger/sim -> Neon via `-m scripts.analytics.store.sync_backtest`.
    assert _modules(run_cmds) == [CANON["sync_backtest"]]
    _assert_resolves(CANON["sync_backtest"])

    # engine_runs lifecycle: the LITERAL 'backtest' trigger INSERT + a terminal 'done' summary jsonb.
    ins = conn.first("insert into engine_runs")
    assert "'backtest'" in ins[0].lower() and len(ins[1]) == 2   # literal trigger binds (run_id, scope)
    fin = conn.first("update engine_runs set status = %s")
    assert fin[1][0] == "done"
    results = json.loads(fin[1][1])
    assert results["sandbox"] is True and results["days"] == 2 and len(results["day_runs"]) == 2
    assert results["total_scored"] == 2                          # 1 scored/day * 2 days
    # sandbox working trees were wiped before the run.
    for sub in runner.SANDBOX_DIRS:
        assert list((tmp_path / sub).iterdir()) == [], f"{sub} not wiped"
    # the Neon backtest tables were cleared (admin-only sandbox tables, never the live ledger).
    deletes = " ".join(s.lower() for s, _ in conn.executed if s.lower().startswith("delete from"))
    assert "backtest_results" in deletes and "backtest_predictions" in deletes


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
