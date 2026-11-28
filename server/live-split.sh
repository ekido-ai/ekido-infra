#!/usr/bin/env bash
# One-time: moves a server still on the single-repo, SQLite layout (the live server, 15 Nov code) to the release in
# deploy.json with Postgres, the Platform API and Inference. Run as root through SSM:  bash live-split.sh <infra-sha>
#
#  0. checks: memory, no job computing, the old data copies cleanly (read-only, with the new code in a temp folder)
#  1. backups: the SQLite file (local and S3), the code, units and settings
#  2. stops the services that write; 3. bootstrap.sh from the release; 4. postgres.sh without restarting
#  5. copies the SQLite data into Postgres and checks every row count
#  6. starts everything, checks health, switches the auto-deploy timer on
# If the copy fails, the Postgres settings are moved aside, so the services start on the untouched SQLite file.
set -euo pipefail
INFRA_SHA=${1:?usage: live-split.sh <ekido-infra sha>}
RAW="https://raw.githubusercontent.com/ekido-ai/ekido-infra/$INFRA_SHA/server"
STAMP=$(date -u +%Y%m%d-%H%M%S)
KEEP=/var/backups/ekido-split-$STAMP
DB=/var/lib/private/ekido-coordinator/jobs.db
[ -f "$DB" ] || DB=/var/lib/ekido-coordinator/jobs.db
PY=/opt/ekido/venv/bin/python
WRITERS=(ekido-smoke-provider ekido-verifier@1 ekido-verifier@2 ekido-coordinator)

echo "== 0. checks"
avail=$(awk '/MemAvailable/ {print int($2/1024)}' /proc/meminfo); echo "memory available: ${avail} MB"
[ "$avail" -ge 3000 ] || { echo "not enough memory"; exit 1; }
[ -f "$DB" ] || { echo "no SQLite file at $DB"; exit 1; }
# Same rule as ekido-autodeploy.sh: a job counts only while it still has compute in flight inside its deadline. A job
# left in RUNNING or VALIDATING past its deadline (a demo leftover) isn't computing; it is copied as it is.
busy=0
list=$(curl -sf --max-time 10 'http://127.0.0.1:8003/jobs?limit=200') || { echo "coordinator unreadable"; exit 1; }
now=$(date -u +%s)
for id in $(jq -r '.[] | select(.state as $s | ["ASSIGNED","RUNNING","RECOVERING","VALIDATING"] | index($s)) | .job_id' <<<"$list"); do
  deadline=$(curl -sf --max-time 10 "http://127.0.0.1:8003/jobs/$id" | jq -r '.deadline // empty')
  [ -n "$deadline" ] || { echo "job $id has no readable deadline"; exit 1; }
  if [ "$(date -u -d "$deadline" +%s)" -gt "$now" ]; then busy=$((busy + 1)); else echo "job $id is past its deadline ($deadline); not computing"; fi
