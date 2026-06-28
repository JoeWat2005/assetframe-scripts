// Sync the generated content (web/content/*.json) into Neon Postgres.
// Run after `python -m scripts.delivery.export_content`:  node scripts/sync-db.mjs
// Applies to EVERY configured target so one publish updates prod AND the dev branch:
//   DATABASE_URL        (primary / production — Neon main branch)
//   DATABASE_URL_DEV    (optional — Neon `development` branch, used by preview deploys)
// Each target: incrementally UPSERTS editions, open calls (+ predictions) and the scored
// track record. Every write is keyed and idempotent — there is no global DELETE, so a
// partial or empty export can never wipe published history (the append-only promise).
import { neon } from "@neondatabase/serverless";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url)); // <repo>/scripts
const root = path.join(here, "..");                        // engine repo root

// Load env from the engine repo's .env for any keys not already in the environment.
try {
  for (const line of readFileSync(path.join(root, ".env"), "utf-8").split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#") || !t.includes("=")) continue;
    const i = t.indexOf("=");
    const k = t.slice(0, i).trim();
    if (!process.env[k]) process.env[k] = t.slice(i + 1).trim();
  }
} catch { /* no .env */ }

const readJson = (f) => JSON.parse(readFileSync(path.join(root, "content", f), "utf-8"));
const toInt = (v) => (v === "" || v == null || Number.isNaN(Number(v)) ? null : parseInt(v, 10));
// T12 additive columns: pass through only when present (guard undefined/empty -> null).
const orNull = (v) => (v === undefined || v === "" || v === null ? null : v);
const toJson = (v) => (v === undefined || v === null ? null : JSON.stringify(v));

// Targets: primary (prod) + optional dev branch. Dedupe identical URLs.
const primary =
  process.env.DATABASE_URL || process.env.POSTGRES_URL ||
  process.env.STORAGE_DATABASE_URL || process.env.STORAGE_URL;
const dev = process.env.DATABASE_URL_DEV || process.env.DEV_DATABASE_URL;
const targets = [];
if (primary) targets.push(["production", primary]);
if (dev && dev !== primary) targets.push(["dev branch", dev]);

if (targets.length === 0) {
  console.error("No DATABASE_URL — set it in .env or the environment.");
  process.exit(1);
}

// Sanity guard (NOT a wipe guard any more): the sync is fully incremental — upserts only,
// never a global DELETE — so empty/partial content is a harmless no-op and can no longer
// wipe the track record. We still refuse on a MISSING or CORRUPT catalog.json, because that
// signals export_content.py didn't run / broke. But a *zero-edition* catalog now PROCEEDS:
// it lets the scored track record (scored_results) sync on days that produced no new edition
// — the previous "zero editions" abort was exactly why scored rows never reached Neon.
let guardCatalog;
try {
  guardCatalog = readJson("catalog.json");
} catch (err) {
  console.error(`Refusing to sync — cannot read content/catalog.json (${err.message}). Run \`python -m scripts.delivery.export_content\` first.`);
  process.exit(1);
}
if (!Array.isArray(guardCatalog)) {
  console.error("Refusing to sync — content/catalog.json is not an array (corrupt export). Run `python -m scripts.delivery.export_content` first.");
  process.exit(1);
}
if (guardCatalog.length === 0) {
  console.warn("Note: catalog.json has zero editions — proceeding anyway (incremental upsert is non-destructive; the scored track record still syncs).");
}

