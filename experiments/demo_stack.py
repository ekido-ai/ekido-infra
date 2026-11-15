"""Starts the whole demo stack and keeps it running, for the dashboard and rehearsals:
coordinator, verifiers, providers, and a local chain with the contracts deployed.

    python experiments/demo_stack.py            real GPU, local Anvil chain
    python experiments/demo_stack.py --cpu      synthetic CPU fallback, no GPU needed
    python experiments/demo_stack.py --testnet  real GPU, Base Sepolia (contracts and wallets from .env)

Prints the API address, provider IDs, and a ready-to-post job. Ctrl+C stops everything it started.
All GPU providers share one physical card, so B and C are capacity limits on it, labelled as such.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = os.pathsep.join(str(ROOT / p) for p in ("apps/coordinator", "packages/gpuchain_core", "workers/agent", "workers/runtime", "workers/verifier"))
CONTAINER_PATH = "/app/packages/gpuchain_core:/app/workers/runtime:/app/workers/verifier"
FOUNDRY = "ghcr.io/foundry-rs/foundry:latest"
FOUNDRY_BIN = ROOT / ".cache" / "foundry"  # anvil and forge, for --no-docker. See docs/demo-runbook.md.
NO_DOCKER = False  # Set from the command line.
CPU_DECLARED_MIB = 4096  # The compiler's margin check needs a number. Nothing on a CPU enforces it.
CPU_CONTAINER = "gpuchain-cpu-stack"
CPU_IMAGE = "gpuchain-test:cpu"
CPU_PATH = ":".join(f"/repo/{p}" for p in ("apps/coordinator", "packages/gpuchain_core", "workers/agent", "workers/runtime", "workers/verifier"))
# Anvil's first default account. A public test key with no value anywhere.
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
# Fixed, public demo-only keys for the local chain. Base Sepolia uses the wallets in .env instead.
LOCAL = {name: "0x" + value * 32 for name, value in {
    "coordinator": "c1", "client": "c2", "arbiter": "c3", "verifier_1": "a1", "verifier_2": "a2", "verifier_3": "a3",
    "provider_a": "d1", "provider_b": "d2", "provider_c": "d3", "provider_p": "d4",
}.items()}
DISPUTE_WINDOW = 10
# Mid-training checks only. From experiments/probation_noise.py: synthetic, honest runs dipped to a lower bound of -0.0275.
# The real model hasn't been measured. Run that script with --real on a GPU, and revisit this.
PROBATION_MARGIN = 0.05
BOND = 1_000_000


def gpu_spec(total_steps: int) -> dict:
    return {
        "schema_version": 1,
        "template": "distilbert-lora-agnews/v1",
        "model": {"id": "distilbert-base-uncased", "revision": "main"},
        "dataset": {"id": "fancyzhx/ag_news", "split": "train", "fingerprint": "0" * 64},
        "train_examples": 20000,
        "lora": {"r": 8, "alpha": 16, "dropout": 0.1, "target_modules": ["q_lin", "v_lin"]},
        "optimizer": {"name": "adamw", "lr": 5e-4, "weight_decay": 0.01, "betas": [0.9, 0.999], "eps": 1e-8, "max_grad_norm": 1.0},
        "lr_schedule": {"name": "linear_warmup_linear_decay", "warmup_steps": max(1, total_steps // 10)},
        "logical_batch_size": 32,
        "total_logical_steps": total_steps,
        "checkpoint_every_steps": 25,
        "max_seq_length": 128,
        "seed": 1337,
    }


def cpu_exec(*arguments: str, env: dict | None = None) -> list[str]:
    """Runs Python for the fallback: on this machine with --no-docker, otherwise in its memory-limited container."""
    if NO_DOCKER:
        return [sys.executable, *arguments]
    # Names only: docker copies the values from this process's environment, so keys never appear in a process list.
    flags = [item for name in (env or {}) for item in ("-e", name)]
    return ["docker", "exec", *flags, CPU_CONTAINER, "python", *arguments]


def cpu_spec(total_steps: int) -> dict:
    code = f"import json\nfrom gpuchain_runtime.synthetic import synthetic_spec\nprint(json.dumps(synthetic_spec(total_steps={total_steps})))"
    spec = json.loads(subprocess.run(cpu_exec("-c", code), capture_output=True, text=True, check=True, env={**os.environ, "PYTHONPATH": HOST_PATH}).stdout)
    spec.update(template="synthetic-linear/v1", checkpoint_every_steps=max(25, total_steps // 30))
    return spec


def start_job_helper(port: int, budget: int) -> None:
    """Behind the dashboard's Start demo job button. Local chain only: it spends the public demo client key."""
    allowed = {f"http://{host}:{p}" for host in ("127.0.0.1", "localhost") for p in (3000, 3100)}
    running: list[subprocess.Popen] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int) -> None:
            self.send_response(status)
            origin = self.headers.get("Origin", "")
            match = next((a for a in allowed if a == origin), None)
            if match:
                self.send_header("Access-Control-Allow-Origin", match)
            self.end_headers()

        def do_POST(self):
            # A plain POST skips the browser's CORS check, so another site's page could send this. The Origin says who it is.
            if self.path != "/start-job" or self.headers.get("Origin", "") not in allowed:
                return self._reply(403)
            if running and running[-1].poll() is None:
                return self._reply(409)
            running.append(subprocess.Popen([sys.executable, str(ROOT / "experiments" / "start_demo_job.py"), "--budget", str(budget)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            return self._reply(202)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def read_env() -> dict:
    values = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip()
    return values


class Stack:
    def __init__(self, args):
        self.args = args
        self.work = Path(tempfile.mkdtemp(prefix="gpuchain_demo_", dir=os.environ.get("GPUCHAIN_RUN_DIR")))
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs = []
        self.base = f"http://127.0.0.1:{args.port}"
        self.env = {**os.environ, "PYTHONPATH": HOST_PATH, "PYTHONDONTWRITEBYTECODE": "1"}
        self.keys = dict(LOCAL)

    def spawn(self, name: str, command: list[str], env: dict | None = None) -> None:
        log = open(self.work / f"{name}.log", "wb")
        self.logs.append(log)
        self.processes[name] = subprocess.Popen(command, env=env or self.env, stdout=log, stderr=subprocess.STDOUT, cwd=self.work)

    def call(self, method: str, path: str, body=None, timeout: float = 900):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")

    def wait(self, check, what: str, timeout: float = 90) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                if check():
                    return
            except Exception as exc:  # Still starting.
                last = exc
            for name, process in self.processes.items():
                if process.poll() is not None:
                    raise SystemExit(f"{name} stopped early. See {self.work / (name + '.log')}")
            time.sleep(0.3)
        raise SystemExit(f"Timed out waiting for {what}: {last}")

    def load_testnet(self) -> dict:
        """Base Sepolia: contracts already deployed, wallets from .env. Nothing here deploys or spends."""
        env = read_env()
        missing = [name for name in ("RPC_URL", "ESCROW_ADDRESS", "USDC_ADDRESS", "COORDINATOR_PRIVATE_KEY", "CLIENT_PRIVATE_KEY") if not env.get(name)]
        if missing:
            raise SystemExit(f".env is missing {missing}. Deploy first (see contracts/README.md)")
        for name in ("coordinator", "client", "arbiter", "verifier_1", "verifier_2", "verifier_3", "provider_a", "provider_b", "provider_c", "provider_p"):
            self.keys[name] = env[f"{name.upper()}_PRIVATE_KEY"]
        w3 = Web3(Web3.HTTPProvider(env["RPC_URL"]))
        chain_id = w3.eth.chain_id
        if chain_id != 84532:
            raise SystemExit(f"expected Base Sepolia (84532), but the RPC reports chain {chain_id}")
        return {
            "rpc": env["RPC_URL"], "chain_id": chain_id, "escrow": env["ESCROW_ADDRESS"], "usdc": env["USDC_ADDRESS"], "explorer": env.get("EXPLORER_URL", ""),
            "verifiers": [Account.from_key(self.keys[f"verifier_{i}"]).address for i in (1, 2, 3)], "client": Account.from_key(self.keys["client"]).address,
        }

    def start_chain(self) -> dict:
        if NO_DOCKER:
            self.spawn("anvil", [str(FOUNDRY_BIN / "anvil"), "--port", str(self.args.chain_port), "--silent"])
            subprocess.run([str(FOUNDRY_BIN / "forge"), "build"], cwd=ROOT / "contracts", check=True, capture_output=True)
        else:
            subprocess.run(["docker", "rm", "-f", "gpuchain-anvil"], capture_output=True, check=False)
            subprocess.run(["docker", "run", "-d", "--rm", "--name", "gpuchain-anvil", "-p", f"127.0.0.1:{self.args.chain_port}:8545", "--memory", "256m", "--entrypoint", "anvil", FOUNDRY, "--host", "0.0.0.0", "--silent"], check=True, capture_output=True)
            subprocess.run(["docker", "run", "--rm", "-v", f"{ROOT}:/repo", "-w", "/repo/contracts", "--entrypoint", "forge", FOUNDRY, "build"], check=True, capture_output=True)
        rpc = f"http://127.0.0.1:{self.args.chain_port}"
        w3 = Web3(Web3.HTTPProvider(rpc))
        self.wait(w3.is_connected, "the local chain", 30)
        for key in LOCAL.values():
            w3.provider.make_request("anvil_setBalance", [Account.from_key(key).address, hex(10**20)])

        def send(call, key):
            account = Account.from_key(key)
            tx = call.build_transaction({"from": account.address, "nonce": w3.eth.get_transaction_count(account.address), "chainId": w3.eth.chain_id})
            return w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction))

        def deploy(name, args_):
            data = json.loads((ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))
            factory = w3.eth.contract(abi=data["abi"], bytecode=data["bytecode"]["object"])
            return w3.eth.contract(address=send(factory.constructor(*args_), ANVIL_KEY)["contractAddress"], abi=data["abi"])

        usdc = deploy("MockUSDC", [])
        verifiers = [Account.from_key(LOCAL[f"verifier_{i}"]).address for i in (1, 2, 3)]
        escrow = deploy("GPUChainEscrow", [usdc.address, Account.from_key(ANVIL_KEY).address, Account.from_key(LOCAL["coordinator"]).address, Account.from_key(LOCAL["arbiter"]).address, verifiers, 2, BOND])
        client = Account.from_key(LOCAL["client"]).address
        send(usdc.functions.mint(client, 10**12), ANVIL_KEY)
        send(usdc.functions.approve(escrow.address, 2**255), LOCAL["client"])
        return {"rpc": rpc, "chain_id": w3.eth.chain_id, "escrow": escrow.address, "usdc": usdc.address, "explorer": "", "verifiers": verifiers, "client": client}

    def start_cpu_container(self) -> None:
        """No GPU and no torch needed on the host. The limits keep the fallback from starving the laptop."""
        subprocess.run(["docker", "rm", "-f", CPU_CONTAINER], capture_output=True, check=False)
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", CPU_CONTAINER, "--memory", "3g", "--memory-swap", "3g", "--cpus", "4", "-p", f"127.0.0.1:{self.args.port}:{self.args.port}",
             "-v", f"{ROOT}:/repo:ro", "-w", "/tmp", "-e", f"PYTHONPATH={CPU_PATH}", "-e", "PYTHONDONTWRITEBYTECODE=1", CPU_IMAGE, "sleep", "infinity"],
            check=True, capture_output=True,
        )

    def docker_base(self, name: str) -> list[str]:
        return [
            "docker", "run", "--rm", "--name", name, "--gpus", "all", "-v", "gpuchain_hf_cache:/hf_cache", "-v", f"{ROOT}:/app:ro",
            "-e", f"PYTHONPATH={CONTAINER_PATH}", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HF_HUB_OFFLINE=1", "-e", "HF_DATASETS_OFFLINE=1", self.args.image,
        ]

    def eval_hash(self, spec: dict) -> str:
        code = f"import json,sys\nfrom gpuchain_verifier.evaluate import load_eval_set\nprint('HASH', load_eval_set(json.loads(sys.argv[1]), {self.args.eval_examples}).hash)\n"
        if self.args.cpu or NO_DOCKER:
            command, env = cpu_exec("-c", code, json.dumps(spec)), self.env if NO_DOCKER else None
        else:
            command, env = [*self.docker_base("gpuchain-evalhash"), "python", "-c", code, json.dumps(spec)], None
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False, env=env)
        for line in result.stdout.splitlines():
            if line.startswith("HASH "):
                return line.split()[1]
        raise SystemExit(f"could not compute the evaluation set hash:\n{result.stderr[-1500:]}")

    def run(self) -> None:
        args = self.args
        if args.cpu and not NO_DOCKER:
            print("starting the CPU container (synthetic workload, 3 GB limit)", flush=True)
            self.start_cpu_container()
        spec = cpu_spec(args.total_steps) if args.cpu else gpu_spec(args.total_steps)
        if args.testnet:
            print("using Base Sepolia, with the contracts and wallets in .env", flush=True)
            chain = self.load_testnet()
        else:
            print("starting local chain and deploying contracts", flush=True)
            chain = self.start_chain()
        settings = {
            "DEMO_MODE": "1", "VERIFIER_ADDRESSES": ",".join(chain["verifiers"]), "VERIFIER_QUORUM": "2", "RPC_URL": chain["rpc"], "CHAIN_ID": str(chain["chain_id"]),
            "ESCROW_ADDRESS": chain["escrow"], "COORDINATOR_PRIVATE_KEY": self.keys["coordinator"], "EXPLORER_URL": chain["explorer"],
        }
        if args.cpu:
            # The dashboard must say this isn't the real model.
            settings["EVENT_PROVENANCE"] = "SYNTHETIC"
        if args.cpu and not NO_DOCKER:
            # Inside the container, the chain is on the host. Clients come through Docker's gateway, so
            # none look local to the API guard. The port is published on 127.0.0.1 only.
            settings.update(OPEN_API="1", DATABASE_URL="sqlite:////tmp/demo.db", ARTIFACT_DIR="/tmp/artifacts", RPC_URL=chain["rpc"].replace("127.0.0.1", "host.docker.internal"))
            self.spawn("coordinator", cpu_exec("-m", "uvicorn", "--factory", "gpuchain_coordinator.main:create_app", "--host", "0.0.0.0", "--port", str(args.port), "--log-level", "warning", env=settings), {**self.env, **settings})
        else:
            settings.update(DATABASE_URL=f"sqlite:///{(self.work / 'demo.db').as_posix()}", ARTIFACT_DIR=str(self.work / "artifacts"))
            # --lan lets agents on other machines connect. The API guard still keeps the operator API to this laptop.
            bind = ["--host", "0.0.0.0"] if args.lan else []
            self.spawn("coordinator", [sys.executable, "-m", "uvicorn", "--factory", "gpuchain_coordinator.main:create_app", *bind, "--port", str(args.port), "--log-level", "warning"], {**self.env, **settings})
        self.wait(lambda: self.call("GET", "/healthz")["status"] == "ok", "the coordinator")

        host = f"http://127.0.0.1:{args.port}" if args.cpu or NO_DOCKER else f"http://host.docker.internal:{args.port}"
        for index in (1, 2):
            # Keys travel in the environment. A command line can be read from any process list.
            key_env = {"VERIFIER_KEY": self.keys[f"verifier_{index}"], "VERIFIER_RPC_URL": chain["rpc"], "VERIFIER_CHAIN_ID": str(chain["chain_id"]), "VERIFIER_ESCROW_ADDRESS": chain["escrow"]}
            if NO_DOCKER:
                key_env["VERIFIER_STATE_DIR"] = str(self.work / f"verifier_state_{index}")
            elif args.cpu:
                key_env["VERIFIER_RPC_URL"] = chain["rpc"].replace("127.0.0.1", "host.docker.internal")
            if args.cpu or NO_DOCKER:
                self.spawn(f"verifier_{index}", cpu_exec("-m", "gpuchain_verifier.worker", "--coordinator", host, "--device", "cpu" if args.cpu else "cuda", "--eval-examples", str(args.eval_examples), env=key_env), {**self.env, **key_env})
            else:
                base = self.docker_base(f"gpuchain-verifier-{index}")
                key_env["VERIFIER_RPC_URL"] = chain["rpc"].replace("127.0.0.1", "host.docker.internal")
                self.spawn(f"verifier_{index}", [*base[:3], *[item for name in key_env for item in ("-e", name)], *base[3:], "python", "-m", "gpuchain_verifier.worker", "--coordinator", host, "--device", "cuda", "--eval-examples", str(args.eval_examples)], {**self.env, **key_env})

        definitions = [
            ("a", "Provider A", "REAL", None, "0.001", None),
            ("p", "Provider P (injected fault)", "INJECTED_FAULT", args.cap_p, "0.002", "POISON"),
            ("b", f"Provider B (capped {args.cap_b} MiB)", "CAPPED_PROFILE", args.cap_b, "0.003", None),
            ("c", f"Provider C (capped {args.cap_c} MiB)", "CAPPED_PROFILE", args.cap_c, "0.0005", None),
        ]
        if args.cpu:
            # A CPU has no GPU allocator, so memory caps can't really be enforced, and showing one refusing
            # or adapting would be fake. The fallback tells the rest of the story: kill, rejection, payout.
            definitions = [
                ("a", "Provider A", "REAL", CPU_DECLARED_MIB, "0.001", None),
                ("p", "Provider P (injected fault)", "INJECTED_FAULT", CPU_DECLARED_MIB, "0.002", "POISON"),
                ("b", "Provider B", "REAL", CPU_DECLARED_MIB, "0.003", None),
            ]
        providers = {}
        for name, label, kind, cap, price, fault in definitions:
            key = self.keys[f"provider_{name}"]
            created = self.call("POST", "/providers", {"name": label, "wallet_address": Account.from_key(key).address, "kind": kind, "capacity_mib": cap, "price_per_step": price})
            providers[name] = created["provider_id"]
            agent = ["-m", "gpuchain_agent.agent", "--coordinator", f"ws://127.0.0.1:{args.port}", "--provider-id", created["provider_id"]]
            faults = ["--fault", fault, "--fault-at-step", "0"] if fault else []
            key_env = {"PROVIDER_WALLET_KEY": key}
            if args.cpu:
                workdir = str(self.work / f"agent_{name}") if NO_DOCKER else f"/tmp/agent_{name}"
                command = cpu_exec(*agent, "--workdir", workdir, "--mode", "process", "--device", "cpu", *faults, env=key_env)
            elif NO_DOCKER:
                # The real GPU with no Docker: every provider runs as a process on this machine, and memory caps are set by the runtime.
                command = [sys.executable, *agent, "--workdir", str(self.work / f"agent_{name}"), "--mode", "process", "--device", "cuda", *faults]
            else:
                command = [sys.executable, *agent, "--workdir", str(self.work / f"agent_{name}"), "--mode", "docker", "--image", args.image, "--repo-root", str(ROOT), "--device", "cuda", *faults]
            self.spawn(f"agent_{name}", command, {**self.env, **key_env})
        for name, provider_id in providers.items():
            self.wait(lambda pid=provider_id: next(p for p in self.call("GET", "/providers") if p["provider_id"] == pid)["status"] == "ONLINE", f"agent {name}")

        print("qualifying providers (about 2 minutes on the GPU)", flush=True)
        for row in self.call("POST", "/providers/qualify", {"workload": spec}):
            outcome = f"{row['exec']['micro_batch']} x {row['exec']['accumulation']}" if row["approved"] else f"refused: {row['code']}"
            print(f"  {row['name']}: {outcome}", flush=True)

        policy = {
            "schema_version": 1, "metric": "accuracy", "min_absolute": 0.88 if not args.cpu else 0.5, "non_inferiority_margin": 0.02, "probation_margin": PROBATION_MARGIN, "confidence": 0.95, "bootstrap_resamples": 2000,
            "eval_set_hash": self.eval_hash(spec), "eval_version_hash": hashlib.sha256((ROOT / "workers/verifier/gpuchain_verifier/evaluate.py").read_bytes()).hexdigest(), "probation_steps": 50,
        }
        # Holds no keys on purpose. start_demo_job.py finds the client's key by itself.
        info = {"testnet": args.testnet, "api": self.base, "ws_job": f"ws://127.0.0.1:{args.port}/ws/jobs/<job_id>", "chain": chain, "providers": providers, "dispute_window": DISPUTE_WINDOW,
                "new_job_body": {"workload": spec, "policy": policy, "budget_usdc": "5", "deadline_seconds": 3600}, "mode": "SYNTHETIC CPU" if args.cpu else "REAL GPU", "logs": str(self.work)}
        info_file = ROOT / "experiments" / "demo_stack_info.json"
        info_file.write_text(json.dumps(info, indent=2), encoding="utf-8")
        if not args.testnet:
            # Every provider must be affordable for the whole job, or the scheduler rightly refuses it.
            start_job_helper(args.port + 1, max(5, math.ceil(args.total_steps * 0.003 * 2)))
        print(f"\nREADY ({info['mode']}). API {self.base}\nDetails for the dashboard, including the job body: {info_file}\nLogs: {self.work}\nCtrl+C stops everything.", flush=True)
        while True:
            for name, process in self.processes.items():
                if process.poll() is not None:
                    print(f"WARNING: {name} stopped. See {self.work / (name + '.log')}", flush=True)
                    self.processes.pop(name)
                    break
            time.sleep(2)

    def stop(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.kill()
        for log in self.logs:
            log.close()
        names = [] if NO_DOCKER else subprocess.run(["docker", "ps", "-q", "--filter", "name=gpuchain-"], capture_output=True, text=True, check=False).stdout.split()
        if names:
            subprocess.run(["docker", "kill", *names], capture_output=True, check=False)
        print("stopped", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cpu", action="store_true", help="Synthetic CPU fallback, in a container built from infra/test-cpu.Dockerfile. No GPU or host torch needed.")
    parser.add_argument("--no-docker", action="store_true", help="Run it all on this machine, with no Docker. With --cpu, synthetic on the CPU. Without it, the real GPU: run with .cache/gpu-venv (CUDA torch). Needs anvil and forge in .cache/foundry.")
    parser.add_argument("--testnet", action="store_true", help="Use Base Sepolia, with the contracts and wallets in .env.")
    parser.add_argument("--lan", action="store_true", help="Let agents on other machines connect. See docs/second-gpu-provider.md.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--chain-port", type=int, default=8545)
    parser.add_argument("--image", default="gpuchain-runtime:spike")
    parser.add_argument("--total-steps", type=int, default=400)
    parser.add_argument("--cap-b", type=int, default=768)
    parser.add_argument("--cap-c", type=int, default=300)
    parser.add_argument("--cap-p", type=int, default=1200)
    parser.add_argument("--eval-examples", type=int, default=2000)
    args = parser.parse_args()
    if args.lan and args.cpu and not args.no_docker:
        parser.error("--lan doesn't work with the CPU container, whose port is only published on this laptop")
    global NO_DOCKER
    NO_DOCKER = args.no_docker
    stack = Stack(args)
    try:
        stack.run()
    except KeyboardInterrupt:
        pass  # Ctrl+C is how the demo is stopped; the stack shuts down in the finally block.
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