done
echo "jobs computing: $busy"; [ "$busy" = 0 ] || { echo "a job is computing; try later"; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
curl -fsSL "$RAW/ops/ekido-release.sh" | tr -d '\r' > "$T/lib.sh"; . "$T/lib.sh"
release_resolve "$T/rel.json" "$INFRA_SHA"; release_assemble "$T/rel.json" "$T/src"
echo "release $(release_id "$T/rel.json"); CI: $(release_ci "$T/rel.json")"
cp "$DB" "$T/check.db"
PYTHONPATH="$T/src/apps/coordinator:$T/src/apps/platform:$T/src/packages/ekido_core" \
  "$PY" "$T/src/infra/server/ops/sqlite-to-postgres.py" --sqlite "$T/check.db" --check

echo "== 1. backups in $KEEP"
mkdir -p "$KEEP"; chmod 700 "$KEEP"
sqlite3 "$DB" ".backup '$KEEP/jobs.db'"
tar -czf "$KEEP/code.tgz" -C / opt/ekido/apps opt/ekido/packages opt/ekido/workers opt/ekido/infra opt/ekido-ops etc/systemd/system/ekido-*.service etc/systemd/system/ekido-*.timer etc/caddy/Caddyfile 2>/dev/null || true
cp -a /etc/ekido "$KEEP/etc-ekido"
if [ -f /etc/ekido/backup.env ]; then
  set -a; . /etc/ekido/backup.env; set +a
  [ -n "${BACKUP_BUCKET:-}" ] && aws s3 cp --only-show-errors "$KEEP/jobs.db" "s3://$BACKUP_BUCKET/before-split/jobs-$STAMP.db" && echo "SQLite copy in S3"
fi
echo "rows in SQLite: $(sqlite3 "$DB" 'select count(*) from jobs') jobs"

echo "== 2. stop the services that write"
systemctl stop "${WRITERS[@]}"

echo "== 3. bootstrap from the release"
[ -s /etc/ekido/platform.env ] || printf 'SITE_URL=https://ekido.xyz\nEMAIL_SENDER=log\nCORS_ORIGINS=["https://ekido.xyz"]\n' > /etc/ekido/platform.env
curl -fsSL "$RAW/bootstrap.sh" | tr -d '\r' > /root/bootstrap.sh
bash /root/bootstrap.sh "$INFRA_SHA" > /var/log/ekido-bootstrap-split.log 2>&1 || { tail -20 /var/log/ekido-bootstrap-split.log; echo "bootstrap failed; services stay stopped, restore from $KEEP"; exit 1; }
tail -3 /var/log/ekido-bootstrap-split.log

echo "== 4. postgres (users, schemas, migrations), without restarting"
curl -fsSL "$RAW/postgres.sh" | tr -d '\r' > /root/postgres.sh
NO_RESTART=1 bash /root/postgres.sh 2>&1 | tail -8
systemctl stop ekido-platform 2>/dev/null || true

echo "== 5. copy the data"
set +e
( set -a; . /etc/ekido/database.env; set +a
  PLATFORM_DATABASE_URL=$(sed -n 's/^DATABASE_URL=//p' /etc/ekido/database-platform.env) \
  PYTHONPATH=/opt/ekido/apps/coordinator:/opt/ekido/apps/platform:/opt/ekido/packages/ekido_core \
  "$PY" "$T/src/infra/server/ops/sqlite-to-postgres.py" --sqlite "$KEEP/jobs.db" --copy )
rc=$?
set -e
if [ "$rc" != 0 ]; then
  echo "COPY FAILED: moving the Postgres settings aside; services start on the untouched SQLite file"
  mv /etc/ekido/database.env "/etc/ekido/database.env.failed-$STAMP"
  mv /etc/ekido/database-platform.env "/etc/ekido/database-platform.env.failed-$STAMP"
fi

echo "== 6. start and check"
systemctl daemon-reload
# Never stop half-way here: a failed restart is reported below, and the other services still start.
set +e
systemctl restart ekido-coordinator ekido-platform ekido-inference || echo "a restart failed; see systemctl status"
for port in 8003 8010; do
  for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")" = 200 ] && break; sleep 2; done
done
systemctl restart ekido-verifier@1 ekido-verifier@2 ekido-smoke-provider ekido-web || echo "a restart failed; see systemctl status"
sleep 8
for port in 8003 8010; do echo "readyz $port: $(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")"; done
echo "inference: $(curl -s --max-time 5 http://127.0.0.1:8020/healthz)"
echo "jobs via the API: $(curl -sf --max-time 10 'http://127.0.0.1:8003/jobs?limit=1000' | jq length)"
systemctl is-active ekido-coordinator ekido-platform ekido-inference ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider caddy | tr '\n' ' '; echo
echo "site: $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://ekido.xyz/)  api: $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://api.ekido.xyz/sepolia/readyz)"
[ "$rc" = 0 ] || { echo "RESULT: copy failed, running on SQLite; backups in $KEEP"; exit 1; }
for port in 8003 8010; do
  [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")" = 200 ] || { echo "RESULT: copied, but $port is not ready; auto-deploy left off; backups in $KEEP"; exit 1; }
done
systemctl enable --now ekido-autodeploy.timer
echo "autodeploy timer: $(systemctl is-active ekido-autodeploy.timer)"
echo "RESULT: ok (backups in $KEEP)"
