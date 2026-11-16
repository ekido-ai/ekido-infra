#!/usr/bin/env bash
# Pull-based continuous deployment for the hosted Base Sepolia network.
#
# Every few minutes (ekido-autodeploy.timer) this checks the newest commit on main. It deploys only when every
# required CI check on that commit has passed, and never holds a GitHub credential: the repository is public, so it
# reads the commit, its check runs and the source archive anonymously. Nothing in GitHub can reach into this server.
#
# Python services: refused while a job is unfinished (retried next run); the previous code is backed up and put back
# if /readyz isn't ready within a minute. Web apps: rebuilt with the previous build kept and restored on failure; a
# changed lockfile is installed with npm ci (CI has already built with it), keeping the old node_modules to put back.
# Python dependency changes (pyproject.toml) are never deployed automatically.
set -euo pipefail

REPO="${EKIDO_REPO:-ekido-ai/Ekido}"
BRANCH="${EKIDO_BRANCH:-main}"
STATE=/var/lib/ekido-autodeploy
PUBLIC="$STATE/public"
APP=/opt/ekido
BACKUPS=/opt/ekido-backups
WEB_DEPLOY=/opt/ekido-ops/ekido-web-deploy.sh
REQUIRED='["Python (core)","Python (runtime)","Python (agent)","Python (coordinator)","Python (verifier)","Web app","Contracts"]'
API="https://api.github.com/repos/$REPO"
CURL=(curl -fsS --max-time 30 -H "Accept: application/vnd.github+json" -H "User-Agent: ekido-autodeploy")

mkdir -p "$STATE" "$PUBLIC" "$BACKUPS"
chmod 755 "$STATE" "$PUBLIC"
log() { echo "$*"; }
DRY="${DRY_RUN:-0}"   # DRY_RUN=1: check CI and compare code, report what would happen, change nothing
fail() {
  echo "FAILED: $*"
  [ "$DRY" = 1 ] && exit 1
  logger -p err -t ekido-autodeploy "$*"; echo "$SHA" > "$STATE/failed"
  # Publish the failure next to the running version, so the GitHub deploy check can stop waiting and say why.
  if [ -f "$PUBLIC/version.json" ]; then
    jq --arg sha "$SHA" --arg why "$*" '. + {failed_sha: $sha, failed_reason: $why}' "$PUBLIC/version.json" > "$PUBLIC/version.json.tmp" \
      && chmod 644 "$PUBLIC/version.json.tmp" && mv "$PUBLIC/version.json.tmp" "$PUBLIC/version.json"
  fi
  exit 1
}

SHA=$(git ls-remote "https://github.com/$REPO" "refs/heads/$BRANCH" | cut -f1)
[ -n "$SHA" ] || { echo "could not read $BRANCH"; exit 1; }
LAST=$(cat "$STATE/deployed" 2>/dev/null || true)
[ "$SHA" = "$LAST" ] && { [ "$DRY" = 1 ] && echo "${SHA:0:7} is already deployed"; exit 0; }
[ "$DRY" != 1 ] && [ "$SHA" = "$(cat "$STATE/failed" 2>/dev/null || true)" ] && exit 0   # don't retry a commit that already failed

# ── 1. CI must be green ────────────────────────────────────────────────────
RUNS=$("${CURL[@]}" "$API/commits/$SHA/check-runs?per_page=100")
VERDICT=$(jq -r --argjson need "$REQUIRED" '
  [.check_runs[] | {name, status, conclusion}] as $runs
  | [$need[] as $n | ($runs | map(select(.name == $n)) | first)] as $mine
  | if any($mine[]; . == null or .status != "completed") then "wait"
    elif all($mine[]; .conclusion == "success") then "green"
    else "red" end' <<<"$RUNS")
case "$VERDICT" in
  wait) log "${SHA:0:7}: CI still running"; exit 0 ;;
  red) fail "${SHA:0:7}: CI failed, not deploying" ;;
esac
log "${SHA:0:7}: CI green"

