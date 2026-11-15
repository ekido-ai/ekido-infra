#!/usr/bin/env bash
# Puts secrets, data and the escrow ABI on a server built by bootstrap.sh, then starts the services.
# Run as root (for example through SSM Run Command):
#   bash restore.sh <bucket> [checkpoint-bucket]   # data from s3://<bucket>/migration/data.tgz
# With a checkpoint bucket, workers upload and download checkpoint blobs straight to it (/etc/ekido/storage.env).
#
# Inputs, all private:
#   s3://<bucket>/migration/contracts-out.tgz   the escrow build output (contracts/out) the deployed contract came from
#   s3://<bucket>/migration/data.tgz            ekido-data/jobs.db and ekido-data/artifacts/ (a consistent copy)
#   SSM SecureString parameters /ekido/server/{sepolia.env,sepolia-verifier-1.env,sepolia-verifier-2.env}, written
#   to /etc/ekido/{coordinator.env,verifier-1.env,verifier-2.env}, and /ekido/server/smoke-provider.key if present
# Secrets are written straight to their files with mode 600 and never printed.
# The smoke provider is not started here: it dials wss://gpuchain.duckdns.org, so it starts once DNS points here.
set -euo pipefail
BUCKET="${1:?usage: restore.sh <bucket> [checkpoint-bucket]}"
CHECKPOINT_BUCKET="${2:-}"
APP=/opt/ekido
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-south-1}"

echo "== escrow ABI"
aws s3 cp "s3://$BUCKET/migration/contracts-out.tgz" "$WORK/contracts-out.tgz" --only-show-errors
tar -xzf "$WORK/contracts-out.tgz" -C "$APP"
test -f "$APP/contracts/out/GPUChainEscrow.sol/GPUChainEscrow.json"

echo "== secrets"
install -d -m 0755 /etc/ekido
for pair in sepolia.env:coordinator.env sepolia-verifier-1.env:verifier-1.env sepolia-verifier-2.env:verifier-2.env; do
  param=${pair%%:*}; file=/etc/ekido/${pair#*:}
  # Create the file with mode 600 first, then fill it through a pipe. The snap-packaged AWS CLI writes nothing when
  # its output is redirected straight to a file, and still exits 0, which left empty secret files once.
  install -m 600 /dev/null "$file"
  aws ssm get-parameter --name "/ekido/server/$param" --with-decryption --query Parameter.Value --output text | cat > "$file"
  keys=$(grep -c '=' "$file" || true)
  [ "$keys" -gt 0 ] || { echo "$file came back empty; stopping"; exit 1; }
  echo "$file: $keys keys"
done
# The smoke provider's wallet, so it keeps its identity on the network. Without it, join makes a new wallet.
SMOKE=/var/lib/private/ekido-smoke-provider
if aws ssm get-parameter --name /ekido/server/smoke-provider.key --query Parameter.Name --output text >/dev/null 2>&1; then
  install -d -m 0700 /var/lib/private
  install -d -m 0755 "$SMOKE"
  install -m 600 /dev/null "$SMOKE/provider.key"
  aws ssm get-parameter --name /ekido/server/smoke-provider.key --with-decryption --query Parameter.Value --output text | tr -d '\n' | cat > "$SMOKE/provider.key"
  [ -s "$SMOKE/provider.key" ] || { echo "smoke provider key came back empty; stopping"; exit 1; }
  chown -R --reference="$SMOKE" "$SMOKE"
  echo "smoke provider key: restored"
fi
printf 'BACKUP_S3_BUCKET=%s\n' "$BUCKET" > /etc/ekido/backup.env
chmod 644 /etc/ekido/backup.env
# Not secret. Read by the coordinator and by retention; without it, blobs stay on the server's disk.
if [ -n "$CHECKPOINT_BUCKET" ]; then
  printf 'ARTIFACT_S3_BUCKET=%s\nARTIFACT_S3_REGION=%s\n' "$CHECKPOINT_BUCKET" "$AWS_DEFAULT_REGION" > /etc/ekido/storage.env
  chmod 644 /etc/ekido/storage.env
fi

echo "== data"
systemctl stop ekido-coordinator ekido-verifier@1 ekido-verifier@2 2>/dev/null || true
aws s3 cp "s3://$BUCKET/migration/data.tgz" "$WORK/data.tgz" --only-show-errors
tar -xzf "$WORK/data.tgz" -C "$WORK"
python3 - "$WORK/ekido-data/jobs.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
print("jobs:", c.execute("select count(*) from jobs").fetchone()[0])
PY
# DynamicUser units keep their state in /var/lib/private/<name>; systemd fixes ownership when the unit starts.
STATE=/var/lib/private/ekido-coordinator
install -d -m 0700 /var/lib/private
install -d -m 0755 "$STATE"
if [ -e "$STATE/jobs.db" ]; then
  mv "$STATE/jobs.db" "$STATE/jobs.db.before-restore-$(date -u +%Y%m%d-%H%M%S)"
fi
cp "$WORK/ekido-data/jobs.db" "$STATE/jobs.db"
mkdir -p "$STATE/artifacts"
cp -r --update=none "$WORK/ekido-data/artifacts/." "$STATE/artifacts/"
# On a first restore the folder is still root's and systemd hands everything to the service user when the unit
# starts. On a later restore the folder already belongs to that user and systemd leaves it, so files copied in as
# root stay read-only for the service ("attempt to write a readonly database" on 15 Nov). Match the folder's owner.
chown -R --reference="$STATE" "$STATE"
echo "artifacts: $(find "$STATE/artifacts" -type f | wc -l) files"

echo "== start"
systemctl start ekido-coordinator
for _ in $(seq 1 90); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)" = 200 ] && break; sleep 2; done
echo "coordinator readyz: $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8003/readyz)"
systemctl start ekido-verifier@1 ekido-verifier@2 ekido-web
sleep 8
systemctl is-active ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web caddy | tr '\n' ' '; echo
echo "web: $(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:3101/)"
systemctl start ekido-backup.timer ekido-monitor.timer
echo "Restore done. Next: point DNS here, then start ekido-smoke-provider and ekido-autodeploy.timer."
