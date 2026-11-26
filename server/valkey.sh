#!/usr/bin/env bash
# Valkey, the Platform's cache (ekido-internal #142), on the same box. Run as root after bootstrap.sh:
#   bash valkey.sh
#
# A cache only: this machine only, 256 MB, least-recently-used keys dropped first, nothing written to disk. Postgres stays the source of truth.
#
# It has a password even though it listens on localhost only: it remembers who an API key or a session belongs to, so a local process that
# could write to it could pretend to be any caller. The password is generated here, never printed, and readable only by root
# (systemd reads the file for the platform unit). Running this again keeps the password.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== valkey"
command -v valkey-server >/dev/null || apt-get install -y -q valkey-server
CONF=/etc/valkey/valkey.conf
test -f "$CONF" || { echo "no $CONF: is this the Ubuntu valkey-server package?"; exit 1; }
install -d -m 0755 /etc/ekido

if [ ! -s /etc/ekido/valkey.env ]; then
  umask 077
  password=$(/opt/ekido/venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))")
  printf 'VALKEY_URL=redis://:%s@127.0.0.1:6379/0\n' "$password" > /etc/ekido/valkey.env
  unset password
  umask 022
fi
chmod 600 /etc/ekido/valkey.env

# Our settings live in one included file; the password is taken from valkey.env and never shown.
( umask 077
  {
    printf 'bind 127.0.0.1\nprotected-mode yes\nmaxmemory 256mb\nmaxmemory-policy allkeys-lru\nsave ""\nappendonly no\n'
    sed -n 's#^VALKEY_URL=redis://:\([^@]*\)@.*#requirepass \1#p' /etc/ekido/valkey.env
  } > /etc/valkey/ekido.conf )
grep -q '^requirepass ' /etc/valkey/ekido.conf || { echo "no password was written: check /etc/ekido/valkey.env"; exit 1; }
chown valkey:valkey /etc/valkey/ekido.conf
chmod 600 /etc/valkey/ekido.conf
grep -qx 'include /etc/valkey/ekido.conf' "$CONF" || printf '\ninclude /etc/valkey/ekido.conf\n' >> "$CONF"

# A hard cap on the process too, above the data cap, so it can never starve the database or the coordinator.
install -d /etc/systemd/system/valkey-server.service.d
printf '[Service]\nMemoryMax=384M\n' > /etc/systemd/system/valkey-server.service.d/ekido.conf
systemctl daemon-reload
systemctl enable valkey-server
systemctl restart valkey-server

# The password goes through the environment, never the command line.
( set -a; . /etc/ekido/valkey.env; set +a
  rest=${VALKEY_URL#redis://:}
  export REDISCLI_AUTH=${rest%@*}
  valkey-cli ping | grep -qx PONG ) && echo "valkey: answering on 127.0.0.1" || { echo "valkey did not answer"; exit 1; }
echo "Next: install the client in /opt/ekido/venv (see RUNBOOK, Cache), then: systemctl restart ekido-platform"
