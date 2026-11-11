# Runbook: running the hosted test network

How the hosted Base Sepolia network is run, checked, backed up and restored. Everything here is a test network with test USDC.

## What runs where

One Google Cloud VM (`gpuchain-vm`, zone `asia-south1-a`, reserved static IP) runs these systemd units, all enabled at boot:

| Unit | What it is |
|---|---|
| `caddy` | HTTPS reverse proxy (Let's Encrypt), security headers |
| `gpuchain-sepolia` | The coordinator for Base Sepolia, on port 8003 behind `/sepolia` |
| `gpuchain-sepolia-verifier@1`, `@2` | Two verifiers, each with its own key and system user |
| `gpuchain-sepolia-web` | The web app for the hosted network |
| `gpuchain-sepolia-smoke-provider` | A small CPU provider so the network always has one online |
| `gpuchain-monitor.timer` | Health check every 5 minutes |
| `gpuchain-backup.timer` | Backup every day at 03:00 UTC |

Keys and settings live in root-only files under `/etc/gpuchain/`. They are never printed, logged or committed.

## Is it healthy?

```bash
curl -s https://gpuchain.duckdns.org/sepolia/readyz      # "status": "ready", with one entry per check
curl -s https://gpuchain.duckdns.org/sepolia/providers   # the smoke provider should be ONLINE
```

`/readyz` checks the database, artifact storage and free space, the chain RPC's latest block age, the coordinator's gas balance and the settlement backlog. It returns 503 when any check fails.

On the VM:

```bash
systemctl is-active caddy gpuchain-sepolia gpuchain-sepolia-verifier@1 gpuchain-sepolia-verifier@2 gpuchain-sepolia-web gpuchain-sepolia-smoke-provider
journalctl -t gpuchain-monitor -p err --since today       # problems the monitor found
```

## Alerts

Cloud Monitoring runs two uptime checks every 5 minutes from several regions: `/sepolia/readyz` (must contain `"status":"ready"`) and the website. If a check fails from more than one region for 5 minutes, an email goes to the maintainer. Each alert's text says what to look at first.

## Backups

Every day at 03:00 UTC, `/opt/gpuchain-ops/gpuchain-backup.sh` (source: `infra/gcp/ops/gpuchain-backup.sh`):

1. Takes a consistent SQLite snapshot with the online backup API, runs `PRAGMA integrity_check`, and gzips it under `/var/backups/gpuchain/db/`.
2. Copies any new checkpoint artifacts to `/var/backups/gpuchain/artifacts/` (they are content-addressed and never change).
3. If `/etc/gpuchain/backup.env` sets `BACKUP_BUCKET`, copies the snapshot and new artifacts to that private Cloud Storage bucket.

The VM's account can add and read objects in the bucket but cannot delete them. The bucket deletes database snapshots after 90 days; artifacts are kept.

Run a backup now:

```bash
sudo systemctl start gpuchain-backup.service
journalctl -u gpuchain-backup.service -n 10 --no-pager
```

## Restoring

1. Stop the coordinator: `sudo systemctl stop gpuchain-sepolia`.
2. Move the current database aside (don't delete it).
3. Fetch a snapshot: `gcloud storage cp gs://<bucket>/db/jobs-<stamp>.db.gz .` and `gunzip` it.
4. Check it: `python3 -c "import sqlite3; print(sqlite3.connect('jobs-<stamp>.db').execute('PRAGMA integrity_check').fetchone())"`.
5. Put it at `/var/lib/gpuchain-sepolia/jobs.db` with the service's ownership, and sync artifacts back with `gcloud storage rsync`.
6. Start the coordinator and check `/readyz`.

Money is never in the database: balances live in the escrow contract, so a restore can't lose or create funds.

## Deploying the web app

The deploy script rebuilds both web apps from a source archive, keeps the previous build outside the app, restarts only after a successful build, and rolls back if the build fails. It refuses to touch anything when a build fails, so the running site stays up.

## Restarting the VM

A full restart takes about 2 minutes; every service above starts on its own. Changing the VM's access scopes needs a stop and start. The external IP is reserved, so DNS keeps working.

## Topping up gas

When `/readyz` reports low coordinator gas, send a little Base Sepolia test ETH to the coordinator's address (shown on the owner console). Settlements wait, and nothing is lost, until it's topped up.

## What is still missing

- A rehearsed restore from the bucket onto a fresh VM.
- Postgres with point-in-time recovery, job leases and a second coordinator, so one VM is no longer a single point of failure.
- Automatic gas top-up.
