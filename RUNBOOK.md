# Runbook: running the hosted test network

How the hosted Base Sepolia network is run, checked, backed up and restored. Everything here is a test network with test USDC.

## What runs where

One AWS EC2 instance (`ekido-server`, `m7i-flex.large`, Mumbai `ap-south-1`, Elastic IP `13.203.167.11`) runs these systemd units, all enabled at boot. Both `ekido.duckdns.org` (the site) and `gpuchain.duckdns.org` (the API and provider endpoint) point at it. It moved here from Google Cloud on 15 Nov 2026; `infra/server/bootstrap.sh` and `infra/server/restore.sh` rebuild it from scratch.

| Unit | What it is |
|---|---|
| `caddy` | HTTPS reverse proxy (Let's Encrypt), security headers |
| `ekido-coordinator` | The coordinator for Base Sepolia, on port 8003 behind `/sepolia` |
| `ekido-verifier@1`, `@2` | Two verifiers, each with its own key and system user |
| `ekido-web` | The web app for the hosted network |
| `ekido-smoke-provider` | A small CPU provider so the network always has one online |
| `ekido-monitor.timer` | Health check every 5 minutes |
| `ekido-backup.timer` | Backup every day at 03:00 UTC |
| `ekido-retention.timer` | Checkpoint retention every day at 03:30 UTC (see below) |
| `ekido-autodeploy.timer` | Deploys `main` every 5 minutes when its CI is green |

Code is in `/opt/ekido` (the dashboard in `/opt/ekido/web`), ops scripts in `/opt/ekido-ops`, and state in `/var/lib/ekido-*`. Keys and settings live in root-only files under `/etc/ekido/`, and their source of truth is AWS SSM Parameter Store (`/ekido/server/*`, SecureString). They are never printed, logged or committed.

There is no SSH: the security group opens only ports 80 and 443. Run commands through SSM, for example `aws ssm send-command --instance-ids <id> --document-name AWS-RunShellScript --parameters 'commands=["systemctl is-active ekido-coordinator"]'`, or open a shell with `aws ssm start-session --target <id>`.

## Is it healthy?

```bash
curl -s https://gpuchain.duckdns.org/sepolia/readyz      # "status": "ready", with one entry per check
curl -s https://gpuchain.duckdns.org/sepolia/providers   # the smoke provider should be ONLINE
```

`/readyz` checks the database, artifact storage and free space, the chain RPC's latest block age, the coordinator's gas balance and the settlement backlog. It returns 503 when any check fails.

On the server:

```bash
systemctl is-active caddy ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider
journalctl -t ekido-monitor -p err --since today       # problems the monitor found
```

## Alerts

The **Uptime** workflow in GitHub Actions (`.github/workflows/uptime.yml`) checks `/sepolia/readyz` (must say `"status": "ready"`) and the site's pages every 15 minutes. When it fails, GitHub emails the repository's admins. On the server, the monitor timer logs problems to the journal every 5 minutes.

## Backups

Every day at 03:00 UTC, `/opt/ekido-ops/ekido-backup.sh` (source: `infra/server/ops/ekido-backup.sh`):

1. Takes a consistent SQLite snapshot with the online backup API, runs `PRAGMA integrity_check`, and gzips it under `/var/backups/ekido/db/`.
2. Copies any new checkpoint artifacts to `/var/backups/ekido/artifacts/` (they are content-addressed and never change).
3. If `/etc/ekido/backup.env` sets `BACKUP_S3_BUCKET`, copies the snapshot and new artifacts to that private S3 bucket (`ekido-backups-…`, encrypted, all public access blocked).

The server's IAM role can add and read objects in the bucket but cannot delete them. The bucket deletes database snapshots after 90 days and artifact copies after 180 days.

Run a backup now:

```bash
sudo systemctl start ekido-backup.service
journalctl -u ekido-backup.service -n 10 --no-pager
```

## Checkpoint storage: retention and limits

Checkpoints are what let a job resume on another device, so they are never removed while a job could still need them. `ekido-retention.timer` runs `ekido_coordinator.retention` every day at 03:30 UTC:

- A job in progress keeps every checkpoint newer than its newest accepted one, its last two accepted ones, and every rejected one (as evidence).
- A job that has ended keeps everything for 7 days, then only its final model, until 90 days after it ended.
- A blob no checkpoint points at is kept for 24 hours, because uploads land before their manifest commits.
- Anything else moves to `/var/lib/ekido-coordinator/artifacts-quarantine/<date>/`. With `--apply`, quarantine folders older than 30 days are purged. Manifests and database rows are never removed.

It ships as a **dry run** and only logs what it would move (`journalctl -u ekido-retention -n 5`). To turn it on, add `--apply` with `systemctl edit ekido-retention.service`. To put a quarantined blob back, use `ArtifactStore.restore(<sha256>)`, or move the file back under `artifacts/<first two hex>/`.

Limits, set in `/etc/ekido/coordinator.env`:
- `MAX_JOB_STORAGE_BYTES` (default 5 GiB) caps the checkpoint bytes one job keeps. A commit over it is refused with `STORAGE_QUOTA`.
- `MAX_PENDING_UPLOAD_BYTES` (default 2 GiB) caps what one provider may upload before a checkpoint commit references it. Over it, the upload gets HTTP 413.

### Direct uploads to S3

With `/etc/ekido/storage.env` (`ARTIFACT_S3_BUCKET`, `ARTIFACT_S3_REGION`; written by `restore.sh <backup-bucket> <checkpoint-bucket>`), workers move checkpoint blobs straight to and from the private bucket `ekido-checkpoints-<account>`, under `blobs/<sha256>`:

- A worker asks `POST /artifacts/<sha256>/upload-url` (with its token and the size) and gets a 15-minute presigned PUT. The link is signed with the blob's SHA-256, and S3 refuses any other content (`BadDigest`). The pending-upload cap is counted when the link is issued.
- `GET /artifacts/<sha256>/download-url` gives a presigned GET. Downloads are verified against the hash on the worker either way.
- Older provider kits, and blobs already on the server's disk, keep using `PUT`/`GET /artifacts/<sha256>`. The coordinator streams a bucket blob to an older worker that asks for it.
- Retention applies the same rules to the bucket. Quarantine there is `quarantine/<date>/`, which a bucket lifecycle rule empties after 30 days. The server's role (`ekido-checkpoints` inline policy) can delete only under `blobs/`, never under `quarantine/`.

Without `storage.env`, everything stays on the server's disk as before.

## Restoring

1. Stop the coordinator: `sudo systemctl stop ekido-coordinator`.
2. Move the current database aside (don't delete it).
3. Fetch a snapshot: `aws s3 cp s3://<bucket>/db/jobs-<stamp>.db.gz - | cat > jobs-<stamp>.db.gz` and `gunzip` it. (The snap-packaged AWS CLI writes nothing when its stdout is redirected straight to a file; pipe it.)
4. Check it: `python3 -c "import sqlite3; print(sqlite3.connect('jobs-<stamp>.db').execute('PRAGMA integrity_check').fetchone())"`.
5. Put it at `/var/lib/ekido-coordinator/jobs.db` and give it the folder's owner (`chown --reference=/var/lib/private/ekido-coordinator ...`), then copy artifacts back with `aws s3 sync s3://<bucket>/artifacts <state>/artifacts`. To rebuild a whole server instead, run `infra/server/bootstrap.sh` then `infra/server/restore.sh`.
6. Start the coordinator and check `/readyz`.

Money is never in the database: balances live in the escrow contract, so a restore can't lose or create funds.

### Postgres

A server set up with `infra/server/postgres.sh` keeps the coordinator's data in Postgres 16 on the same box (localhost only). `DATABASE_URL` lives in `/etc/ekido/database.env` (mode 600). Alembic migrations in `apps/coordinator/alembic` own the schema, and the coordinator unit applies them before every start. A schema change is a new migration (`python -m alembic -c alembic/alembic.ini revision --autogenerate -m "..."`), and CI's `Postgres` job checks that the migrations match the models.

The nightly backup takes `pg_dump --format=custom` to `/var/backups/ekido/db/jobs-<stamp>.pgdump`, checks it with `pg_restore --list`, and copies it to S3 when a bucket is set.

Restore drill (safe on a running server, because it restores into a scratch database):
1. As postgres: `createdb ekido_drill`, then `pg_restore --no-owner -d ekido_drill /var/backups/ekido/db/jobs-<stamp>.pgdump`.
2. Compare row counts of `jobs`, `checkpoints` and `orgs` between `ekido` and `ekido_drill`.
3. `dropdb ekido_drill`.

For a real restore, stop the coordinator, restore into `ekido` the same way (with `--clean`), and start it again.

## Deploying: automatic, from main

Merging into `main` deploys itself. Every 5 minutes `ekido-autodeploy.timer` runs `/opt/ekido-ops/ekido-autodeploy.sh` (source: `infra/server/ops/`), which:

1. Reads the newest commit on `main` and its CI check runs, anonymously (the repo is public; GitHub holds no credential for this server).
2. Waits while CI runs, and refuses a commit whose CI failed.
3. Compares the new source with what is running, file by file, and deploys only what changed:
   - **Python services:** waits while any job is unfinished, backs up the running code, swaps it, restarts, and puts the old code back if `/readyz` isn't ready within a minute.
   - **Web app:** `ekido-web-deploy.sh` rebuilds it, keeping the previous build and restoring it if a build fails.
4. Installs web dependency changes with `npm ci` (CI has already built with the new lockfile), keeping the old `node_modules` to put back if anything fails. Python dependency changes (`pyproject.toml`) are refused; deploy those by hand.
5. Publishes the running commit at `https://gpuchain.duckdns.org/deploy/version.json` (plus `failed_sha` and the reason when a deploy is refused).

The **Deploy** workflow in GitHub Actions waits for that file to show the merged commit, then checks `/readyz` and the main pages; it fails, and GitHub emails you, if the server refused the deploy.

```bash
sudo DRY_RUN=1 bash /opt/ekido-ops/ekido-autodeploy.sh   # what would happen, changing nothing
journalctl -u ekido-autodeploy.service -n 30 --no-pager     # the last runs
sudo rm /var/lib/ekido-autodeploy/failed                    # retry a refused commit after fixing the cause
```

Installed by `infra/server/bootstrap.sh`, which also records the commit it built from as the one already running.

## Restarting the server

A full restart takes about 2 minutes; every service above starts on its own. The Elastic IP stays attached, so DNS keeps working.

## Topping up gas

When `/readyz` reports low coordinator gas, send a little Base Sepolia test ETH to the coordinator's address (shown on the owner console). Settlements wait, and nothing is lost, until it's topped up.

## What is still missing

- A rehearsed restore of a backup snapshot onto a fresh instance. (The 15 Nov move used the same scripts with a live copy.)
- Postgres with point-in-time recovery, job leases and a second coordinator, so one server is no longer a single point of failure.
- Automatic gas top-up.

## Inference gateway configuration

The model gateway is disabled until both the Platform authority and inference
router have matching operator-managed secrets and model manifests. Provision
root-owned `/etc/ekido/inference-platform.env` and `/etc/ekido/inference.env`
with restrictive permissions; never put real secrets in the repository.
Platform needs `INFERENCE_MODELS_ROOT` and `INFERENCE_INTERNAL_TOKEN`. The
router needs `EKIDO_INFERENCE_AUTHORITY_URL=http://127.0.0.1:8010` and
`EKIDO_INFERENCE_INTERNAL_TOKEN` set to the same secret. Follow the Platform
inference-authority documentation to provision job-owned model manifests and
local engines. Use Postgres for the per-key lock and usage records.

Deploy the matching Platform and inference changes together before directing
customer traffic to the new Caddy routes. Check `/readyz` on the private
inference listener, key authorization, usage finalization and a real model
request on staging; an unconfigured service intentionally returns unavailable.
Pending usage reservations need reconciliation after router failures.
