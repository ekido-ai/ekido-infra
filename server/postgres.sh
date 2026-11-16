#!/usr/bin/env bash
# Postgres for the Ekido services, on the same box. Run as root (for example through SSM) after bootstrap.sh:
#   bash postgres.sh
#
# One database, `ekido`, with one schema per service, each owned by that service's own login:
#   coordinator  ->  user ekido_coordinator  ->  /etc/ekido/database.env           (read by the coordinator units)
#   platform     ->  user ekido_platform     ->  /etc/ekido/database-platform.env  (read by the platform unit)
# A service's user owns only its schema and has no rights on the others, so one service can't read another's
# tables: Postgres refuses (permission denied). Each login's search_path is its own schema.
#
# Passwords are generated here and never printed. Files are mode 600. Running it again changes nothing that
# already exists. An older single-user file (user `ekido`, schema public) is moved aside, not deleted.
# Each service applies its own migrations before it starts (ExecStartPre); this script applies them once too.
set -euo pipefail
PY=/opt/ekido/venv/bin/python
test -x "$PY" || { echo "run bootstrap.sh first"; exit 1; }
export DEBIAN_FRONTEND=noninteractive
STAMP=$(date -u +%Y%m%d-%H%M%S)

echo "== postgres"
command -v psql >/dev/null || apt-get install -y -q postgresql postgresql-contrib
systemctl enable --now postgresql
CONF=$(sudo -u postgres psql -tAc "SHOW config_file")
grep -q "^listen_addresses = 'localhost'" "$CONF" || echo "listen_addresses = 'localhost'" >> "$CONF"
systemctl restart postgresql
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'ekido'" | grep -q 1 || sudo -u postgres createdb ekido
# Nobody gets to create tables in the shared public schema.
sudo -u postgres psql -d ekido -v ON_ERROR_STOP=1 -q -c "REVOKE CREATE ON SCHEMA public FROM PUBLIC"

install -d -m 0755 /etc/ekido
# service_db <schema> <env file>: a login and a schema for one service, and its connection string.
service_db() {
  local schema=$1 file=$2 role="ekido_$1"
  if [ -s "$file" ] && ! grep -q "://$role:" "$file"; then
    mv "$file" "$file.before-schemas-$STAMP"   # Old single-user layout: keep it, don't reuse it.
  fi
  if [ ! -s "$file" ]; then
    umask 077
    local password
    password=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
    sudo -u postgres psql -d ekido -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role') THEN CREATE ROLE $role LOGIN; END IF;
END \$\$;
ALTER ROLE $role PASSWORD '$password';
CREATE SCHEMA IF NOT EXISTS $schema AUTHORIZATION $role;
ALTER ROLE $role IN DATABASE ekido SET search_path = $schema;
REVOKE ALL ON SCHEMA $schema FROM PUBLIC;
SQL
    printf 'DATABASE_URL=postgresql+psycopg://%s:%s@127.0.0.1:5432/ekido?options=-csearch_path%%3D%s\n' "$role" "$password" "$schema" > "$file"
    unset password
    umask 022
  fi
  chmod 600 "$file"
  echo "$schema: user $role"
}
service_db coordinator /etc/ekido/database.env
service_db platform /etc/ekido/database-platform.env

echo "== migrations"
migrate() {  # migrate <app folder> <env file>
  ( set -a; . "$2"; set +a
    cd "/opt/ekido/apps/$1"
    PYTHONPATH="/opt/ekido/apps/$1:/opt/ekido/packages/ekido_core" "$PY" -m alembic -c alembic/alembic.ini upgrade head
    PYTHONPATH="/opt/ekido/apps/$1:/opt/ekido/packages/ekido_core" "$PY" -m alembic -c alembic/alembic.ini current )
}
migrate coordinator /etc/ekido/database.env
migrate platform /etc/ekido/database-platform.env

# NO_RESTART=1: leave the services stopped, for example to copy old SQLite data in first (ops/sqlite-to-postgres.py).
if [ "${NO_RESTART:-0}" = 1 ]; then echo "== NO_RESTART=1: services not restarted"; exit 0; fi
echo "== restart on Postgres"
systemctl daemon-reload
systemctl restart ekido-coordinator ekido-platform
for port in 8003 8010; do
  for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")" = 200 ] && break; sleep 2; done
  echo "readyz on $port: $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")"
done
