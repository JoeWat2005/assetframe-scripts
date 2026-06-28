# AssetFrame — engine (`assetframe-script`)

The AssetFrame **forecast engine**: market data → analysis → AI research brief → deterministic
compiler → confidence → report → scoring → append‑only ledger → publish. It pairs with
**`assetframe-infra`** (the Next.js site + Clerk auth, Neon, R2, Lemon Squeezy). **This repo
*writes*; the website *reads*.**

## Two‑repo architecture

| Repo | Owns |
|---|---|
| **`assetframe-script`** (this) | Python engine + the publish chain — generation, scoring, the ledger, and writing to R2 + Neon. |
| **`assetframe-infra`** | The website + infra — auth, billing, the DB **schema** (migrations), and *reading* R2/Neon to serve users. |

They share **R2** (report files) and **Neon** (tables) — **no shared code**. The contract:

- **R2** — private bucket, key format `<date>/<slug>/<file>` (`free`/`pro` × `html`/`pdf`, plus `preview.png`).
- **Neon** — the engine `INSERT`s, the web `SELECT`s: `editions`, `open_calls`,
  `open_call_predictions`, `scored_results` (id format `AF-YYYYMMDD-<SLUG>` / `report_ref`).
  **Schema migrations live in `assetframe-infra`; the engine only inserts data.**
- **Enums** — `scripts/pipeline/scoring/taxonomy.py` (confidence buckets / asset classes) is mirrored in the infra
  repo's `web/lib/content.ts`. Keep the two in sync.

## Setup

```bash
pip install -r requirements.txt     # fpdf2 (PDF), pymupdf (preview)
pip install boto3                   # only for publish.py (R2 upload)
npm install                         # @neondatabase/serverless, for sync-db.mjs
cp .env.example .env                # fill R2_*, DATABASE_URL[_DEV], EODHD/ANTHROPIC
```

## Daily run (the scheduler decides; Claude only writes/criticises)

The engine is a Python package (`scripts/` with concern subpackages: `pipeline`, `scheduler`,
`analytics`, `delivery`, `coordination`). Run every entrypoint as a module from the repo root:

```bash
python -m scripts.scheduler.config.validate_config        # validate config/assets.json
python -m scripts.scheduler.run.run_daily --mode dry_run       # resolve today's DUE assets, write nothing
python -m scripts.scheduler.run.run_daily --mode score_only    # score closed windows + refresh memory (idempotent)
python -m scripts.scheduler.run.run_daily --mode generate_only # generate due assets
```
Modes: `dry_run | score_only | generate_only | production` (Phase 2 adds `approval`). Scope with
`--asset <id>` or `--asset-class fx`. Run manifests land in `runs/<date>/run_manifest.json`.

## Publish chain (engine → site)

```bash
python -m scripts.delivery.export_content   # -> content/catalog.json + content/track-record.json
python -m scripts.delivery.publish          # -> uploads report files to R2
npm run sync-db                     # -> writes editions + track record into Neon (both branches)
```

`data/`, `reports/`, `content/`, `runs/` are gitignored working artifacts (regenerated each run).
The append‑only `ledger/outcome_ledger.csv` is the tracked source of truth.

## Tests

```bash
for t in scripts/test_*.py; do python "$t"; done                 # bash / CI
```
```powershell
Get-ChildItem scripts\test_*.py | ForEach-Object { python $_.FullName }   # PowerShell
```

The full report pipeline is documented in `.claude/skills/mvp/SKILL.md`. Decision‑support only —
not regulated financial advice; **no auto‑trading; no auto‑publish** (a human approval gate gates
every report before it goes live on the site).
