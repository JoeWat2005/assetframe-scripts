# AssetFrame OCI engine runner — bring-up runbook

This directory holds the VM-side counterpart to the admin **Engine console** in the web
app. The Oracle Cloud (OCI) VM runs the AssetFrame engine (`scripts/run_daily.py`) on a
schedule **and** on demand, and reports status back to the web app.

## How it talks to the web app (no inbound ports)

The VM has **NO inbound ports** — it never accepts a connection. It coordinates with the
web app **only through three Neon tables**, all over **outbound** Postgres/HTTPS:

| Table | VM does | Web app does |
|---|---|---|
| `generation_requests` | polls + claims + writes status/run_id/error | enqueues a manual scoped run, can set `cancel_requested` |
| `engine_runs` | inserts one row per run; updates status/results/log | reads for live run status + history |
| `engine_state` (singleton id=1) | heartbeats `last_heartbeat_at`, sets `current_run_id` | reads heartbeat ("online?"), sets `automation_paused` |

Two processes do the work, both via `scripts/engine_ops.py` (the shared DB + run layer):

- **`scripts/poller.py`** — long-lived systemd **service**. Every 30s: heartbeat → claim
  the oldest queued `generation_requests` row → run it (`trigger='manual'`). **Manual
  requests run even when `automation_paused` is true** — enqueuing is an explicit admin
  action.
- **`scripts/scheduled_run.py`** — systemd **oneshot**, fired by a **timer at 05:00 UTC**.
  Heartbeat → if `automation_paused`, log + record a skip + exit 0; else run the full due
  batch (`trigger='schedule'`, scope `{all_due:true}` → `run_daily.py --mode production`).

`run_daily.py` is serialised behind a file lock (`<repo>/.run.lock`) so the timer and the
poller never run it concurrently. Cancellation is co-operative: the web app sets
`generation_requests.cancel_requested`; an in-flight run polls it and terminates.

---

## 1. Provision an Always-Free ARM VM

1. In the OCI console → **Compute → Instances → Create instance**.
2. Shape: **VM.Standard.A1.Flex** (Ampere ARM, Always-Free eligible). 2 OCPU / 12 GB is
   plenty; 1 OCPU / 6 GB works.
3. Image: **Canonical Ubuntu 24.04** (ships Python 3.12).
4. Add your **SSH public key**.
5. Networking: a public subnet is fine. **Do NOT open any inbound ports** beyond the
   default SSH (22) for your own admin access. The engine needs **outbound only**
   (443 to Neon, R2, and the market-data provider). You may even close inbound 22 after
   setup and use the OCI serial console / Cloud Shell.
6. Create, then SSH in: `ssh ubuntu@<public-ip>`.

> Always-Free ARM capacity can be scarce by region. If "out of capacity", retry or pick a
> different availability domain/region.

## 2. Install the OS prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip git tzdata curl

# Node 20 (only for scripts/sync-db.mjs, the Neon publish helper)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Pin the VM clock to UTC so the 05:00 UTC timer is unambiguous.
sudo timedatectl set-timezone UTC
```

Verify: `python3.12 --version` (3.12.x), `node --version` (v20.x), `git --version`,
`timedatectl` (should show `UTC`).

## 3. Clone the engine repo

We deploy to `/opt/assetframe-scripts` (the path baked into the systemd units; change all
three units if you use a different path). Run the engine as the `ubuntu` user.

```bash
sudo mkdir -p /opt/assetframe-scripts
sudo chown ubuntu:ubuntu /opt/assetframe-scripts
git clone https://github.com/JoeWat2005/assetframe-scripts.git /opt/assetframe-scripts
cd /opt/assetframe-scripts
```

(If you cannot use HTTPS, add the deploy key from step 6 first, then clone over SSH:
`git@github.com:JoeWat2005/assetframe-scripts.git`.)

## 4. Create the venv + install deps

```bash
cd /opt/assetframe-scripts
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt    # fpdf2, pymupdf, anthropic, psycopg[binary]
npm install                                   # @neondatabase/serverless for sync-db.mjs
```

`requirements.txt` now includes **`psycopg[binary]`** (psycopg3) — the poller's Postgres
client. (The web app's `sync-db.mjs` uses the Node Neon driver; the Python engine had no
PG client before this runner.)

## 5. Create the `.env` (root-only perms 600)

The engine reads `DATABASE_URL` (and the rest) from the environment, falling back to
`<repo>/.env`. **systemd loads this same file via `EnvironmentFile=`.** Never commit it.

```bash
cd /opt/assetframe-scripts
umask 077
cat > .env <<'EOF'
# Cloudflare R2 (publish.py uploads Pro files here)
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=assetframe-pro

# Neon Postgres — the PROD url (the engine writes, the web app reads).
DATABASE_URL=postgresql://...neon.tech/neondb?sslmode=require&channel_binding=require

