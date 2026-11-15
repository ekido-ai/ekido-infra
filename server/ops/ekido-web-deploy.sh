#!/usr/bin/env bash
# Rebuilds the web app from a source archive. Used by ekido-autodeploy.sh and by hand.
#   ekido-web-deploy.sh /path/to/web-src.tgz          # src, next.config.ts, public
#   ekido-web-deploy.sh /path/to/web-src.tgz deps     # also package.json and package-lock.json, then npm ci
# The previous src, .next, next.config.ts (and with deps, node_modules and the package files) are kept outside the
# app, never deleted. A service is restarted only after its build succeeds; otherwise everything is put back.
# Exits non-zero if any app failed.
set -u
TGZ="${1:?usage: ekido-web-deploy.sh <web-src.tgz> [deps]}"
DEPS="${2:-}"
STAMP=$(date +%Y%m%d-%H%M%S)
FAILED=0
# Logs live in a root-owned folder: in /tmp, a file left by another user can't be overwritten even by root
# (fs.protected_regular), which silently skipped a build once.
LOGS=/var/log/ekido-deploy
mkdir -p "$LOGS"
as() { sudo -u ekido "$@"; }
deploy() {
  local dir=$1 svc=$2; shift 2
  echo "== $svc in $dir"
  cd "$dir" || { FAILED=1; return; }
  local bk="/opt/ekido-web-backups/$svc"
  mkdir -p "$bk" && chown ekido "$bk" /opt/ekido-web-backups
  for old in src.bak-* src.failed-* .next.bak-* .next.failed-* next.config.ts.bak-*; do [ -e "$old" ] && as mv "$old" "$bk/"; done
  as cp -a src "$bk/src.bak-$STAMP"
  as cp -a .next "$bk/.next.bak-$STAMP"
  as cp next.config.ts "$bk/next.config.ts.bak-$STAMP"
  as mv src "$bk/src.replaced-$STAMP"
  local files=(src next.config.ts public)
  if [ "$DEPS" = deps ]; then
    as cp package.json "$bk/package.json.bak-$STAMP"
    as cp package-lock.json "$bk/package-lock.json.bak-$STAMP"
    as mv node_modules "$bk/node_modules.bak-$STAMP"
    files+=(package.json package-lock.json)
  fi
  as tar -xzf "$TGZ" "${files[@]}"
  local ok=yes
  if [ "$DEPS" = deps ] && ! as npm ci --no-audit --no-fund > "$LOGS/npm-ci-$svc.log" 2>&1; then
    ok=no; echo "npm ci FAILED"; tail -20 "$LOGS/npm-ci-$svc.log"
  fi
  if [ "$ok" = yes ] && ! as env "$@" npx next build > "$LOGS/build-$svc.log" 2>&1; then
    ok=no; echo "BUILD FAILED"; tail -20 "$LOGS/build-$svc.log"
  fi
  if [ "$ok" = yes ]; then
    systemctl restart "$svc"
    sleep 4
    echo "built and restarted: $(systemctl is-active "$svc")"
  else
    FAILED=1
    echo "restoring the previous version"
    as mv src "$bk/src.failed-$STAMP"; as cp -a "$bk/src.bak-$STAMP" src
    as mv .next "$bk/.next.failed-$STAMP"; as cp -a "$bk/.next.bak-$STAMP" .next
    as cp "$bk/next.config.ts.bak-$STAMP" next.config.ts
    if [ "$DEPS" = deps ]; then
      [ -e node_modules ] && as mv node_modules "$bk/node_modules.failed-$STAMP"
      as mv "$bk/node_modules.bak-$STAMP" node_modules
      as cp "$bk/package.json.bak-$STAMP" package.json
      as cp "$bk/package-lock.json.bak-$STAMP" package-lock.json
    fi
  fi
}
SITE_HOST=ekido.duckdns.org
API_HOST=gpuchain.duckdns.org
[ -f /etc/ekido/site.env ] && . /etc/ekido/site.env   # A staging server sets its own names here.
deploy /opt/ekido/web ekido-web \
  NEXT_PUBLIC_COORDINATOR_URL="https://$API_HOST/sepolia" \
  "NEXT_PUBLIC_COORDINATOR_WS=wss://$API_HOST/sepolia/ws/jobs/{job}" \
  NEXT_PUBLIC_EVENT_SOURCE=live NEXT_PUBLIC_CHAIN_ID=84532 NEXT_PUBLIC_SIGNED_JOBS=1 \
  NEXT_PUBLIC_ESCROW_ADDRESS=0x1BF65f755457a92850009c88AFb337Ec57745a22
for u in "https://$SITE_HOST/" "https://$SITE_HOST/chain"; do
  echo "$u $(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$u")"
done
exit $FAILED
