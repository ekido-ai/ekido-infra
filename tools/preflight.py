"""Run before every rehearsal and before going on stage: python infra/preflight.py

Each check prints OK, WARN or FAIL, with what to do. Exit code 1 means don't start the demo.
Only reads things. It changes nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_FREE_DISK_GB = 15
WARN_FREE_RAM_GB = 6
FAIL_FREE_RAM_GB = 4
WARN_TEMP_C = 70
IMAGE = "ekido-runtime:spike"
results: list[tuple[str, str, str]] = []


def report(level: str, name: str, detail: str) -> None:
    results.append((level, name, detail))
    print(f"[{level:4}] {name}: {detail}", flush=True)


def run(command: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)


def free_ram_gb() -> float | None:
    if sys.platform == "win32":
        code, out = run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"])
        return int(out.strip()) / 2**20 if code == 0 and out.strip().isdigit() else None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except OSError:
        pass  # No /proc/meminfo here; the function returns None, meaning unknown.
    return None


def check_resources() -> None:
    free = shutil.disk_usage(ROOT).free / 2**30
    report("OK" if free >= MIN_FREE_DISK_GB else "WARN", "Disk", f"{free:.1f} GB free (want {MIN_FREE_DISK_GB}+). Each run also leaves files in the temp folder")
    ram = free_ram_gb()
    if ram is None:
        report("WARN", "Memory", "couldn't read free memory")
    else:
        level = "FAIL" if ram < FAIL_FREE_RAM_GB else "WARN" if ram < WARN_FREE_RAM_GB else "OK"
        report(level, "Memory", f"{ram:.1f} GB free. A run was stopped at 1.9 GB, so close the browser and other apps")


def check_gpu() -> None:
    code, out = run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"])
    if code != 0:
        report("FAIL", "GPU", "nvidia-smi failed. Use the CPU fallback: experiments/demo_stack.py --cpu --no-docker")
        return
    name, used, total, temp = [part.strip() for part in out.strip().splitlines()[0].split(",")]
    report("OK", "GPU", f"{name}, {used}/{total} MiB used")
    if int(used) > 1500:
        report("WARN", "GPU memory", f"{used} MiB already in use. Something else may be holding the GPU")
    report("OK" if int(temp) < WARN_TEMP_C else "WARN", "GPU temperature", f"{temp}°C at idle. Reached 88°C in training, so use AC power and cooling")


def check_docker() -> None:
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if code != 0:
        report("FAIL", "Docker", "not running. Start Docker Desktop")
        return
    report("OK", "Docker", f"version {out.strip()}")
    code, _ = run(["docker", "image", "inspect", IMAGE])
    report("OK" if code == 0 else "FAIL", "Runtime image", IMAGE if code == 0 else f"{IMAGE} is missing: docker build -t {IMAGE} -f infra/runtime.Dockerfile infra")
    code, out = run(["docker", "volume", "ls", "-q", "--filter", "name=gpuchain_hf_cache"])
    has_cache = "gpuchain_hf_cache" in out
    report("OK" if has_cache else "FAIL", "Cached model and data", "present, so no downloads on stage" if has_cache else "missing: run experiments/spike_feasibility.py once while online")
    code, out = run(["docker", "ps", "--format", "{{.Names}}", "--filter", "name=gpuchain-"])
    leftovers = out.split()
    report("WARN" if leftovers else "OK", "Leftover containers", f"{leftovers}: these can hold GPU memory. Run: docker kill {' '.join(leftovers)}" if leftovers else "none")


def check_cpu_fallback() -> None:
    """The way out if the GPU or Docker fails on stage, so it must be ready before anyone needs it."""
    python = ROOT / ".cache" / "cpu-venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    anvil = ROOT / ".cache" / "foundry" / ("anvil.exe" if sys.platform == "win32" else "anvil")
    ready = python.is_file() and anvil.is_file()
    report("OK" if ready else "WARN", "CPU fallback", "ready: demo_stack.py --cpu --no-docker" if ready else "not set up: run bash infra/setup-host.sh while online")


def check_project() -> None:
    env = ROOT / ".env"
    report("OK" if env.is_file() else "WARN", ".env", "present" if env.is_file() else "missing: only needed for Base Sepolia")
    built = (ROOT / "contracts" / "out" / "GPUChainEscrow.sol" / "GPUChainEscrow.json").is_file()
    report("OK" if built else "WARN", "Contracts built", "yes" if built else "no: demo_stack.py builds them on start")
    try:
        import eth_account  # noqa: F401
        import fastapi  # noqa: F401
        import web3  # noqa: F401
        import websockets  # noqa: F401

        report("OK", "Python packages", f"found in {sys.executable}")
    except ImportError as exc:
        report("FAIL", "Python packages", f"{exc}. Use the project venv: .venv\\Scripts\\python.exe")


def check_testnet() -> None:
    """Base Sepolia reachability and gas, if wallets exist. Failing here means using the local-chain fallback."""
    rpc = os.environ.get("RPC_URL", "https://sepolia.base.org")
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
        # Some RPC gateways refuse urllib's default User-Agent, which would look like a network failure.
        request = urllib.request.Request(rpc, data=body, headers={"Content-Type": "application/json", "User-Agent": "gpuchain-preflight/0.1"})
        with urllib.request.urlopen(request, timeout=8) as response:
            chain = int(json.loads(response.read())["result"], 16)
        report("OK", "Base Sepolia RPC", f"reachable, chain {chain}")
    except Exception as exc:  # Any network failure means the same thing here.
        report("WARN", "Base Sepolia RPC", f"unreachable ({type(exc).__name__}). Use the local chain and say so on stage")


def main() -> int:
    for check in (check_resources, check_gpu, check_docker, check_cpu_fallback, check_project, check_testnet):
        check()
    failures = [r for r in results if r[0] == "FAIL"]
    warnings = [r for r in results if r[0] == "WARN"]
    print(f"\n{'NOT READY' if failures else 'READY'}: {len(failures)} failed, {len(warnings)} warnings", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