# Market data — yahoo (keyless default) or eodhd (+ key). Futures =F always use Yahoo.
ADVISOR_DATA_PROVIDER=yahoo
# EODHD_API_KEY=...

# Anthropic — required only when ASSETFRAME_AUTHOR_BRIEFS is on (autonomous brief authoring).
ANTHROPIC_API_KEY=...
EOF
chmod 600 .env
```

> `EnvironmentFile` parses simple `KEY=VALUE` lines (no shell expansion). Keep the
> `DATABASE_URL` on one line and unquoted, exactly as above.

Sanity check the DB wiring before installing services:

```bash
.venv/bin/python -c "import scripts.engine_ops as e; print('DATABASE_URL ok:', bool(e.database_url()))"
# Then a single real tick (heartbeats + claims one request if queued, else no-op):
.venv/bin/python scripts/poller.py --once
```

If `DATABASE_URL` is missing you get a clear `ConfigError`, not a stack trace.

## 6. Add a GitHub deploy key (read/write) so the VM can commit the ledger

The engine appends scored outcomes to `ledger/outcome_ledger.csv` and must push them back.
Give this VM its **own** deploy key with **write** access.

```bash
ssh-keygen -t ed25519 -C "assetframe-oci-runner" -f ~/.ssh/assetframe_deploy -N ""
cat ~/.ssh/assetframe_deploy.pub
```

- GitHub → repo **assetframe-scripts** → **Settings → Deploy keys → Add deploy key** →
  paste the public key → **Allow write access** → Add.
- Point git at the key and use the SSH remote:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-assetframe
  HostName github.com
  User git
  IdentityFile ~/.ssh/assetframe_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
cd /opt/assetframe-scripts
git remote set-url origin git@github-assetframe:JoeWat2005/assetframe-scripts.git
git config user.name  "AssetFrame OCI runner"
git config user.email "engine@assetframe.local"
ssh -T git@github-assetframe   # expect the GitHub "successfully authenticated" banner
```

> The runner scripts here do not auto-commit; wire the ledger push into your publish/commit
> step. This key is what makes that push possible from a VM with no interactive login.

## 7. Install the systemd units

```bash
cd /opt/assetframe-scripts
sudo cp deploy/assetframe-poller.service /etc/systemd/system/
sudo cp deploy/assetframe-daily.service  /etc/systemd/system/
sudo cp deploy/assetframe-daily.timer    /etc/systemd/system/
```

If you deployed somewhere other than `/opt/assetframe-scripts` **or** run as a user other
than `ubuntu`, edit the three units first (the `WorkingDirectory`, `EnvironmentFile`, and
`ExecStart` paths; add `User=`/`Group=` if not running as root-installed `ubuntu`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now assetframe-poller.service assetframe-daily.timer
```

(The `.service` for the daily run is *not* enabled directly — the **timer** drives it.)

## 8. Verify

```bash
# The poller should be active and heartbeating every 30s.
systemctl status assetframe-poller.service
journalctl -u assetframe-poller -f          # watch live ticks

# The timer should be scheduled with a next-run at the upcoming 05:00 UTC.
systemctl list-timers assetframe-daily.timer

# Optional: fire a scheduled run right now to prove the path end-to-end.
sudo systemctl start assetframe-daily.service
journalctl -u assetframe-daily -e
```

In the web app's admin **Engine console**, the VM should flip to **"online"** within ~30s
(that is the first heartbeat landing in `engine_state.last_heartbeat_at`). Enqueue a manual
scoped run from the console and watch the poller claim it (`journalctl -u assetframe-poller
-f`) and a row appear in `engine_runs`.

## Operating notes

- **Pause/resume automation** from the web app — it sets `engine_state.automation_paused`.
  The 05:00 timer respects it (logs "automation paused, skipping" and records a skip row).
  **Manual console requests still run while paused** — that is intentional.
- **Cancel a run** from the console — it sets `cancel_requested`; an in-flight run polls it
  (~every 5s) and terminates → status `cancelled`.
- **Logs:** `journalctl -u assetframe-poller -f` (live) and `-u assetframe-daily -e` (last
  scheduled run). Run artifacts: `<repo>/runs/<date>/run_manifest.json`.
- **No inbound ports** are ever required. If the console shows "offline", check outbound
  network and `journalctl -u assetframe-poller` — the loop logs DB errors and keeps going,
  so a persistent offline state usually means a bad `DATABASE_URL` or no network.
- **Concurrency:** the timer and poller share `<repo>/.run.lock`; if a run is already in
  progress, the other path records `failed: another run is already in progress` rather than
  double-running the engine.
- **Update the engine:** `cd /opt/assetframe-scripts && git pull && .venv/bin/pip install
  -r requirements.txt && sudo systemctl restart assetframe-poller.service` (the timer picks
  up the new code on its next fire automatically).
