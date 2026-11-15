#!/usr/bin/env bash
# Moves a server's coordinator database to Postgres on the same box. Run once as root (for example through SSM),
# after bootstrap.sh:
#   bash postgres.sh            # a fresh server (staging): empty database, schema from the migrations
#
# What it does:
#   - installs Postgres 16 and keeps it listening on localhost only;
#   - creates the `ekido` role and database with a password generated here, never printed;
#   - writes DATABASE_URL to /etc/ekido/database.env (mode 600), which the coordinator and retention units read;
#   - applies the Alembic migrations; the coordinator unit also applies them on every start (ExecStartPre).
# Copying an existing SQLite database's rows across is a separate step (the live server, on day 7).
set -euo pipefail
PY=/opt/ekido/venv/bin/python
test -x "$PY" || { echo "run bootstrap.sh first"; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "== postgres"
command -v psql >/dev/null || apt-get install -y -q postgresql postgresql-contrib
systemctl enable --now postgresql
CONF=$(sudo -u postgres psql -tAc "SHOW config_file")
grep -q "^listen_addresses = 'localhost'" "$CONF" || echo "listen_addresses = 'localhost'" >> "$CONF"
systemctl restart postgresql

echo "== role and database"
install -d -m 0755 /etc/ekido
if [ ! -s /etc/ekido/database.env ]; then
  umask 077
  PASSWORD=$("$PY" -c "import secrets; print(secrets.token_urlsafe(32))")
  sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ekido') THEN CREATE ROLE ekido LOGIN; END IF;
END \$\$;
ALTER ROLE ekido PASSWORD '$PASSWORD';
SQL
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = 'ekido'" | grep -q 1 || sudo -u postgres createdb -O ekido ekido
  printf 'DATABASE_URL=postgresql+psycopg://ekido:%s@127.0.0.1:5432/ekido\n' "$PASSWORD" > /etc/ekido/database.env
  unset PASSWORD
  umask 022
fi
chmod 600 /etc/ekido/database.env

echo "== migrations"
set -a; . /etc/ekido/database.env; set +a
cd /opt/ekido/apps/coordinator
PYTHONPATH=/opt/ekido/apps/coordinator:/opt/ekido/packages/ekido_core "$PY" -m alembic -c alembic.ini upgrade head
PYTHONPATH=/opt/ekido/apps/coordinator:/opt/ekido/packages/ekido_core "$PY" -m alembic -c alembic.ini current

echo "== restart on Postgres"
systemctl daemon-reload
systemctl restart ekido-coordinator
for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)" = 200 ] && break; sleep 2; done
echo "readyz: $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8003/readyz)"
