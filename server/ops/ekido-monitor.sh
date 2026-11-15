#!/usr/bin/env bash
# Every 5 minutes: is everything a job and a payout depend on healthy? Problems go to the journal at error
# priority (journalctl -t ekido-monitor -p err) and to a status file. No external alerting is configured.
set -u
OUT=/var/lib/ekido-monitor
mkdir -p "$OUT"
problems=()
ready=$(curl -s -m 15 -w '\n%{http_code}' http://127.0.0.1:8003/readyz)
code=${ready##*$'\n'}; body=${ready%$'\n'*}
[ "$code" = "200" ] || problems+=("coordinator readyz $code: $(echo "$body" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(", ".join(f"{k}: {v[\"detail\"]}" for k,v in d["checks"].items() if not v["ok"]))' 2>/dev/null || echo unreachable)")
for svc in ekido-coordinator ekido-verifier@1 ekido-verifier@2 ekido-web ekido-smoke-provider caddy; do
  systemctl is-active --quiet "$svc" || problems+=("service $svc is $(systemctl is-active "$svc")")
done
SITE_HOST=ekido.duckdns.org
API_HOST=gpuchain.duckdns.org
[ -f /etc/ekido/site.env ] && . /etc/ekido/site.env   # A staging server sets its own names here.
for url in "https://$SITE_HOST/" "https://$API_HOST/sepolia/healthz"; do
  c=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$url"); [ "$c" = "200" ] || problems+=("$url answered $c")
done
free_pct=$(df --output=pcent / | tail -1 | tr -dc '0-9'); [ "$free_pct" -lt 85 ] || problems+=("root disk ${free_pct}% used")
now=$(date -u +%FT%TZ)
if [ ${#problems[@]} -eq 0 ]; then
  printf '{"checked":"%s","ok":true,"problems":[]}\n' "$now" > "$OUT/last.json"
  logger -t ekido-monitor -p user.info "all healthy"
else
  printf '%s\n' "${problems[@]}" | python3 -c 'import json,sys; print(json.dumps({"checked":sys.argv[1],"ok":False,"problems":[l.strip() for l in sys.stdin if l.strip()]}))' "$now" > "$OUT/last.json"
  for p in "${problems[@]}"; do logger -t ekido-monitor -p user.err "$p"; done
fi
