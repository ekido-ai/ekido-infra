#!/usr/bin/env bash
# Builds the Ekido Base Sepolia server on a fresh Ubuntu 24.04 instance from one commit of the public repo.
# Run as root, for example through SSM Run Command (no SSH):
#   bash bootstrap.sh <commit-sha>
#
# Layout: the coordinator, two verifiers and the smoke provider run from /opt/ekido, the dashboard from /opt/ekido/web,
# Caddy in front, and the auto-deploy, backup, monitor and retention timers from /opt/ekido-ops. The units are
# infra/server/systemd/ekido-*.
#
# Not done here, on purpose:
#   - secrets: /etc/ekido/coordinator.env, verifier-{1,2}.env and backup.env are placed by restore.sh;
#   - data: /var/lib/ekido-coordinator (jobs.db, artifacts) is restored separately;
#   - the escrow ABI under /opt/ekido/contracts/out is copied from the build that deployed the contract;
#   - starting services: done once the secrets and data are in place.
# Safe to run again: it installs what's missing and refreshes code, units and scripts.
set -euo pipefail
SHA="${1:?usage: bootstrap.sh <commit-sha>}"
REPO="${EKIDO_REPO:-ekido-ai/Ekido}"
APP=/opt/ekido
WEB=/opt/ekido/web
OPS=/opt/ekido-ops
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -q
apt-get install -y -q python3-venv python3-pip jq curl ca-certificates gnupg rsync sqlite3 unzip \
  debian-keyring debian-archive-keyring apt-transport-https
if ! command -v node >/dev/null || ! node --version | grep -q '^v22'; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -q nodejs
fi
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi
command -v aws >/dev/null || snap install aws-cli --classic   # used by ekido-backup.sh for S3

echo "== source $REPO@${SHA:0:7}"
SRC=$(mktemp -d); trap 'rm -rf "$SRC"' EXIT
curl -fsSL --max-time 180 "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$SRC" --strip-components=1
id ekido >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin ekido

echo "== services' code and Python"
mkdir -p "$APP/infra/server" "$APP/contracts"
for d in apps/coordinator packages/ekido_core workers/agent workers/runtime workers/verifier; do
  mkdir -p "$APP/$d"
  rsync -a --delete --exclude tests --exclude __pycache__ "$SRC/$d/" "$APP/$d/"
done
install -m 0644 "$SRC/infra/server/sepolia_coordinator.py" "$APP/infra/server/sepolia_coordinator.py"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/python" -m pip install -q --upgrade pip
"$APP/venv/bin/python" -m pip install -q -r "$SRC/infra/server/requirements.txt"
"$APP/venv/bin/python" -m compileall -q "$APP/apps" "$APP/packages" "$APP/workers" "$APP/infra/server/sepolia_coordinator.py" >/dev/null
[ -f "$APP/contracts/out/GPUChainEscrow.sol/GPUChainEscrow.json" ] || echo "NOTE: escrow ABI missing at $APP/contracts/out; copy it before starting the coordinator"

echo "== dashboard"
mkdir -p "$WEB"
rsync -a --delete --exclude node_modules --exclude .next "$SRC/apps/web-combined/" "$WEB/"
chown -R ekido:ekido "$WEB"
sudo -u ekido bash -c "cd '$WEB' && npm ci --no-audit --no-fund >/tmp/ekido-npm-ci.log 2>&1"
sudo -u ekido env -C "$WEB" \
  NEXT_PUBLIC_COORDINATOR_URL=https://gpuchain.duckdns.org/sepolia \
  "NEXT_PUBLIC_COORDINATOR_WS=wss://gpuchain.duckdns.org/sepolia/ws/jobs/{job}" \
  NEXT_PUBLIC_EVENT_SOURCE=live NEXT_PUBLIC_CHAIN_ID=84532 NEXT_PUBLIC_SIGNED_JOBS=1 \
  NEXT_PUBLIC_ESCROW_ADDRESS=0x1BF65f755457a92850009c88AFb337Ec57745a22 \
  npx next build >/tmp/ekido-next-build.log 2>&1 || { tail -30 /tmp/ekido-next-build.log; exit 1; }

echo "== units and ops scripts"
for u in "$SRC"/infra/server/systemd/ekido-*; do
  sed 's/\r$//' "$u" > "/etc/systemd/system/$(basename "$u")"; chmod 0644 "/etc/systemd/system/$(basename "$u")"
done
mkdir -p "$OPS"
for f in ekido-autodeploy.sh ekido-web-deploy.sh ekido-backup.sh ekido-monitor.sh; do
  sed 's/\r$//' "$SRC/infra/server/ops/$f" > "$OPS/$f"; chmod 0755 "$OPS/$f"; bash -n "$OPS/$f"
done

echo "== auto-deploy state"
mkdir -p /var/lib/ekido-autodeploy/public && chmod 755 /var/lib/ekido-autodeploy /var/lib/ekido-autodeploy/public
echo "$SHA" > /var/lib/ekido-autodeploy/deployed
printf '{"sha":"%s","deployed_at":"%s","python":true,"web":true}\n' "$SHA" "$(date -u +%FT%TZ)" > /var/lib/ekido-autodeploy/public/version.json
chmod 644 /var/lib/ekido-autodeploy/public/version.json

echo "== caddy"
install -m 0644 "$SRC/infra/server/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
systemctl reload caddy || systemctl restart caddy

systemctl daemon-reload
systemctl enable ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web \
  ekido-smoke-provider ekido-autodeploy.timer ekido-backup.timer ekido-monitor.timer \
  ekido-retention.timer >/dev/null 2>&1
echo "Bootstrap done for ${SHA:0:7}. Next: secrets, data, escrow ABI, then start the services."
