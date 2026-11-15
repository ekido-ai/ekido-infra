#!/usr/bin/env bash
# One-time move of a server built before the rename (gpuchain-* units, /opt/gpuchain-sepolia, /etc/gpuchain) to the
# ekido-* layout. Run bootstrap.sh <sha> first: it installs the new code, venv, dashboard, units and scripts, and
# leaves the old services running. Then run this as root:
#   bash migrate-names.sh
#
# Downtime is about a minute. It copies, never moves or deletes: the old units are stopped and disabled, and every old
# file stays where it is, so the old setup can be started again. If the new stack doesn't come up healthy, it rolls
# back automatically. Removing the old files later is a separate, manual step.
set -euo pipefail
OLD_TIMERS=(gpuchain-autodeploy.timer gpuchain-backup.timer gpuchain-monitor.timer gpuchain-retention.timer)
OLD_UNITS=(gpuchain-sepolia-smoke-provider gpuchain-sepolia-verifier@1 gpuchain-sepolia-verifier@2 gpuchain-sepolia-web gpuchain-sepolia)
NEW_UNITS=(ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider)
NEW_TIMERS=(ekido-autodeploy.timer ekido-backup.timer ekido-monitor.timer ekido-retention.timer)
STAMP=$(date -u +%Y%m%d-%H%M%S)

ready() { for _ in $(seq 1 90); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)" = 200 ] && return 0; sleep 2; done; return 1; }

rollback() {
  echo "ROLLING BACK to the old units"
  systemctl stop "${NEW_TIMERS[@]}" "${NEW_UNITS[@]}" 2>/dev/null || true
  systemctl disable "${NEW_TIMERS[@]}" "${NEW_UNITS[@]}" >/dev/null 2>&1 || true
  systemctl enable "${OLD_UNITS[@]}" "${OLD_TIMERS[@]}" >/dev/null 2>&1 || true
  systemctl start gpuchain-sepolia; ready || true
  systemctl start gpuchain-sepolia-verifier@1 gpuchain-sepolia-verifier@2 gpuchain-sepolia-web gpuchain-sepolia-smoke-provider "${OLD_TIMERS[@]}"
  echo "RESULT: rolled back"
  exit 1
}

copy_dir() {  # copy_dir <old> <new>: keep a copy of anything already at <new>, then copy, owned by root
  local old=$1 new=$2
  [ -e "$old" ] || { echo "skip (missing): $old"; return 0; }
  [ -e "$new" ] && mv "$new" "$new.before-rename-$STAMP"
  cp -a "$old" "$new"
  # Root-owned, so systemd re-owns the whole tree for the new unit's dynamic user when the unit starts.
  chown -R root:root "$new"
  echo "copied $old -> $new"
}

test -x /opt/ekido/venv/bin/python && test -d /opt/ekido/web/.next || { echo "run bootstrap.sh first"; exit 1; }

echo "== secrets"
install -d -m 0755 /etc/ekido
for pair in sepolia.env:coordinator.env sepolia-verifier-1.env:verifier-1.env sepolia-verifier-2.env:verifier-2.env; do
  install -m 600 "/etc/gpuchain/${pair%%:*}" "/etc/ekido/${pair#*:}"
done
install -m 644 /etc/gpuchain/backup.env /etc/ekido/backup.env
mkdir -p /opt/ekido/contracts && cp -a /opt/gpuchain-sepolia/contracts/out /opt/ekido/contracts/
test -f /opt/ekido/contracts/out/GPUChainEscrow.sol/GPUChainEscrow.json

echo "== stop the old units"
systemctl stop "${OLD_TIMERS[@]}"
systemctl stop "${OLD_UNITS[@]}"

echo "== state"
trap rollback ERR
install -d -m 0700 /var/lib/private
copy_dir /var/lib/private/gpuchain-sepolia /var/lib/private/ekido-coordinator
copy_dir /var/lib/private/gpuchain-sepolia-verifier-1 /var/lib/private/ekido-verifier-1
copy_dir /var/lib/private/gpuchain-sepolia-verifier-2 /var/lib/private/ekido-verifier-2
copy_dir /var/lib/private/gpuchain-sepolia-smoke /var/lib/private/ekido-smoke-provider
copy_dir /var/backups/gpuchain /var/backups/ekido
copy_dir /var/lib/gpuchain-monitor /var/lib/ekido-monitor

echo "== start the new units"
systemctl daemon-reload
systemctl start ekido-coordinator
ready || rollback
# Belt and braces: whatever systemd didn't re-own, give to the folder's owner, as restore.sh does.
chown -R --reference=/var/lib/private/ekido-coordinator /var/lib/private/ekido-coordinator
systemctl start ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider
sleep 20
for u in "${NEW_UNITS[@]}"; do systemctl is-active --quiet "$u" || { echo "$u is $(systemctl is-active "$u")"; rollback; }; done
[ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:3101/)" = 200 ] || rollback
journalctl -u ekido-coordinator --since "-2 min" --no-pager -o cat | grep -q "readonly database" && rollback
trap - ERR

echo "== switch over"
systemctl disable "${OLD_UNITS[@]}" "${OLD_TIMERS[@]}" >/dev/null 2>&1 || true
systemctl enable --now "${NEW_TIMERS[@]}" >/dev/null 2>&1
systemctl is-active "${NEW_UNITS[@]}" | tr '\n' ' '; echo
echo "RESULT: ok. Old files are untouched under /opt/gpuchain-sepolia, /opt/GPUchain, /opt/gpuchain-ops, /etc/gpuchain"
echo "and /var/lib/private/gpuchain-*; remove them by hand once the new setup has run for a while."
