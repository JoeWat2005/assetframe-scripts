// Sync the generated content (web/content/*.json) into Neon Postgres.
// Run after `python scripts/export_content.py`:  node scripts/sync-db.mjs
// Applies to EVERY configured target so one publish updates prod AND the dev branch:
//   DATABASE_URL        (primary / production — Neon main branch)
//   DATABASE_URL_DEV    (optional — Neon `development` branch, used by preview deploys)
// Each target: upserts editions, replaces the track-record snapshot (open calls + scored).
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

// Wipe-foot-gun guard: syncOne snapshot-replaces open_calls + scored_results, so syncing
// empty/missing content would wipe the track record. Refuse up front — before any DB is
// touched — if catalog.json is absent or has zero editions. Run export_content.py first.
let guardCatalog;
try {
  guardCatalog = readJson("catalog.json");
} catch (err) {
  console.error(`Refusing to sync — cannot read content/catalog.json (${err.message}). Run \`python scripts/export_content.py\` first.`);
  process.exit(1);
}
if (!Array.isArray(guardCatalog) || guardCatalog.length === 0) {
  console.error("Refusing to sync — content/catalog.json has zero editions (syncing empty content would wipe the track record). Run `python scripts/export_content.py` first.");
  process.exit(1);
}

// Schema is owned by node-pg-migrate (web/migrations). Run `npm run migrate:up` first
// (against each branch), or `npm run db:setup`. This script only syncs DATA.
async function syncOne(label, url) {
  const sql = neon(url);

  // 1. editions (upsert)
  const catalog = readJson("catalog.json");
  for (const e of catalog) {
    const id = `${e.date}/${e.slug}`;
    await sql.query(
      // Approval gate: `hidden` is set ONLY on INSERT — it is deliberately NOT in the
      // DO UPDATE SET list, so re-running sync never flips an admin's later un-hide
      // (set via the web app) back to true.
      `INSERT INTO editions (id, report_date, slug, instrument, ticker, asset_class, status, risk, bias,
         data_quality, window_end, catalyst_status, has_pro, free_html_key, free_pdf_key, preview_key,
         pro_html_key, pro_pdf_key,
         asset_class_key, direction_view, prediction_type, market_regime, confidence_band, social_context,
         hidden)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,
         $19,$20,$21,$22,$23,$24,$25)
       ON CONFLICT (id) DO UPDATE SET
         report_date=excluded.report_date, slug=excluded.slug, instrument=excluded.instrument,
         ticker=excluded.ticker, asset_class=excluded.asset_class, status=excluded.status,
         risk=excluded.risk, bias=excluded.bias, data_quality=excluded.data_quality,
         window_end=excluded.window_end, catalyst_status=excluded.catalyst_status, has_pro=excluded.has_pro,
         free_html_key=excluded.free_html_key, free_pdf_key=excluded.free_pdf_key,
         preview_key=excluded.preview_key, pro_html_key=excluded.pro_html_key, pro_pdf_key=excluded.pro_pdf_key,
         asset_class_key=excluded.asset_class_key, direction_view=excluded.direction_view,
         prediction_type=excluded.prediction_type, market_regime=excluded.market_regime,
         confidence_band=excluded.confidence_band, social_context=excluded.social_context`,
      [id, e.date, e.slug, e.instrument, e.ticker, e.assetClass, e.status, e.risk, e.bias,
       toInt(e.dataQuality), e.windowEnd, e.catalystStatus, !!e.hasPro, e.freeHtml, e.freePdf, e.preview,
       e.hasPro ? `${e.date}/${e.slug}/pro.html` : null, e.hasPro ? `${e.date}/${e.slug}/pro.pdf` : null,
       // T12 (additive) — pass through when export_content includes them, else null.
       orNull(e.assetClassKey), orNull(e.directionView), orNull(e.predictionType),
       orNull(e.marketRegime), orNull(e.confidenceBand), toJson(e.socialContext),
       // Approval gate (INSERT-only above). Default hidden when export omits the flag.
       e.hidden === false ? false : true]
    );
  }

  // 2. track record (snapshot — replace open_calls + predictions + scored_results)
  const track = readJson("track-record.json");
  await sql.query("DELETE FROM open_calls"); // cascades to open_call_predictions
  let predCount = 0;
  for (const c of track.open || []) {
    await sql.query(
      `INSERT INTO open_calls (report_id, instrument, symbol, view, confidence, window_end, n, n_manual, hits, scored)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
      [c.reportId, c.instrument, c.symbol, c.view, String(c.confidence), c.windowEnd, c.n || 0, c.nManual || 0,
       c.hits || 0, !!c.scored]
    );
    const preds = c.predictions || [];
    for (let i = 0; i < preds.length; i++) {
      const p = preds[i];
      await sql.query(
        `INSERT INTO open_call_predictions (report_id, seq, pred_id, type, text, manual, expect,
           pred_type, verdict, setup_side)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (report_id, pred_id) DO UPDATE SET
           seq=excluded.seq, type=excluded.type, text=excluded.text,
           manual=excluded.manual, expect=excluded.expect,
           pred_type=excluded.pred_type, verdict=excluded.verdict, setup_side=excluded.setup_side`,
        [c.reportId, i + 1, p.id || `P${i + 1}`, p.type || "", p.text || "",
         !!p.manual, typeof p.expect === "boolean" ? p.expect : null,
         // T12 (additive) — verdict + predType are emitted by export_content; setup_side is reserved.
         orNull(p.predType), orNull(p.verdict), orNull(p.setupSide)]
      );
      predCount++;
    }
  }
  await sql.query("DELETE FROM scored_results");
  for (const r of track.scored || []) {
    await sql.query(
      `INSERT INTO scored_results (report_id, instrument, view, confidence, results, hits, misses, hit_rate, window_end,
         conf_version, confidence_components)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
      [r.reportId || null, r.instrument, r.view, String(r.confidence), r.results,
       toInt(r.hits), toInt(r.misses), String(r.hitRate), r.windowEnd,
       // T12 (additive) — present only when export_content emits them.
       toInt(r.confVersion), toJson(r.confidenceComponents)]
    );
  }
  console.log(`  [${label}] editions: ${catalog.length}, open_calls: ${(track.open || []).length} (${predCount} predictions), scored_results: ${(track.scored || []).length}`);
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
