#!/usr/bin/env bash
# The assistant's model engines: llama.cpp `llama-server`, built from source at a pinned tag, serving GGUF models that
# are pinned and checked in models.lock.json. Run as root through SSM, from a checkout of this folder:
#
#   bash assistant-engine.sh build                 # build llama-server (skipped if this tag is already built)
#   bash assistant-engine.sh fetch guard qwen2.5-1.5b ...   # download, verify (sha256) and if needed convert to GGUF
#   bash assistant-engine.sh install               # install the two systemd units (they start once a model is set)
#   bash assistant-engine.sh use <model>           # point the assistant engine at a fetched model and restart it
#   bash assistant-engine.sh status                # what's built, fetched, running, and how much memory it uses
#
# Two units, loopback only, each with its own memory cap: ekido-assistant-guard (Qwen3Guard, port 8031) and
# ekido-assistant (the chosen model, port 8032). Platform reaches them as ASSISTANT_GUARD_URL / ASSISTANT_ENGINE_URL.
# Nothing here deletes anything: a re-run reuses what's already there, and a failed check leaves the file aside as
# *.bad for a person to look at.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
LOCK="$HERE/models.lock.json"
ROOT=/opt/llama.cpp
MODELS=/var/lib/ekido-models
ENVF=/etc/ekido/assistant-engine.env
PY=/opt/ekido/venv/bin/python
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }
lock() { python3 -c "import json,sys; d=json.load(open('$LOCK')); print(eval(sys.argv[1], {'d': d}))" "$1"; }

need_disk() {  # need_disk <GiB>: refuse to start when the root disk can't take it, instead of filling it
  local free_kb; free_kb=$(df --output=avail -k / | tail -1)
  if [ "$free_kb" -lt $(( $1 * 1024 * 1024 )) ]; then
    echo "not enough disk: $(( free_kb / 1024 / 1024 )) GiB free, this step needs about $1 GiB"; exit 1
  fi
}

build() {
  local tag; tag=$(lock "d['llama_cpp']['tag']")
  if [ -x "$ROOT/bin/llama-server" ] && [ "$(cat "$ROOT/bin/TAG" 2>/dev/null)" = "$tag" ]; then
    echo "llama-server $tag already built"; return
  fi
  need_disk 3
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -q build-essential cmake git >/dev/null
  install -d -m 0755 "$ROOT" "$ROOT/bin"
  local src="$ROOT/src-$tag"
  [ -d "$src/.git" ] || git clone -q --depth 1 --branch "$tag" "$(lock "d['llama_cpp']['repo']")" "$src"
  git -C "$src" rev-parse HEAD > "$ROOT/bin/COMMIT"
  cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DGGML_NATIVE=ON >/dev/null
  cmake --build "$src/build" --target llama-server -j 2 >/dev/null
  install -m 0755 "$src/build/bin/llama-server" "$ROOT/bin/llama-server"
  echo "$tag" > "$ROOT/bin/TAG"
  echo "built llama-server $tag ($(cut -c1-12 "$ROOT/bin/COMMIT"))"
}

fetch_one() {  # fetch_one <name>
  local name=$1 repo rev convert dir out
  repo=$(lock "d['models']['$name']['repo']"); rev=$(lock "d['models']['$name']['revision']")
  convert=$(lock "d['models']['$name']['convert']")
  out="$MODELS/$name.gguf"
  [ -s "$out" ] && { echo "$name: already fetched"; return; }
  dir="$MODELS/src/$name"; install -d -m 0755 "$dir"
  local files; files=$(lock "' '.join(d['models']['$name']['files'])")
  for f in $files; do
    local want; want=$(lock "d['models']['$name']['files']['$f'] or ''")
    if [ ! -s "$dir/$f" ]; then
      curl -fsSL --retry 3 --max-time 3600 -o "$dir/$f.part" "https://huggingface.co/$repo/resolve/$rev/$f"
      mv "$dir/$f.part" "$dir/$f"
    fi
    if [ -n "$want" ] && [ "$(sha256sum "$dir/$f" | cut -d' ' -f1)" != "$want" ]; then
      mv "$dir/$f" "$dir/$f.bad"; echo "$name: $f failed its sha256 check (kept as $f.bad)"; exit 1
    fi
  done
  if [ "$convert" = True ]; then
    local quant; quant=$(lock "d['models']['$name']['quant']")
    local tag; tag=$(lock "d['llama_cpp']['tag']")
    PYTHONPATH="$ROOT/src-$tag/gguf-py" "$PY" "$ROOT/src-$tag/convert_hf_to_gguf.py" "$dir" --outtype "$quant" --outfile "$out.part" >/dev/null
    mv "$out.part" "$out"
  else
    ln -sf "$dir/$files" "$out"
  fi
  echo "$name: $(du -shL "$out" | cut -f1) ready at $out"
}

fetch() {
  [ $# -gt 0 ] || { echo "name the models to fetch"; exit 1; }
  install -d -m 0755 "$MODELS"
  for name in "$@"; do fetch_one "$name"; done
}

install_units() {
  id ekido-llm >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ekido-llm
  install -m 0644 "$HERE/../systemd/ekido-assistant-guard.service" "$HERE/../systemd/ekido-assistant.service" /etc/systemd/system/
  [ -f "$ENVF" ] || { umask 077; printf 'GUARD_MODEL=%s/guard.gguf\nASSISTANT_MODEL=\n' "$MODELS" > "$ENVF"; chmod 644 "$ENVF"; }
  systemctl daemon-reload
  [ -s "$MODELS/guard.gguf" ] && systemctl enable --now ekido-assistant-guard
  echo "units installed"
}

use() {
  local model="$MODELS/$1.gguf"
  [ -s "$model" ] || { echo "$1 is not fetched"; exit 1; }
  sed -i "s|^ASSISTANT_MODEL=.*|ASSISTANT_MODEL=$model|" "$ENVF"
  systemctl enable ekido-assistant >/dev/null 2>&1
  systemctl restart ekido-assistant
  for _ in $(seq 1 60); do curl -sf --max-time 2 http://127.0.0.1:8032/health >/dev/null && break; sleep 2; done
  echo "assistant engine now on $1: $(curl -s --max-time 2 http://127.0.0.1:8032/health)"
}

status() {
  echo "llama-server: $(cat "$ROOT/bin/TAG" 2>/dev/null || echo 'not built') $(cut -c1-12 "$ROOT/bin/COMMIT" 2>/dev/null)"
  ls -1sh "$MODELS"/*.gguf 2>/dev/null || echo "no models fetched"
  for u in ekido-assistant-guard ekido-assistant; do
    echo "$u: $(systemctl is-active "$u" 2>/dev/null) memory=$(systemctl show "$u" -p MemoryCurrent --value 2>/dev/null)"
  done
  grep -h '^ASSISTANT_MODEL=' "$ENVF" 2>/dev/null || true
  df -h / | tail -1
}

case "${1:-}" in
  build) build ;;
  fetch) shift; fetch "$@" ;;
  install) install_units ;;
  use) use "${2:?model name}" ;;
  status) status ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
