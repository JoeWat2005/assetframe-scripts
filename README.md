# assetframe-scripts

The AssetFrame forecast engine: a Python pipeline that generates next-session market research reports, scores their predictions once the forecast window closes, and publishes the results for the AssetFrame website.

**Status:** MVP. The launch runbook covering this engine and the website is `LAUNCH.md` in [assetframe-infra](https://github.com/JoeWat2005/assetframe-infra).

AssetFrame is decision support only. It is not regulated financial advice, it never places trades, and a human approves every report before it appears on the site.

## Features

- Config-driven asset universe (`config/assets.json`) covering FX, crypto, equities, commodities and indices, with per-asset cadence, trading session, data symbols and publish policy
- Market data from Yahoo by default, with optional Twelve Data or EODHD feeds and a CoinGecko fallback for crypto; computes indicators, pivots, ATR bands and session levels
- AI-drafted research briefs: one Anthropic API call writes the brief using web search, and a second call reviews it adversarially before it is used
- Deterministic compilation: price levels, setups, risk/reward, predictions and the confidence score are computed in Python, never written by the model
- Two report tiers, a one-page free Snapshot and a 3–6 page Pro report, rendered as PDF and HTML with a preview image, behind a QA gate
- Scoring after the fact: closed prediction windows are graded against price data and appended to an outcome ledger, which feeds confidence calibration and research memory
- Sandboxed backtests (`--sandbox`) that never touch the live ledger
- Publishing chain to a private Cloudflare R2 bucket and Neon Postgres
- Unattended operation on an Oracle Cloud VM with systemd: a daily timer, an always-on poller for manual requests from the website's admin console, and an optional HTTP control server behind Cloudflare Tunnel and Access

## Tech stack

- Python 3.12 (the version used by CI and the deployment runbook)
- fpdf2 (PDF reports), PyMuPDF (preview images), psycopg 3 (Postgres), boto3 (R2), the Anthropic Python SDK, PyJWT and tzdata
- Node.js 20 and `@neondatabase/serverless` for the database sync script (`scripts/sync-db.mjs`)
- Cloudflare R2, Neon Postgres and, optionally, Upstash Redis
- systemd on an Oracle Cloud (OCI) VM; GitHub Actions for tests and optional deployment

## Getting started

### Prerequisites

- Python 3.12
- Node.js 20 and npm, needed only for `npm run sync-db`
- Optional, depending on what you run: an Anthropic API key (brief authoring), a Cloudflare R2 bucket and API token (publishing), a Neon Postgres database (sync and coordination), and Twelve Data or EODHD keys (licensed market data)

### Installation

```bash
git clone https://github.com/JoeWat2005/assetframe-scripts.git
cd assetframe-scripts
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm install                        # only needed for sync-db
cp .env.example .env
```

### Configuration

Secrets go in `.env`, which is git-ignored. The variables are shared with the website repository, because both use the same R2 bucket and Neon database.

| Variable | Purpose |
|---|---|
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Cloudflare R2 credentials used to upload and delete report files |
| `R2_BUCKET` | Name of the private R2 bucket for report files |
| `DATABASE_URL` | Neon Postgres connection string, used by the poller, the coordination layer and `sync-db.mjs` |
| `DATABASE_URL_DEV` | Optional Neon development branch; `sync-db.mjs` updates it as well as the main database |
| `TWELVEDATA_API_KEY` | Optional Twelve Data key, used when the data provider is `twelvedata` |
| `EODHD_API_KEY` | Optional EODHD key, used when the data provider is `eodhd` |
| `ANTHROPIC_API_KEY` | Anthropic API key for brief authoring and review. Without it, assets fall back to an operator-written brief (`needs_brief`) instead of failing the run |
| `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` | Optional Upstash Redis credentials. The website sets a wake flag here when it queues work; without them the poller checks Neon on every tick |
| `UPSTASH_KEY_PREFIX` | Optional key prefix so that several environments can share one Redis database |
| `ASSETFRAME_CONTROL_PORT` | Port for the control server (defaults to 8787) |
| `ASSETFRAME_CF_TEAM` | Cloudflare Zero Trust team name, used to verify Cloudflare Access tokens |
| `ASSETFRAME_CONTROL_AUD` | Cloudflare Access application audience (AUD) for this environment |
| `ASSETFRAME_CONTROL_TOKEN` | Bearer token required on `POST /control` |
| `ASSETFRAME_CONTROL_INSECURE` | Set to `1` to skip the Cloudflare Access check. For local development only |
| `ASSETFRAME_POLLER_UNIT`, `ASSETFRAME_CONTROL_UNIT` | Optional systemd unit names the control server checks and restarts |

The control server will not start without `ASSETFRAME_CF_TEAM`, `ASSETFRAME_CONTROL_AUD` and `ASSETFRAME_CONTROL_TOKEN`, unless `ASSETFRAME_CONTROL_INSECURE=1`. The poller and the daily run do not need them.

Non-secret runtime settings live in `config/engine.json`. Copy `config/engine.example.json` to create it; without the file, built-in defaults are used. Any value set in the environment overrides the file.

| Setting | Purpose |
|---|---|
| `ADVISOR_DATA_PROVIDER` | Market data provider: `yahoo` (default), `twelvedata` or `eodhd` |
| `ASSETFRAME_DATA_LICENSE` | Data licence recorded with each report: `personal` or `commercial` |
| `ASSETFRAME_BRIEF_MODEL`, `ASSETFRAME_CRITIC_MODEL` | Anthropic models used to write and review briefs |
| `ASSETFRAME_AUTHOR_BRIEFS` | Set to `0` to turn off AI brief authoring |
| `ASSETFRAME_BRIEF_BATCH` | Set to `1` to author and review all briefs through the Anthropic Message Batches API |
| `ASSETFRAME_BRIEF_CONCURRENCY` | Maximum number of briefs authored at the same time |
| `ASSETFRAME_BRIEF_WEB_MAX_USES` | Maximum web searches per brief |
| `ASSETFRAME_RETENTION_DAYS` | Days to keep dated folders under `reports/` and `runs/`; `0` keeps everything |
| `ASSETFRAME_RUN_TIMEOUT` | Time limit for a run, in seconds |
| `TWELVEDATA_RATE_PER_MIN` | Request rate limit for Twelve Data |

### Running locally

Run every entry point as a module from the repository root.

```bash
python -m scripts.scheduler.config.validate_config              # check config/assets.json
python -m scripts.scheduler.run.run_daily --mode dry_run         # plan today's due assets and write the run manifest only
python -m scripts.scheduler.run.run_daily --mode score_only      # score closed windows and refresh ledger memory
python -m scripts.scheduler.run.run_daily --mode generate_only   # generate reports for due assets
python -m scripts.scheduler.run.run_daily --mode production      # score, refresh and generate
```

`dry_run` is the default mode. Narrow a run with `--asset <id>` (repeatable) or `--asset-class <class>`. Other options are `--date`, `--as-of "YYYY-MM-DD HH:MM"`, `--workers`, `--no-render` and `--sandbox`.

Each run writes `runs/<date>/run_manifest.json`. Reports are written to `reports/YYYY-MM-DD/<INSTRUMENT>/` as `free.pdf`, `pro.pdf`, `free.html`, `pro.html`, `metadata.json` and `preview.png`.

### Publishing

```bash
python -m scripts.delivery.export_content   # write content/catalog.json and content/track-record.json
python -m scripts.delivery.publish          # upload report files to R2 (add --dry-run to preview)
npm run sync-db                             # upsert editions and the track record into Neon
```

`python -m scripts.delivery.r2_purge` lists the objects in the bucket and deletes them only when you add `--yes`. `--prefix <date>/` limits it to one edition date.

### Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

CI (`.github/workflows/ci.yml`) runs the same command with Python 3.12 on every push and pull request.

### Deployment

Production runs on an Oracle Cloud VM. `deploy/README.md` is the full runbook: VM set-up, `.env`, systemd units and verification. The units are:

| Unit | Runs |
|---|---|
| `assetframe-poller.service` | `python -m scripts.scheduler.service.poller --interval 30`, the always-on loop that picks up manual requests |
| `assetframe-daily.timer`, `assetframe-daily.service` | `python -m scripts.scheduler.service.scheduled_run` at 04:00 UTC every day |
| `assetframe-control.service` | `python -m scripts.coordination.control_server`, the HTTP control server |
| `*-dev.service` | The same services for a separate development checkout in `/opt/assetframe-scripts-dev` |

The `deploy` job in CI runs after the tests pass, on pushes to `main` or `development`, and only when the repository variable `DEPLOY_ENABLED` is `true`. It runs on a self-hosted runner labelled `assetframe`, fast-forwards the checkout on the VM and restarts the poller.

## How it works

```text
config/assets.json
        |
        v
run_daily.py
  1. resolve due assets (cadence, market calendars, sessions)
  2. score closed prediction windows        -> ledger/outcome_ledger.csv
  3. refresh calibration and research memory
  4. for each due asset:
       market data -> memory pack -> brief (writer + critic)
       -> compiled payload + confidence -> Snapshot and Pro reports (QA gate)
  5. write runs/<date>/run_manifest.json
        |
        v
publish chain: export_content -> publish (R2) -> sync-db.mjs (Neon)
        |
        v
editions arrive hidden on the website until an admin approves them
```

- **Scheduler** (`scripts/scheduler/`): `run/run_daily.py` orchestrates each run. `calendars/` holds the market calendars and session rules, and `config/` loads and validates the asset universe and runtime settings. `service/` contains the poller and the daily scheduled run.
- **Market data** (`scripts/pipeline/marketdata/`): `intraday.py` fetches hourly and daily candles through `data_providers.py`, then computes indicators, pivots, ATR bands and freshness flags.
- **Authoring** (`scripts/pipeline/authoring/`): `brief_writer.py` asks Claude for the research brief, which holds the thesis, scenarios, sourced claims and prediction intent but no prices. `critic.py` reviews it and returns `approve`, `revise`, `reject` or `stand_aside`. `brief_batch.py` is the Message Batches alternative.
- **Scoring and compilation** (`scripts/pipeline/scoring/`): `scaffold_payload.py` binds the brief to price levels computed from the market data and registers the predictions. `confidence.py` computes the confidence score deterministically, and `score_report.py` grades predictions once their window closes.
- **Rendering** (`scripts/pipeline/render/`): `mvp_report.py` builds the Snapshot and Pro reports, and `mvp_report_qa.py` blocks output that fails its checks.
- **Analytics** (`scripts/analytics/`): `calibrate.py` fits a calibration map from the ledger, `research_memory.py` aggregates what has worked across instruments, `ledger_db.py` maintains a rebuildable SQLite mirror, and `sync_backtest.py` pushes sandbox results to admin-only tables.
- **Delivery** (`scripts/delivery/`, `scripts/sync-db.mjs`): exports the catalogue and track record, uploads report files to R2, and upserts editions, open calls and scored results into Neon.
- **Coordination** (`scripts/coordination/`): the VM accepts no inbound connections. It works with the website through Neon tables (`generation_requests`, `engine_runs`, `engine_state` and an allow-listed command queue). The daily run respects the admin "pause" flag, manual requests run regardless, and `.run.lock` stops two runs overlapping. `control_server.py` is an alternative path: it listens on localhost, is reached through a Cloudflare Tunnel, checks the Cloudflare Access token on every request except `/health`, and offers `/status`, `/events` (server-sent events), `POST /control` and `/jobs/<id>`.

The outcome ledger (`ledger/outcome_ledger.csv`) is append-only working state on the VM. It is git-ignored, and the publish chain mirrors it into the Neon `scored_results` table. Dated folders under `reports/` and `runs/` are pruned after the retention period, since published files live in R2.

## Project structure

```text
assetframe-scripts/
├── .claude/            Claude Code /mvp command and skill describing the report pipeline
├── .github/workflows/  CI: tests on every push and pull request, optional deployment
├── config/             assets.json (asset universe) and engine.example.json (runtime settings)
├── deploy/             systemd units and the Oracle Cloud VM runbook
├── logo/               Brand images used in reports
├── scripts/            The engine package
│   ├── analytics/      Ledger memory, calibration, SQLite mirror, backtest sync
│   ├── coordination/   Neon coordination layer and control server
│   ├── delivery/       Content export, R2 upload and purge
│   ├── pipeline/       Market data, authoring, research packs, scoring, rendering
│   ├── scheduler/      Calendars, config loading, daily run, poller
│   └── sync-db.mjs     Node script that upserts exported content into Neon
├── tests/              pytest suite
├── CLAUDE.md           Rules for AI-assisted report generation
├── package.json        Node dependency and the sync-db script
└── requirements.txt    Python dependencies
```

## Related repositories

- [assetframe-infra](https://github.com/JoeWat2005/assetframe-infra): the AssetFrame website (Next.js on Vercel). It owns the database schema and migrations, reads report files from R2 and data from Neon, and hosts the admin console that queues engine runs.

The two repositories share no code. They share an R2 bucket, using keys of the form `<date>/<slug>/<file>`, and a Neon database where the engine inserts rows and the website reads them. The confidence buckets and asset-class enums in `scripts/pipeline/scoring/taxonomy.py` are mirrored in the website and must be kept in sync.
