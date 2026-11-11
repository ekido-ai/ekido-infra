#!/usr/bin/env bash
# Sets up the no-Docker tools in .cache/, which git ignores: CPU torch and the other Python packages, plus
# anvil and forge from Foundry's official release, checked against its published SHA-256. About 1.5 GB of disk.
set -eu
cd "$(dirname "${BASH_SOURCE[0]}")/.."
FOUNDRY=v1.9.0

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) ASSET="foundry_${FOUNDRY}_win32_amd64.zip"; SHA=2b02444d46c9218fe2e6be66015265ce131fe6b7c384e33e630b00f9a0bff072; PY=.cache/cpu-venv/Scripts/python.exe; MAKE="py -3" ;;
  *) echo "This script covers Windows only. On Linux or macOS, prepare equivalent host tools or use Docker: bash infra/run-tests.sh"; exit 1 ;;
esac

mkdir -p .cache/foundry
[ -x "$PY" ] || $MAKE -m venv .cache/cpu-venv
export PIP_DISABLE_PIP_VERSION_CHECK=1
"$PY" -m pip install --timeout 300 --retries 10 --resume-retries 20 "torch==2.15.0" --index-url https://download.pytorch.org/whl/cpu
"$PY" -m pip install --timeout 300 --retries 10 --resume-retries 20 safetensors numpy eth-account "sqlalchemy>=2" "fastapi>=0.115" httpx httpx2 pydantic-settings websockets "uvicorn>=0.30" web3

if [ ! -x .cache/foundry/anvil.exe ]; then
  curl -fsSL -o .cache/foundry/foundry.zip "https://github.com/foundry-rs/foundry/releases/download/${FOUNDRY}/${ASSET}"
  echo "${SHA}  .cache/foundry/foundry.zip" | sha256sum -c -
  "$PY" -c "import zipfile; z = zipfile.ZipFile('.cache/foundry/foundry.zip'); [z.extract(n, '.cache/foundry') for n in ('anvil.exe', 'forge.exe')]"
  rm -f .cache/foundry/foundry.zip
fi
echo "Ready. Tests: bash infra/run-tests-host.sh    Demo: $PY experiments/demo_stack.py --cpu --no-docker"
