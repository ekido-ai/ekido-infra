#!/usr/bin/env bash
# Turns a server built by bootstrap.sh into a staging server. Run once as root, for example through SSM:
#   bash staging.sh <site-host> <api-host>        # e.g. ekido-staging.duckdns.org ekido-staging-api.duckdns.org
#
# Staging is disposable and separate from live:
#   - its own host names (/etc/ekido/site.env), read by Caddy, the web build, the monitor and the smoke provider;
#   - fresh keys generated here, on the box, for the API token and two verifiers. They are never printed and never
#     leave the server. A new smoke-provider wallet is created on first join;
#   - no chain: the coordinator runs the plain app (no escrow checks), so jobs run without on-chain settlement
#     until the fiat rail lands;
#   - no off-site backup.
# Auto-deploy runs as on live: every green commit on main is deployed.
set -euo pipefail
SITE_HOST="${1:?usage: staging.sh <site-host> <api-host>}"
API_HOST="${2:?usage: staging.sh <site-host> <api-host>}"
PY=/opt/ekido/venv/bin/python
test -x "$PY" && test -d /opt/ekido/web/.next || { echo "run bootstrap.sh first"; exit 1; }

echo "== host names"
install -d -m 0755 /etc/ekido
printf 'SITE_HOST=%s\nAPI_HOST=%s\n' "$SITE_HOST" "$API_HOST" > /etc/ekido/site.env
chmod 644 /etc/ekido/site.env

echo "== keys (generated here, never printed)"
umask 077
if [ ! -s /etc/ekido/coordinator.env ]; then
  "$PY" - "$SITE_HOST" <<'PY'
import json, secrets, sys
from eth_account import Account

site = sys.argv[1]
addresses = []
for n in (1, 2):
    account = Account.create()
    addresses.append(account.address)
    with open(f"/etc/ekido/verifier-{n}.env", "w") as handle:
        handle.write(f"VERIFIER_KEY={account.key.hex()}\n")
with open("/etc/ekido/coordinator.env", "w") as handle:
    handle.write(f"API_TOKEN={secrets.token_urlsafe(32)}\n")
    handle.write("PUBLIC_READ=true\n")
    handle.write(f"CORS_ORIGINS={json.dumps([f'https://{site}'])}\n")
    handle.write(f"VERIFIER_ADDRESSES={','.join(addresses)}\n")
    handle.write("VERIFIER_QUORUM=2\n")
    # Accounts: sign-in links point at the staging site, and go to the server log until SES is set up.
    handle.write(f"SITE_URL=https://{site}\n")
    handle.write("EMAIL_SENDER=log\n")
print("wrote coordinator.env and two verifier keys")
PY
fi
chmod 600 /etc/ekido/coordinator.env /etc/ekido/verifier-1.env /etc/ekido/verifier-2.env
umask 022
: > /etc/ekido/backup.env   # No off-site copy for staging.

echo "== coordinator without a chain"
mkdir -p /etc/systemd/system/ekido-coordinator.service.d
cat > /etc/systemd/system/ekido-coordinator.service.d/staging.conf <<'UNIT'
[Service]
# Staging: the plain app, without the Base Sepolia escrow checks in sepolia_coordinator.py.
ExecStart=
ExecStart=/opt/ekido/venv/bin/python -m uvicorn ekido_coordinator.main:create_app --factory --host 127.0.0.1 --port 8003
UNIT
systemctl daemon-reload
systemctl restart caddy

echo "== start"
systemctl enable --now ekido-coordinator
for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)" = 200 ] && break; sleep 2; done
systemctl enable --now ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider \
  ekido-autodeploy.timer ekido-monitor.timer ekido-retention.timer ekido-backup.timer
sleep 15
systemctl is-active ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider caddy | tr '\n' ' '; echo
echo "readyz: $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)"
echo "Staging is set up for $SITE_HOST (site) and $API_HOST (API)."