# ── 2. What changed: compare the new source with what is running, file by file ──
SRC=$(mktemp -d); trap 'rm -rf "$SRC"' EXIT
curl -fsSL --max-time 120 "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$SRC" --strip-components=1
# apps/coordinator/alembic holds the database migrations, which the coordinator unit applies before it starts.
DIRS=(apps/coordinator/ekido_coordinator apps/coordinator/alembic apps/platform/ekido_platform apps/platform/alembic packages/ekido_core/ekido_core workers/agent/ekido_agent workers/runtime/ekido_runtime workers/verifier/ekido_verifier)
WEB_LIVE=/opt/ekido/web
same() { diff -rq --strip-trailing-cr --exclude tests --exclude __pycache__ "$1" "$2" >/dev/null 2>&1; }

# Python dependencies are never changed automatically.
if [ -f "$APP/apps/coordinator/pyproject.toml" ] && ! same "$SRC/apps/coordinator/pyproject.toml" "$APP/apps/coordinator/pyproject.toml"; then
  fail "${SHA:0:7}: coordinator dependencies changed; deploy this one by hand (see RUNBOOK.md)"
fi

PY=no; WEB=no; DEPS=no
for d in "${DIRS[@]}"; do same "$SRC/$d" "$APP/$d" || PY=yes; done
same "$SRC/infra/server/sepolia_coordinator.py" "$APP/infra/server/sepolia_coordinator.py" || PY=yes
for p in src public next.config.ts; do same "$SRC/apps/web-combined/$p" "$WEB_LIVE/$p" || WEB=yes; done
for f in package.json package-lock.json; do same "$SRC/apps/web-combined/$f" "$WEB_LIVE/$f" || { WEB=yes; DEPS=yes; }; done
log "${SHA:0:7}: python=$PY web=$WEB web_dependencies=$DEPS"
[ "$DRY" = 1 ] && { log "dry run: stopping before any change"; exit 0; }

# ── 3. Python services ──────────────────────────────────────────────────────
UNITS=(ekido-coordinator ekido-platform ekido-verifier@1 ekido-verifier@2 ekido-smoke-provider)
# A restart is refused only while a job has compute in flight: assigned, training, recovering or being evaluated, and
# still inside its deadline. A COMPLETED job's settlement and an OPEN job's funding both resume after a restart.
# Anything unreadable counts as busy.
busy_jobs() {
  local list id deadline n=0 now
  list=$(curl -sf --max-time 10 'http://127.0.0.1:8003/jobs?limit=50') || { echo unknown; return; }
  jq -e 'type == "array"' >/dev/null 2>&1 <<<"$list" || { echo unknown; return; }
  now=$(date -u +%s)
  for id in $(jq -r '.[] | select(.state as $s | ["ASSIGNED","RUNNING","RECOVERING","VALIDATING"] | index($s)) | .job_id' <<<"$list"); do
    deadline=$(curl -sf --max-time 10 "http://127.0.0.1:8003/jobs/$id" | jq -r '.deadline // empty' 2>/dev/null)
    [ -n "$deadline" ] || { echo unknown; return; }
    deadline=$(date -u -d "$deadline" +%s 2>/dev/null) || { echo unknown; return; }
    if [ "$deadline" -gt "$now" ]; then n=$((n + 1)); fi
  done
  echo "$n"
}
# The Coordinator (8003) and the Platform API (8010) must both answer /readyz.
ready() {
  local port
  for port in 8003 8010; do
    for _ in $(seq 1 60); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/readyz")" = 200 ] && continue 2; sleep 1; done
    return 1
  done
}
if [ "$PY" = yes ]; then
  BUSY=$(busy_jobs)
  if [ "$BUSY" != 0 ]; then log "${SHA:0:7}: $BUSY job(s) still computing, or the coordinator is unreadable; will retry"; exit 0; fi
  K="$BACKUPS/auto-$(date -u +%Y%m%d-%H%M%S)-${LAST:0:7}"
  mkdir -p "$K"
  # Back up only what exists: after a package rename the new folders aren't on the server yet.
  HAVE=(infra/server/sepolia_coordinator.py)
  for d in "${DIRS[@]}"; do if [ -e "$APP/$d" ]; then HAVE+=("$d"); fi; done
  tar -czf "$K/code.tgz" -C "$APP" "${HAVE[@]}"
  for d in "${DIRS[@]}"; do
    mkdir -p "$APP/$d"
    rsync -a --delete --exclude tests --exclude __pycache__ "$SRC/$d/" "$APP/$d/"
  done
  install -m 0644 "$SRC/infra/server/sepolia_coordinator.py" "$APP/infra/server/sepolia_coordinator.py"
  if ! "$APP/venv/bin/python" -m compileall -q "${DIRS[@]/#/$APP/}" "$APP/infra/server/sepolia_coordinator.py" >/dev/null; then
    tar -xzf "$K/code.tgz" -C "$APP"; fail "${SHA:0:7}: code did not compile; previous code restored"
  fi
  systemctl restart "${UNITS[@]}"
  if ! ready; then
    tar -xzf "$K/code.tgz" -C "$APP"; systemctl restart "${UNITS[@]}"; ready || true
    fail "${SHA:0:7}: /readyz not ready after restart; previous code restored"
  fi
  log "${SHA:0:7}: python services deployed (backup $K)"
