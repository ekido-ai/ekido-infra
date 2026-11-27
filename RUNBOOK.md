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

## Sign-in email

Platform emails sign-in links (and team invites) through the team Gmail, `teamekido@gmail.com`, over SMTP with a Google app password. Both servers set this in `/etc/ekido/platform.env` (mode 600):

```
EMAIL_SENDER=smtp
EMAIL_FROM="Ekido <teamekido@gmail.com>"
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=teamekido@gmail.com
SMTP_PASSWORD=<app password, written by the server from Parameter Store>
```

- **The app password** lives only in Parameter Store as the SecureString `/ekido/server/smtp-password`. Each server reads it there and writes it into its own env file; it never goes through a laptop, a command line or chat.
  - To rotate it: make a new app password on the team Google account, update the parameter in the console, then run the same server-side step again and restart `ekido-platform`.
  - Live reads it with its `/ekido/*` permission. Staging has the inline policy `ekido-read-smtp-password`, which covers only this parameter.
- **A failed send** answers `503 EMAIL_NOT_SENT` with a plain message, and logs only the error type, never the link or password.
- **SES is the fallback** (`EMAIL_SENDER=ses`, `EMAIL_REGION=ap-south-1`).
  - `teamekido@gmail.com` is verified there, and production access was requested on 20 Nov. Until AWS grants it, SES delivers only to verified addresses.
  - The roles carry the inline policy `ekido-send-email` (`ses:SendEmail` and `ses:SendRawEmail` only).
- **`EMAIL_SENDER=log`** writes the link to the Platform journal instead of sending it. Use it on a test box only, never on live.
- **The From address is a team mailbox,** never a person's own account.
- **Gmail limits:** about 500 messages a day. Move to SES (or a domain sender) before real customer volume.

## Python packages on the servers

The autodeploy updates code, but it doesn't install Python packages. When a service adds a dependency, install it in `/opt/ekido/venv` on both servers **before** merging. Otherwise the service restarts into an `ImportError`.

```
sudo /opt/ekido/venv/bin/pip install '<package spec from pyproject.toml>'
```

On 20 Nov, `fpdf2>=2.8,<3` (Platform's invoice PDFs) was installed this way. `pyrage` (core v0.4.3's encryption) is still needed before `deploy.json` moves to core v0.4.3.

## Job keys (core v0.4.3, ticket A12)

Each job gets its own age key. The coordinator keeps the secret half only **wrapped**, by KMS, and it refuses to start unless it knows how to wrap: there is no default and no throwaway key, because a wrapper that disappears on a restart would make every job's encrypted data unreadable, with no error until someone tried to read it. Before merging the coordinator change that adds this (ekido-coordinator #13), on **each** server:

1. `sudo /opt/ekido/venv/bin/pip install 'pyrage==1.4.0'` (the autodeploy does not install packages; without it the coordinator restarts into an `ImportError`).
2. Put `JOB_KEY_WRAP=kms` in `/etc/ekido/coordinator.env` (`KMS_KEY_ID` defaults to `alias/ekido-data` and `KMS_REGION` to `ap-south-1`). `staging.sh` adds the line to staging; on live add it by hand. Never leave it empty: `local` is only for a laptop or a test and needs `LOCAL_WRAP_IDENTITY` (an age secret key) as well.
3. The server's role must be allowed `kms:Encrypt`, `kms:Decrypt`, `kms:GenerateDataKey` and `kms:DescribeKey` on the key. `aws/kms-hardening.sh` grants that to the role `ekido-server`; check that the staging instance uses a role that has it too (`aws iam get-instance-profile`), or the first job key fails.
4. Restart and check: `sudo systemctl restart ekido-coordinator`, then `/readyz` must answer 200. A missing or bad setting shows up at once in `journalctl -u ekido-coordinator -n 20` as `JOB_KEY_WRAP must be set to 'kms' ...` (the message never prints a key).

What this does not give you yet: deleting a job's key does not make **older database backups** unreadable, because every job key is wrapped by the same KMS key. That needs a key per customer (an open decision); until then say "removed from our live systems", not "unrecoverable".

## Cache (Valkey)

