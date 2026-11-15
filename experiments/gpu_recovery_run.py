"""The whole story on the real GPU: kill, a cheating replacement, rejection, adaptation, payout.

Run from the repo root on the host, in the project venv:
    python experiments/gpu_recovery_run.py

A trains and gets a real `docker kill`. P, an injected-fault provider, resumes and poisons its
weights. Verifiers reject it, and B finishes under a memory cap. C is too small and must be refused.
All of them share one physical GPU, so this shows memory adaptation, not a different GPU model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parents[1]
HOST_PATH = os.pathsep.join(str(ROOT / p) for p in ("apps/coordinator", "packages/ekido_core", "workers/agent", "workers/runtime"))
WALLETS = {name: "0x" + c * 64 for name, c in (("a", "1"), ("b", "2"), ("c", "3"), ("p", "4"))}
VERIFIER_KEYS = ["0x" + c * 64 for c in ("a", "b")]
CONTAINER_PATH = "/app/packages/ekido_core:/app/workers/runtime:/app/workers/verifier"


class Client:
    def __init__(self, base: str):
        self.base = base

    def call(self, method: str, path: str, body=None, timeout: float = 900):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"null")

    def get(self, path: str):
        return self.call("GET", path)

    def post(self, path: str, body=None):
        return self.call("POST", path, body if body is not None else {})


def job_spec(args) -> dict:
    return {
        "schema_version": 1,
        "template": "distilbert-lora-agnews/v1",
        "model": {"id": "distilbert-base-uncased", "revision": args.revision},
        "dataset": {"id": "fancyzhx/ag_news", "split": "train", "fingerprint": "0" * 64},
        "train_examples": 20000,
        "lora": {"r": 8, "alpha": 16, "dropout": 0.1, "target_modules": ["q_lin", "v_lin"]},
        "optimizer": {"name": "adamw", "lr": 5e-4, "weight_decay": 0.01, "betas": [0.9, 0.999], "eps": 1e-8, "max_grad_norm": 1.0},
        "lr_schedule": {"name": "linear_warmup_linear_decay", "warmup_steps": max(1, args.total_steps // 10)},
        "logical_batch_size": 32,
        "total_logical_steps": args.total_steps,
        "checkpoint_every_steps": args.checkpoint_every,
        "max_seq_length": 128,
        "seed": args.seed,
    }


def docker_base(args, name: str) -> list[str]:
    return [
        "docker", "run", "--rm", "--name", name, "--gpus", "all",
        "--label", f"gpuchain.story_owner={args.run_id}",
        "-v", "gpuchain_hf_cache:/hf_cache", "-v", f"{ROOT}:/app:ro",
        "-e", f"PYTHONPATH={CONTAINER_PATH}", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HF_HUB_OFFLINE=1", "-e", "HF_DATASETS_OFFLINE=1",
        args.image,
    ]


def eval_set_hash(args, spec: dict) -> str:
    code = (
        "import json,sys\n"
        "from ekido_verifier.evaluate import load_eval_set\n"
        f"print('HASH', load_eval_set(json.loads(sys.argv[1]), {args.eval_examples}).hash)\n"
    )
    result = subprocess.run([*docker_base(args, f"ekido-evalhash-{args.run_id}"), "python", "-c", code, json.dumps(spec)], capture_output=True, text=True, timeout=300, check=False)
    for line in result.stdout.splitlines():
        if line.startswith("HASH "):
            return line.split()[1]
    raise SystemExit(f"could not compute the evaluation set hash:\n{result.stderr[-1500:]}")


def wait_for(check, what: str, timeout: float, processes: dict):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as exc:  # The server may still be starting.
            last = exc
        for name, process in processes.items():
            if process.poll() is not None:
                raise SystemExit(f"{name} exited early with code {process.returncode} while waiting for {what}")
        time.sleep(0.25)
    raise SystemExit(f"Timed out waiting for {what} (last error: {last})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total-steps", type=int, default=400)
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--kill-at-step", type=int, default=150)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--revision", default="main")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--image", default="ekido-runtime:spike")
    parser.add_argument("--cap-b", type=int, default=768)
    parser.add_argument("--cap-c", type=int, default=300)
    parser.add_argument("--cap-p", type=int, default=1200)
    parser.add_argument("--eval-examples", type=int, default=2000)
    args = parser.parse_args()
    args.run_id = secrets.token_hex(8)

    work = Path(tempfile.mkdtemp(prefix="gpuchain_run_", dir=os.environ.get("EKIDO_RUN_DIR")))
    client = Client(f"http://127.0.0.1:{args.port}")
    processes: dict[str, subprocess.Popen] = {}
    logs = []
    base_env = {**os.environ, "PYTHONPATH": HOST_PATH, "PYTHONDONTWRITEBYTECODE": "1"}

    def spawn(name: str, command: list[str], env: dict) -> None:
        log = open(work / f"{name}.log", "wb")
        logs.append(log)
        processes[name] = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=work)

    report: dict = {"started_at": datetime.now().isoformat(), "args": vars(args), "work_dir": str(work)}
    out_file = ROOT / "experiments" / "results" / f"gpu_story_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    try:
        spec = job_spec(args)
        print("computing the evaluation set hash", flush=True)
        policy = {
            "schema_version": 1,
            "metric": "accuracy",
            "min_absolute": 0.88,
            "non_inferiority_margin": 0.02,
            "probation_margin": 0.05,  # See PROBATION_MARGIN in demo_stack.py.
            "confidence": 0.95,
            "bootstrap_resamples": 2000,
            "eval_set_hash": eval_set_hash(args, spec),
            "eval_version_hash": hashlib.sha256((ROOT / "workers/verifier/ekido_verifier/evaluate.py").read_bytes()).hexdigest(),
            "probation_steps": 50,
        }
        verifiers = ",".join(Account.from_key(k).address for k in VERIFIER_KEYS)
        spawn(
            "coordinator",
            [sys.executable, "-m", "uvicorn", "--factory", "ekido_coordinator.main:create_app", "--port", str(args.port), "--log-level", "warning"],
            {**base_env, "DATABASE_URL": f"sqlite:///{(work / 'run.db').as_posix()}", "DEMO_MODE": "1", "ARTIFACT_DIR": str(work / "artifacts"), "VERIFIER_ADDRESSES": verifiers, "VERIFIER_QUORUM": "2"},
        )
        wait_for(lambda: client.get("/healthz")["status"] == "ok", "the coordinator", 60, processes)

        for index, key in enumerate(VERIFIER_KEYS):
            spawn(
                f"verifier_{index + 1}",
                [*docker_base(args, f"ekido-verifier-{args.run_id}-{index + 1}"), "python", "-m", "ekido_verifier.worker", "--coordinator", f"http://host.docker.internal:{args.port}", "--key", key, "--device", "cuda", "--eval-examples", str(args.eval_examples)],
                base_env,
            )

        definitions = [
            ("a", "A full 6 GB", "REAL", None, "0.001", None),
            ("p", "P injected fault", "INJECTED_FAULT", args.cap_p, "0.002", "POISON"),
            ("b", f"B capped {args.cap_b} MiB", "CAPPED_PROFILE", args.cap_b, "0.003", None),
            ("c", f"C capped {args.cap_c} MiB", "CAPPED_PROFILE", args.cap_c, "0.0005", None),
        ]
        providers = {}
        for name, label, kind, cap, price, fault in definitions:
            created = client.post("/providers", {"name": label, "wallet_address": Account.from_key(WALLETS[name]).address, "kind": kind, "capacity_mib": cap, "price_per_step": price})
            providers[name] = created["provider_id"]
            command = [sys.executable, "-m", "ekido_agent.agent", "--coordinator", f"ws://127.0.0.1:{args.port}", "--provider-id", created["provider_id"], "--wallet-key", WALLETS[name],
                       "--workdir", str(work / f"agent_{name}"), "--mode", "docker", "--image", args.image, "--repo-root", str(ROOT), "--device", "cuda"]
            if fault:
                command += ["--fault", fault, "--fault-at-step", "0"]
            spawn(f"agent_{name}", command, base_env)
        for name, provider_id in providers.items():
            wait_for(lambda pid=provider_id: next(p for p in client.get("/providers") if p["provider_id"] == pid)["status"] == "ONLINE", f"agent {name}", 60, processes)
        report["providers"] = providers

        t0 = time.time()
        report["qualification"] = client.post("/providers/qualify", {"workload": spec})
        report["qualification_seconds"] = round(time.time() - t0, 2)
        print(f"qualified in {report['qualification_seconds']}s", flush=True)
        for row in report["qualification"]:
            print(f"  {row['name']}: {'OK ' + str(row['exec']['micro_batch']) + 'x' + str(row['exec']['accumulation']) if row['approved'] else 'REFUSED ' + str(row['code'])} peak={row['peak_mem_mib']} steps/s={row['steps_per_s']} probes={row['probes']}", flush=True)

        job = client.post("/jobs", {"workload": spec, "policy": policy, "budget_usdc": "5", "deadline_seconds": 3600})["job_id"]
        report["job_id"] = job
        t0 = time.time()
        client.post(f"/jobs/{job}/activate")
        report["placement_seconds"] = round(time.time() - t0, 2)
        print(f"placed in {report['placement_seconds']}s", flush=True)

        def state():
            return client.get(f"/jobs/{job}")

        def events(kind=None):
            found = client.get(f"/jobs/{job}/timeline")
            return [e for e in found if kind is None or e["type"] == kind]

        def last_step():
            steps = [e["data"].get("step") or 0 for e in events("training.progress")]
            return max(steps) if steps else 0

        wait_for(lambda: last_step() >= args.kill_at_step, f"step {args.kill_at_step}", 900, processes)
        kill_time = time.time()
        report["fault_response"] = client.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        print(f"killed provider A at about step {last_step()}", flush=True)

        wait_for(lambda: state()["attempt"] >= 2 and state()["state"] == "RUNNING", "the replacement to start", 600, processes)
        report["seconds_kill_to_replacement_running"] = round(time.time() - kill_time, 2)
        print(f"replacement running after {report['seconds_kill_to_replacement_running']}s", flush=True)

        wait_for(lambda: events("checkpoint.rejected"), "the Quality Guard to reject the poisoned candidate", 600, processes)
        report["seconds_kill_to_rejection"] = round(time.time() - kill_time, 2)
        print(f"poisoned candidate rejected after {report['seconds_kill_to_rejection']}s", flush=True)

        wait_for(lambda: state()["state"] in ("COMPLETED", "FAILED"), "the job to finish", 1800, processes)
        report["seconds_total_after_kill"] = round(time.time() - kill_time, 2)
    except SystemExit as exc:
        report["error"] = str(exc)
        print("ERROR:", exc, flush=True)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("ERROR:", report["error"], flush=True)
    finally:
        report["finished_at"] = datetime.now().isoformat()
        try:
            if "job_id" in report:
                job = report["job_id"]
                report["final_state"] = client.get(f"/jobs/{job}")
                report["checkpoints"] = client.get(f"/jobs/{job}/checkpoints")
                report["recoveries"] = client.get(f"/jobs/{job}/recoveries")
                report["settlement_preview"] = client.get(f"/jobs/{job}/settlement-preview")
                report["events"] = client.get(f"/jobs/{job}/timeline")
                print("final:", report["final_state"]["state"], flush=True)
                print("settlement:", json.dumps(report["settlement_preview"].get("steps")), json.dumps(report["settlement_preview"].get("payouts")), "refund", report["settlement_preview"].get("refund"), flush=True)
        except Exception as exc:
            print("could not read final state:", exc, flush=True)
        out_file.write_text(json.dumps(report, indent=1), encoding="utf-8")
        print("wrote", out_file, flush=True)
        for process in processes.values():
            if process.poll() is None:
                process.kill()
        for log in logs:
            log.close()
        # Never kill other agents' containers. Inspect the owner label before stopping
        # the uniquely named helper containers started by this experiment.
        for name in [f"ekido-evalhash-{args.run_id}", *(f"ekido-verifier-{args.run_id}-{i}" for i in (1, 2))]:
            owner = subprocess.run(["docker", "inspect", "--format", '{{index .Config.Labels "gpuchain.story_owner"}}', name], capture_output=True, text=True, check=False)
            if owner.returncode == 0 and owner.stdout.strip() == args.run_id:
                subprocess.run(["docker", "kill", name], capture_output=True, check=False)
        print("logs in", work, flush=True)


if __name__ == "__main__":
    main()