fi

# ── 4. Web apps ─────────────────────────────────────────────────────────────
if [ "$WEB" = yes ]; then
  TGZ="$STATE/web-src.tgz"
  if [ "$DEPS" = yes ]; then
    tar -czf "$TGZ" -C "$SRC/apps/web-combined" src next.config.ts public package.json package-lock.json
    bash "$WEB_DEPLOY" "$TGZ" deps || fail "${SHA:0:7}: web dependencies or build failed; previous version kept"
  else
    tar -czf "$TGZ" -C "$SRC/apps/web-combined" src next.config.ts public
    bash "$WEB_DEPLOY" "$TGZ" || fail "${SHA:0:7}: web build failed; previous build kept"
  fi
  log "${SHA:0:7}: web apps deployed"
fi

# ── 4b. Inference (Rust): built here, swapped in only if the new binary answers /healthz ──
if ! same "$SRC/apps/inference" "$APP/apps/inference"; then
  CARGO=/root/.cargo/bin/cargo
  [ -x "$CARGO" ] || fail "${SHA:0:7}: inference changed but Rust isn't installed; run bootstrap.sh"
  mkdir -p "$APP/apps/inference" /opt/ekido/bin
  rsync -a --delete --exclude target "$SRC/apps/inference/" "$APP/apps/inference/"
  if ! (cd "$APP/apps/inference" && CARGO_TARGET_DIR=/var/cache/ekido-inference-build nice "$CARGO" build --release --locked -q); then
    fail "${SHA:0:7}: inference build failed; previous binary kept"
  fi
  [ -x /opt/ekido/bin/ekido-inference ] && cp -a /opt/ekido/bin/ekido-inference "/opt/ekido/bin/ekido-inference.previous"
  install -m 0755 /var/cache/ekido-inference-build/release/ekido-inference /opt/ekido/bin/ekido-inference
  systemctl restart ekido-inference
  ok=no
  for _ in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:8020/healthz)" = 200 ] && { ok=yes; break; }; sleep 1; done
  if [ "$ok" != yes ]; then
    [ -x /opt/ekido/bin/ekido-inference.previous ] && install -m 0755 /opt/ekido/bin/ekido-inference.previous /opt/ekido/bin/ekido-inference && systemctl restart ekido-inference
    fail "${SHA:0:7}: inference didn't answer /healthz; previous binary restored"
  fi
  log "${SHA:0:7}: inference deployed"
fi

# ── 5. Record it ────────────────────────────────────────────────────────────
echo "$SHA" > "$STATE/deployed"
rm -f "$STATE/failed"
jq -n --arg sha "$SHA" --arg at "$(date -u +%FT%TZ)" --arg py "$PY" --arg web "$WEB" \
  '{sha: $sha, deployed_at: $at, python: ($py == "yes"), web: ($web == "yes")}' > "$PUBLIC/version.json.tmp"
chmod 644 "$PUBLIC/version.json.tmp"; mv "$PUBLIC/version.json.tmp" "$PUBLIC/version.json"
log "${SHA:0:7}: deployed"
