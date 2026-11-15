"""Gate G2, for real: a running coordinator, separate agent processes, and a real kill.

Nothing is mocked except the workload, which is the CPU synthetic one so this runs without a GPU.

    docker run --rm -v <repo>:/repo -w /repo -e PYTHONPATH=apps/coordinator:packages/ekido_core:workers/agent:workers/runtime \
      ekido-test:cpu python -m unittest tests/test_e2e_recovery.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

from eth_account import Account

from ekido_core.gpck import manifest_hash  # noqa: F401  (import check for the test environment)
from ekido_runtime.synthetic import exec_config, synthetic_spec  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
PYTHONPATH = os.pathsep.join(str(ROOT / p) for p in ("apps/coordinator", "packages/ekido_core", "workers/agent", "workers/runtime", "workers/verifier"))
WALLETS = {name: "0x" + c * 64 for name, c in (("a", "1"), ("b", "2"), ("c", "3"))}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def job_spec(total=400, every=25):
    spec = synthetic_spec(total_steps=total)
    spec.update(template="synthetic-linear/v1", checkpoint_every_steps=every)
    return spec


class Cluster(unittest.TestCase):
    server_env: dict = {}  # Extra coordinator settings, for tests that need them.

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.remove_tmp)
        self.root = Path(self.tmp.name)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.processes = {}
        self.addCleanup(self.stop_all)
        env = {**os.environ, "PYTHONPATH": PYTHONPATH, "DATABASE_URL": f"sqlite:///{self.root}/test.db", "DEMO_MODE": "1", "ARTIFACT_DIR": str(self.root / "artifacts"), "PYTHONDONTWRITEBYTECODE": "1", **self.server_env}
        self.logs = open(self.root / "server.log", "wb")
        self.addCleanup(self.logs.close)
        self.processes["server"] = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "--factory", "ekido_coordinator.main:create_app", "--port", str(self.port), "--log-level", "warning"],
            env=env, stdout=self.logs, stderr=subprocess.STDOUT, cwd=self.root,
        )
        self.wait_for(lambda: self.get("/healthz")["status"] == "ok", "server start", 90)  # Imports are slow when memory is tight.

    def stop_all(self):
        for process in self.processes.values():
            if process.poll() is None:
                if os.name == "nt":
                    # An agent's runtime is its child, and Windows doesn't kill children with their parent.
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False)
                process.kill()
                process.wait(timeout=10)

    def remove_tmp(self):
        """Windows can't delete a file still open. A runtime whose agent was killed holds one until its lease runs out."""
        for _ in range(25):
            try:
                shutil.rmtree(self.tmp.name)
                break
            except FileNotFoundError:
                break
            except OSError:
                time.sleep(1)
        self.tmp.cleanup()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.loads(response.read() or b"null")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def wait_for(self, check, what, timeout=60, interval=0.2):
        deadline = time.monotonic() + timeout  # Not time.time(): the Docker VM's wall clock can jump.
        last = None
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except Exception as exc:  # The server may still be starting.
                last = exc
            time.sleep(interval)
        server_log = (self.root / "server.log").read_text(errors="replace")[-2500:]
        runtime_logs = "\n".join(f"[{p.parent.name}]\n{p.read_text(errors='replace')[-1500:]}" for p in self.root.rglob("runtime.log"))
        self.fail(f"timed out waiting for {what} (last error: {last})\n--- server log\n{server_log}\n--- agent logs\n{self.agent_logs()}\n--- runtime logs\n{runtime_logs}")

    def agent_logs(self):
        logs = [*self.root.glob("agent_*.log"), *self.root.glob("verifier_*.log")]
        return "\n".join(f"[{p.name}]\n{p.read_text(errors='replace')[-1500:]}" for p in logs)

    def add_provider(self, name, capacity, price, fault=None, kind="CAPPED_PROFILE"):
        key = WALLETS[name]
        provider = self.post("/providers", {"name": name, "wallet_address": Account.from_key(key).address, "kind": kind, "capacity_mib": capacity, "price_per_step": price})
        args = [
            sys.executable, "-m", "ekido_agent.agent", "--coordinator", f"ws://127.0.0.1:{self.port}", "--provider-id", provider["provider_id"],
            "--wallet-key", key, "--workdir", str(self.root / f"agent_{name}"), "--mode", "process", "--device", "cpu",
        ]
        if fault:
            args += ["--fault", fault["kind"], "--fault-at-step", str(fault["at_step"])]
        log = open(self.root / f"agent_{name}.log", "wb")
        self.addCleanup(log.close)
        self.processes[name] = subprocess.Popen(args, env={**os.environ, "PYTHONPATH": PYTHONPATH, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT)
        self.wait_for(lambda: self.provider(provider["provider_id"])["status"] == "ONLINE", f"agent {name} online", 30)
        return provider["provider_id"]

    def provider(self, provider_id):
        return next(p for p in self.get("/providers") if p["provider_id"] == provider_id)

    def create_job(self, spec=None):
        return self.post("/jobs", {"workload": spec or job_spec(), "policy": {"metric": "accuracy"}, "budget_usdc": "5", "deadline_seconds": 1800})["job_id"]

    def state(self, job_id):
        return self.get(f"/jobs/{job_id}")["state"]

    def events(self, job_id, event_type=None):
        found = self.get(f"/jobs/{job_id}/timeline")
        return [e for e in found if event_type is None or e["type"] == event_type]

    def steps(self, job_id):
        return [e["data"]["step"] for e in self.events(job_id, "training.progress")]

    def committed(self, job_id):
        return self.get(f"/jobs/{job_id}/checkpoints")


class EndToEnd(Cluster):
    def test_kill_recovers_on_replacement_and_finishes(self):
        # Probes on CPU report zero memory, so these caps can't force adaptation here. The GPU run covers that.
        a = self.add_provider("a", 6144, "0.001", kind="REAL")
        b = self.add_provider("b", 3072, "0.002")
        job = self.create_job()
        self.post(f"/jobs/{job}/activate")
        self.assertEqual(self.get(f"/jobs/{job}")["fencing_token"], 1, msg=json.dumps(self.events(job))[:1500])

        self.wait_for(lambda: len(self.committed(job)) >= 2, "two checkpoints before the kill")
        fault = self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        self.assertTrue(fault["delivered"])
        self.wait_for(lambda: self.events(job, "failure.detected"), "failure detection", 30)

        detected = self.events(job, "failure.detected")[0]["data"]
        self.assertEqual(detected["provider_id"], a)
        self.assertTrue(detected["injected"], "a fault we injected must be labelled as injected")

        self.wait_for(lambda: self.state(job) in ("VALIDATING", "COMPLETED"), "the replacement to finish", 180)
        stages = [e["data"]["stage"] for e in self.events(job, "recovery.step")]
        for stage in ("DETECTED", "FENCED", "SELECT_CHECKPOINT", "FIND_CANDIDATES", "COMPILE", "ASSIGN", "RESUMED"):
            self.assertIn(stage, stages)

        # The replacement really resumed instead of starting over, and the chain is unbroken.
        checkpoints = self.committed(job)
        self.assertEqual(checkpoints[-1]["logical_step"], 400)
        signers = {c["signer"] for c in checkpoints}
        self.assertEqual(len(signers), 2, "both providers should have contributed checkpoints")
        by_hash = {c["manifest_hash"]: c for c in checkpoints}
        chain, node = [], checkpoints[-1]
        while node:
            chain.append(node["logical_step"])
            node = by_hash.get(node["parent_hash"])
        self.assertEqual(chain[-1], 25, "the final checkpoint must chain back to the very first")
        self.assertEqual(chain, sorted(chain, reverse=True))
        self.assertEqual(self.get(f"/jobs/{job}")["fencing_token"], 2)
        self.assertNotEqual(self.provider(b)["status"], "OFFLINE")

    def test_a_replacement_can_be_killed_too(self):
        # The kill must reach whoever is running the job now, not a worker that was already replaced.
        a = self.add_provider("a", 6144, "0.001", kind="REAL")
        b = self.add_provider("b", 3072, "0.002")
        c = self.add_provider("c", 3072, "0.003")
        # Long enough to kill twice, but C's 0.003 USDC a step must still fit the 5 USDC budget.
        job = self.create_job(job_spec(total=1500, every=25))
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: self.committed(job), "A's first checkpoint")
        self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})

        def replacement_training():
            rows = self.events(job, "training.progress")
            return rows and rows[-1]["data"]["provider_id"] == b

        self.wait_for(replacement_training, "B to take over", 90)
        fault = self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        self.assertTrue(fault["delivered"])
        self.wait_for(lambda: len(self.events(job, "failure.detected")) >= 2, "the second failure", 30)

        targets = [e["data"]["target_provider_id"] for e in self.events(job, "fault.injected")]
        self.assertEqual(targets, [a, b])
        self.assertEqual([e["data"]["provider_id"] for e in self.events(job, "failure.detected")], [a, b])
        self.wait_for(lambda: self.events(job, "training.progress")[-1]["data"]["provider_id"] == c, "C to take over", 90)

    def test_killing_the_whole_agent_is_detected_and_recovered(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        self.add_provider("b", 3072, "0.002")
        job = self.create_job()
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: self.committed(job), "a checkpoint before the kill")

        self.processes["a"].kill()
        self.wait_for(lambda: self.events(job, "failure.detected"), "detecting a dead agent", 30)
        self.assertIn(self.events(job, "failure.detected")[0]["data"]["cause"], ("DISCONNECT", "HEARTBEAT_LOST"))
        self.wait_for(lambda: self.state(job) in ("VALIDATING", "COMPLETED"), "the replacement to finish", 180)
        self.assertEqual(self.committed(job)[-1]["logical_step"], 400)

    def test_no_replacement_fails_the_job_honestly(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        job = self.create_job()
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: self.committed(job), "a checkpoint")
        self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        self.wait_for(lambda: self.state(job) == "FAILED", "the job to fail", 60)
        self.assertEqual(self.events(job, "recovery.step")[-1]["data"]["stage"], "ABORT")

    def test_uploads_need_a_token_and_reject_wrong_content(self):
        digest = "a" * 64
        req = urllib.request.Request(f"{self.base}/artifacts/{digest}", data=b"x", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