// Schema is owned by node-pg-migrate (web/migrations). Run `npm run migrate:up` first
// (against each branch), or `npm run db:setup`. This script only syncs DATA.
async function syncOne(label, url) {
  const sql = neon(url);

  // 1. editions (upsert). Per-row try/catch (like the track-record loop below) so a single
  // malformed edition can't throw out of syncOne and skip the whole track-record section — the
  // scored history must still propagate even if one edition row is bad (or a transient HTTP blip
  // hits one upsert). Nothing is deleted; a failed row is counted and surfaced at the end.
  const catalog = readJson("catalog.json");
  let editionFailures = 0;
  for (const e of catalog) {
    const id = `${e.date}/${e.slug}`;
    try {
    await sql.query(
      // Approval gate: `hidden` is set ONLY on INSERT — it is deliberately NOT in the
      // DO UPDATE SET list, so re-running sync never flips an admin's later un-hide
      // (set via the web app) back to true.
      `INSERT INTO editions (id, report_date, slug, instrument, ticker, asset_class, status, risk, bias,
         data_quality, window_end, catalyst_status, has_pro, free_html_key, free_pdf_key, preview_key,
         pro_html_key, pro_pdf_key,
         asset_class_key, direction_view, prediction_type, market_regime, confidence_band, social_context,
         hidden, report_id, scored_cadence, chart_intervals, forecast_window)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
         $19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29)
       ON CONFLICT (id) DO UPDATE SET
         report_date=excluded.report_date, slug=excluded.slug, instrument=excluded.instrument,
         ticker=excluded.ticker, asset_class=excluded.asset_class, status=excluded.status,
         risk=excluded.risk, bias=excluded.bias, data_quality=excluded.data_quality,
         window_end=excluded.window_end, catalyst_status=excluded.catalyst_status, has_pro=excluded.has_pro,
         free_html_key=excluded.free_html_key, free_pdf_key=excluded.free_pdf_key,
         preview_key=excluded.preview_key, pro_html_key=excluded.pro_html_key, pro_pdf_key=excluded.pro_pdf_key,
         asset_class_key=excluded.asset_class_key, direction_view=excluded.direction_view,
         prediction_type=excluded.prediction_type, market_regime=excluded.market_regime,
         confidence_band=excluded.confidence_band, social_context=excluded.social_context,
         report_id=excluded.report_id, scored_cadence=excluded.scored_cadence,
         chart_intervals=excluded.chart_intervals, forecast_window=excluded.forecast_window`,
      [id, e.date, e.slug, e.instrument, e.ticker, e.assetClass, e.status, e.risk, e.bias,
       toInt(e.dataQuality), e.windowEnd, e.catalystStatus, !!e.hasPro, e.freeHtml, e.freePdf, e.preview,
       e.hasPro ? `${e.date}/${e.slug}/pro.html` : null, e.hasPro ? `${e.date}/${e.slug}/pro.pdf` : null,
       // T12 (additive) — pass through when export_content includes them, else null.
       orNull(e.assetClassKey), orNull(e.directionView), orNull(e.predictionType),
       orNull(e.marketRegime), orNull(e.confidenceBand), toJson(e.socialContext),
       // Approval gate (INSERT-only above). Default hidden when export omits the flag.
       e.hidden === false ? false : true,
       // Cadence + intervals (additive): report_id is the cadence-aware join key.
       orNull(e.reportId), orNull(e.scoredCadence), toJson(e.chartIntervals), orNull(e.forecastWindow)]
    );
    } catch (err) {
      // A unique-violation on report_id means another edition (a different date in the same week/
      // month) already owns this period's report_id — expected on a within-period re-run. Skip it
      // (the existing period edition stands) rather than failing the whole sync.
      if (/report_id/i.test(err.message || "") && /duplicate|unique/i.test(err.message || "")) {
        console.warn(`  [${label}] edition ${id} skipped — its period report_id is already taken (re-run).`);
      } else if (/column .* does not exist/i.test(err.message || "")) {
        // A missing column = this branch's migrations LAG the engine (deploy skew). Warn + skip rather
        // than failing the WHOLE sync (which would mark an otherwise-good PROD publish 'failed' when a
        // DEV branch lags) — the edition syncs once the migration runs. Mirrors the provenance-UPDATE
        // and scored_results lag-tolerance below.
        console.warn(`  [${label}] edition ${id} skipped — a column is missing (migrations lag this branch); syncs once migrated.`);
      } else {
        editionFailures++;
        console.error(`  [${label}] edition ${id} FAILED: ${err.message}`);
      }
    }
    // data-source provenance (best-effort, DECOUPLED from the critical upsert above): the
    // data_provider/data_license columns are added by a later migration, so tolerate their absence
    // — the badge just stays dark until the migration runs; the editions sync NEVER breaks on it.
    if (e.dataProvider) {
      try {
        await sql.query(
          `UPDATE editions SET data_provider=$1, data_license=$2, data_license_degraded=$3 WHERE id=$4`,
          [orNull(e.dataProvider), orNull(e.dataLicense), !!e.dataLicenseDegraded, id]);
      } catch (err) {
        if (!/column .* does not exist/i.test(err.message || "")) {
          console.warn(`  [${label}] edition ${id} provenance update skipped: ${err.message}`);
        }
      }
    }
  }

  // 2. track record — INCREMENTAL UPSERT (no global DELETE; every write is keyed + idempotent):
  //    - open_calls            ON CONFLICT (report_id)            [PK]
  //    - open_call_predictions ON CONFLICT (report_id, pred_id)   + a per-report prune of
  //                            predictions that vanished from THAT report (scoped — never
  //                            touches other reports)
  //    - scored_results        ON CONFLICT (report_id)            [the append-only TRACK RECORD —
  //                            rows are only inserted or refreshed in place, NEVER deleted]
  //  The old DELETE+INSERT was the wipe foot-gun: non-transactional on the HTTP driver, a crash
  //  between the DELETE and the re-INSERT left the track record empty, and a partial/empty export
  //  replaced good history with nothing. Upsert-only removes both failure modes entirely.
  const track = readJson("track-record.json");

  // scored_results has no natural key in the baseline schema (bigserial PK only), so its upsert
  // needs a unique target. report_id is unique per scored report (one append-only ledger row per
  // closed window). The CANONICAL definition is the web migration (web/migrations/
  // 1750000019000_scored-results-upsert-key.js); this IF-NOT-EXISTS create is a belt-and-suspenders
  // for the two-repo split so the engine can still upsert against a Neon branch whose migrations
  // lag. It is a no-op once the migration has run (same index name).
  try {
    // Dedupe FIRST (keep max(id) per report_id) so the unique index can build even if a legacy
    // duplicate slipped in via the old constraint-free INSERT path — byte-aligned with
    // web/migrations/1750000019000. No-op on clean data (and on the empty pre-launch table).
    await sql.query("DELETE FROM scored_results a USING scored_results b WHERE a.report_id IS NOT NULL AND a.report_id = b.report_id AND a.id < b.id");
    await sql.query("CREATE UNIQUE INDEX IF NOT EXISTS scored_results_report_id_uniq ON scored_results (report_id)");
  } catch (err) {
    console.warn(`  [${label}] could not ensure scored_results unique index (${err.message}); the scored upsert may fail — run \`npm run migrate:up\`.`);
  }

  let predCount = 0;
  let trackFailures = 0;
  for (const c of track.open || []) {
    if (!c.reportId) { console.warn(`  [${label}] open call with no reportId skipped (${c.instrument || "?"})`); continue; }
    try {
      await sql.query(
        `INSERT INTO open_calls (report_id, instrument, symbol, view, confidence, window_end, n, n_manual, hits, scored)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (report_id) DO UPDATE SET
           instrument=excluded.instrument, symbol=excluded.symbol, view=excluded.view,
           confidence=excluded.confidence, window_end=excluded.window_end, n=excluded.n,
           n_manual=excluded.n_manual, hits=excluded.hits, scored=excluded.scored`,
        [c.reportId, c.instrument, c.symbol, c.view, String(c.confidence), c.windowEnd, c.n || 0, c.nManual || 0,
         c.hits || 0, !!c.scored]
      );
      const preds = c.predictions || [];
      const keepIds = [];
      for (let i = 0; i < preds.length; i++) {
        const p = preds[i];
        const predId = p.id || `P${i + 1}`;
        keepIds.push(predId);
        await sql.query(
          `INSERT INTO open_call_predictions (report_id, seq, pred_id, type, text, manual, expect,
             pred_type, verdict, setup_side)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
           ON CONFLICT (report_id, pred_id) DO UPDATE SET
             seq=excluded.seq, type=excluded.type, text=excluded.text,
             manual=excluded.manual, expect=excluded.expect,
             pred_type=excluded.pred_type, verdict=excluded.verdict, setup_side=excluded.setup_side`,
          [c.reportId, i + 1, predId, p.type || "", p.text || "",
           !!p.manual, typeof p.expect === "boolean" ? p.expect : null,
           // T12 (additive) — verdict + predType are emitted by export_content; setup_side is reserved.
           orNull(p.predType), orNull(p.verdict), orNull(p.setupSide)]
        );
        predCount++;
      }
      // Prune predictions that disappeared from THIS report only (replaces the old cascade from
      // DELETE FROM open_calls). Upserts ran first, so a crash here leaves extra/stale rows, never
      // missing ones. An empty incoming set clears any leftovers for this report.
      if (keepIds.length) {
        await sql.query(
          "DELETE FROM open_call_predictions WHERE report_id = $1 AND NOT (pred_id = ANY($2::text[]))",
          [c.reportId, keepIds]
        );
      } else {
        await sql.query("DELETE FROM open_call_predictions WHERE report_id = $1", [c.reportId]);
      }
    } catch (err) {
      // An open call whose parent edition isn't in this sync (e.g. a still-pending call for a
      // report the catalog scoped out via --since, or whose edition is awaiting approval) hits the
      // FK to editions(report_ref). That's expected — skip+warn WITHOUT failing the run, so a single
      // out-of-window pending call can't poison the exit code (and block) every sync. Other errors
      // are real failures.
      if (/foreign key/i.test(err.message || "")) {
        console.warn(`  [${label}] open_call ${c.reportId} skipped — its edition isn't in this sync (FK).`);
      } else {
        trackFailures++;
        console.error(`  [${label}] open_call ${c.reportId} FAILED: ${err.message}`);
      }
    }
  }

  let scoredCount = 0;
  for (const r of track.scored || []) {
    // A scored row with no report_id can't be upserted idempotently (every sync would duplicate
    // it), so skip it. In practice the append-only ledger always carries report_id.
    if (!r.reportId) { console.warn(`  [${label}] scored row with no reportId skipped (${r.instrument || "?"})`); continue; }
    try {
      await sql.query(
        `INSERT INTO scored_results (report_id, instrument, view, confidence, results, hits, misses, hit_rate, window_end,
           conf_version, confidence_components, scored_cadence)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
         ON CONFLICT (report_id) DO UPDATE SET
           instrument=excluded.instrument, view=excluded.view, confidence=excluded.confidence,
           results=excluded.results, hits=excluded.hits, misses=excluded.misses,
           hit_rate=excluded.hit_rate, window_end=excluded.window_end,
           conf_version=excluded.conf_version, confidence_components=excluded.confidence_components,
           scored_cadence=excluded.scored_cadence`,
        [r.reportId, r.instrument, r.view, String(r.confidence), r.results,
         toInt(r.hits), toInt(r.misses), String(r.hitRate), r.windowEnd,
         // T12 (additive) — present only when export_content emits them.
         toInt(r.confVersion), toJson(r.confidenceComponents),
         // cadence (additive): daily | weekly | monthly, for per-period track-record grouping.
         orNull(r.scoredCadence)]
      );
      scoredCount++;
    } catch (err) {
      trackFailures++;
      console.error(`  [${label}] scored_result ${r.reportId} FAILED: ${err.message}`);
    }
  }
  console.log(`  [${label}] editions: ${catalog.length - editionFailures}/${catalog.length}, `
    + `open_calls: ${(track.open || []).length} (${predCount} predictions), `
    + `scored_results: ${scoredCount}/${(track.scored || []).length}`
    + `${editionFailures + trackFailures ? `, ${editionFailures + trackFailures} row failure(s)` : ""}`);
  // Surface partial failures as a failed sync (the outer loop exits 1) WITHOUT having wiped
  // anything — the upserts that did land are preserved; a re-sync heals the rest. Crucially the
  // track-record section ran regardless of edition failures, so scored history still propagated.
  if (editionFailures + trackFailures) throw new Error(`${editionFailures + trackFailures} row(s) failed to sync`);
}

let failures = 0;
for (const [label, url] of targets) {
  try {
    await syncOne(label, url);
  } catch (err) {
    failures++;
    console.error(`  [${label}] FAILED: ${err.message}`);
  }
}
console.log(targets.length > 1
  ? `done — synced ${targets.length} database(s)${failures ? `, ${failures} failed` : ""}`
  : "done — synced to Neon");
if (failures) process.exit(1);