The Platform can use Valkey on the same server as a cache (ekido-internal #142): who an API key or session belongs to, the assistant's rate limits, and webhooks already handled. Postgres stays the source of truth. If Valkey is stopped, full or broken, the Platform keeps working from the database.

To turn it on, after the Platform code that reads `VALKEY_URL` is deployed:

1. `sudo /opt/ekido/venv/bin/pip install 'redis>=5,<7'` (first: with `VALKEY_URL` set and no client library, the Platform does not start).
2. `sudo bash /opt/ekido/server/valkey.sh` (localhost only, password, 256 MB, least recently used dropped first, nothing on disk).
3. `sudo systemctl daemon-reload && sudo systemctl restart ekido-platform`, then `/readyz` must answer 200.

To turn it off: move `/etc/ekido/valkey.env` aside and restart the Platform.

A revoked key, a sign-out and "delete my data" take effect at once. The one exception: if Valkey cannot be reached at the moment of the revoke, a remembered entry can still work for up to 30 seconds after it comes back.

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
customer traffic to the new Caddy routes. Install the updated systemd units,
run `systemctl daemon-reload`, validate the updated Caddyfile, and reload Caddy
as part of the coordinated rollout. Check `/readyz` on the private
inference listener, key authorization, usage finalization and a real model
request on staging; an unconfigured service intentionally returns unavailable.
Pending usage reservations need reconciliation after router failures.

## Assistant engines (llama.cpp)

The assistant runs two local models, each in its own systemd unit, on loopback only:

| Unit | Model | Port | Memory cap |
|---|---|---|---|
| `ekido-assistant-guard` | Qwen3Guard-Gen-0.6B, the safety screen in front of every message | 8031 | 1 GB |
| `ekido-assistant` | the bake-off winner, which picks the recipe | 8032 | 3.2 GB |

`server/assistant/assistant-engine.sh` builds `llama-server` from source at the pinned tag in `server/assistant/models.lock.json`. It downloads each model from its pinned Hugging Face commit, checks every weights file's sha256, and converts with llama.cpp's own converter when there's no official GGUF. It never deletes anything; a file that fails its check is kept as `*.bad`.

```
sudo bash assistant-engine.sh build                       # once per llama.cpp tag (about 15 minutes on 2 vCPU)
sudo bash assistant-engine.sh fetch guard qwen2.5-1.5b    # any names from models.lock.json
sudo bash assistant-engine.sh install                     # units; the guard starts once its model is there
sudo bash assistant-engine.sh use qwen2.5-1.5b            # switch the assistant model (bake-off, then the winner)
sudo bash assistant-engine.sh status
```

Platform reaches them through `ASSISTANT_GUARD_URL=http://127.0.0.1:8031` and `ASSISTANT_ENGINE_URL=http://127.0.0.1:8032` in `/etc/ekido/platform.env`. Without them, the assistant answers 503 (fail closed).

**Disk:** on 24 Nov staging's root volume was grown from 30 to 49 GB, online, to hold the build and the bake-off candidates.

## Staging and live: what each one runs, and how live moves

Both servers run the same auto-deploy, but they follow different files (decision of 24 Nov 2026). Before this, both followed `deploy.json`, so every merge reached live within about 5 minutes.

| | Follows | Moves when |
|---|---|---|
| **Staging** | `deploy.json`: each repo's `main` (core at its tag) | every green merge, within about 5 minutes |
| **Live** | `deploy.live.json`: an exact commit for every repo | a promotion pull request is merged. Piyush's approval on it is the "haan" |

Each server says which it is in `/etc/ekido/environment` (`staging` or `live`; a missing file means staging; any other word is refused, never guessed). `https://<host>/deploy/version.json` now shows `"environment"`.

**To promote (put what staging runs onto live):**
1. Check staging: the pages work, the PR evidence is in, the nightly privacy test is green.
2. In `ekido-infra`: `python tools/promote.py`. It rewrites `deploy.live.json` from staging's `version.json` and prints a compare link for every repo that moves.
3. Open a pull request with that file. Paste the printed list into it. Piyush approves it (CODEOWNERS), and merging it is the "haan".
4. Within about 5 minutes live deploys exactly those commits, with the same checks as staging. `python tools/promote.py --check` shows whether live matches staging.

**To roll back:** open a pull request that puts the previous commits back into `deploy.live.json` (`git revert` of the promotion).

If `version.json` says `failed_sha`, the server refused that release: read `journalctl -u ekido-autodeploy` there, as under "Deploying".

## Publishing a finished model (serving)

`server/inference/publish_model.py` starts one llama.cpp engine per model on a loopback port (`ekido-engine@<id>`) and writes the manifest
the Platform reads. A model is listed only after its engine answers. It refuses a GGUF whose SHA-256 differs from the one you give.

```
sudo python3 server/inference/publish_model.py publish --model-id <id> --job-id <job> --gguf <file> --sha256 <hex> --mode test
sudo python3 server/inference/publish_model.py unpublish --model-id <id>    # hides it; deletes nothing
sudo python3 server/inference/publish_model.py list
```

Once per server, before the first publish (not done yet on staging or live):
1. Install the unit: copy `server/systemd/ekido-engine@.service` to `/etc/systemd/system/`, then `systemctl daemon-reload`.
2. `llama-server` must be built (`assistant-engine.sh build`) and the user `ekido-llm` must exist.
3. Platform: `INFERENCE_MODELS_ROOT=/var/lib/ekido-inference-models` and an `INFERENCE_INTERNAL_TOKEN` of 32 or more characters.
4. Inference: `/etc/ekido/inference.env` with `EKIDO_INFERENCE_AUTHORITY_URL=http://127.0.0.1:8010` and the same token as
   `EKIDO_INFERENCE_INTERNAL_TOKEN`.

Memory: each engine may use up to 3 GB, so a server with 8 GB holds one or two besides everything else.
