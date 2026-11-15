"""Runs the demo story again and again with no browser, and checks each run on-chain. Needs demo_stack.py running.

    python experiments/rehearse.py --rounds 5

Each round starts a job, kills the active provider at a different point, waits for settlement, and checks
what each wallet earned on the chain against accepted steps and price. Raw results go to experiments/results/.

This finds flaky bugs. It isn't gate G5: that takes people running the whole demo, with the dashboard, 10 times in a row.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
ESCROW_ABI = json.loads((ROOT / "contracts" / "out" / "GPUChainEscrow.sol" / "GPUChainEscrow.json").read_text(encoding="utf-8"))["abi"]
STATES = ("None", "Funded", "Active", "Attested", "Disputed", "Settled", "Refunded", "Cancelled")
FINISHED = {"SETTLED", "FAILED", "REFUNDED", "CANCELLED"}


def timestamp(event: dict) -> float:
    return datetime.fromisoformat(event["ts"]).timestamp()


class Rehearsal:
    def __init__(self, base: str, info: dict, budget: str, timeout: int):
        self.base, self.info, self.budget, self.timeout = base, info, budget, timeout
        w3 = Web3(Web3.HTTPProvider(info["chain"]["rpc"]))
        self.escrow = w3.eth.contract(address=info["chain"]["escrow"], abi=ESCROW_ABI)

    def call(self, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def owed(self, wallet: str) -> int:
        return self.escrow.functions.withdrawable(Web3.to_checksum_address(wallet)).call()

    def wait_for_step(self, job: str, step: int, deadline: float) -> None:
        while time.monotonic() < deadline:
            progress = [e["data"].get("step", 0) for e in self.call(f"/jobs/{job}/timeline") if e["type"] == "training.progress"]
            if max(progress, default=0) >= step:
                return
            time.sleep(1)
        raise TimeoutError(f"training never reached step {step}")

    def round(self, kill_step: int) -> dict:
        providers = self.call("/providers")
        before = {p["name"]: self.owed(p["wallet_address"]) for p in providers}
        started = time.monotonic()
        result: dict = {"kill_step": kill_step, "problems": []}
        launched = subprocess.run([sys.executable, str(ROOT / "experiments" / "start_demo_job.py"), "--budget", self.budget], capture_output=True, text=True, timeout=180)
        if launched.returncode:
            result["problems"].append("couldn't start the job: " + launched.stderr[-300:])
            return result
        job = self.call("/jobs?limit=1")[0]["job_id"]
        result["job_id"] = job
        deadline = started + self.timeout
        self.wait_for_step(job, kill_step, deadline)
        killed_at = time.time()
        result["fault"] = self.call("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"}).get("delivered")

        state, settlement = None, {}
        while time.monotonic() < deadline:
            state = self.call(f"/jobs/{job}")["state"]
            settlement = self.call(f"/jobs/{job}/settlement")
            if state in FINISHED and (state != "SETTLED" or settlement.get("finalize_tx")):
                break
            time.sleep(2)
        result["state"] = state
        result["seconds"] = round(time.monotonic() - started)

        events = self.call(f"/jobs/{job}/timeline")
        detected = [e for e in events if e["type"] == "failure.detected" and timestamp(e) >= killed_at - 1]
        if detected:
            result["kill_to_detected_s"] = round(timestamp(detected[0]) - killed_at, 2)
            result["cause"] = detected[0]["data"].get("cause")
        result["rejected_checkpoints"] = sum(e["type"] == "checkpoint.rejected" for e in events)

        if state != "SETTLED":
            result["problems"].append(f"ended in {state}, not SETTLED")
            return result
        record = self.call(f"/jobs/{job}")
        onchain = self.escrow.functions.getJob(Web3.to_bytes(hexstr=record["onchain_id"])).call()
        if STATES[onchain[2]] != "Settled":
            result["problems"].append(f"the chain says {STATES[onchain[2]]}")

        steps = self.call(f"/jobs/{job}/settlement-preview")["steps"]
        result["steps"] = steps
        earned = {p["name"]: self.owed(p["wallet_address"]) - before[p["name"]] for p in providers}
        result["earned_usdc"] = {name: value / 1e6 for name, value in earned.items()}
        for provider in providers:
            name = provider["name"]
            expected = round(float(provider["price_per_step"]) * 1_000_000) * steps.get(name, 0)
            if earned[name] != expected:
                result["problems"].append(f"{name} earned {earned[name]}, expected {expected}")
            if provider["kind"] == "INJECTED_FAULT" and earned[name]:
                result["problems"].append(f"{name} cheated, and was paid")
        total_steps = record["spec"]["total_logical_steps"]
        if sum(steps.values()) != total_steps:
            result["problems"].append(f"accepted steps add up to {sum(steps.values())}, not {total_steps}")
        if not result["rejected_checkpoints"]:
            result["problems"].append("the injected-fault provider wasn't rejected")
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--budget", default="36")
    parser.add_argument("--timeout", type=int, default=600, help="Seconds allowed for each round.")
    args = parser.parse_args()
    info = json.loads((ROOT / "experiments" / "demo_stack_info.json").read_text(encoding="utf-8"))
    if info["testnet"]:
        raise SystemExit("This spends tokens on every round, so it only runs on the local chain.")
    total = info["new_job_body"]["workload"]["total_logical_steps"]
    every = info["new_job_body"]["workload"]["checkpoint_every_steps"]
    rehearsal = Rehearsal(info["api"], info, args.budget, args.timeout)

    rows = []
    out = ROOT / "experiments" / "results" / f"rehearsal_{datetime.now():%Y%m%d_%H%M%S}.json"
    for index in range(args.rounds):
        # Different kill points, some just after a checkpoint and some just before one.
        kill_step = every * (2 + index % 4) + (every // 2 if index % 2 else every // 8)
        kill_step = min(kill_step, total // 2)
        try:
            row = rehearsal.round(kill_step)
        except Exception as exc:  # A failed round is a result. Keep going, and record it.
            row = {"kill_step": kill_step, "problems": [f"{type(exc).__name__}: {exc}"]}
        rows.append(row)
        print(f"round {index + 1}: kill at {kill_step}, {row.get('state')}, {row.get('seconds')} s, detected in {row.get('kill_to_detected_s')} s, paid {row.get('earned_usdc')}, problems: {row['problems']}", flush=True)
        report = {"mode": info["mode"], "chain": "local Anvil", "what": "Automated, no dashboard. Not gate G5.", "rounds": len(rows), "clean": sum(not r["problems"] for r in rows), "rows": rows}
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n{report['clean']} of {report['rounds']} clean. Raw results: {out}")
    sys.exit(0 if report["clean"] == report["rounds"] else 1)


if __name__ == "__main__":
    main()
