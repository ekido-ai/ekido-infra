"""Runs every injectable fault against a running demo stack, and checks what actually happened.

    python experiments/demo_stack.py --total-steps 300      (in another terminal, wait for READY)
    python experiments/fault_matrix.py

Each fault gets its own funded job on the chain. Results are saved after every fault, to
experiments/results/. KILL_AGENT runs last, because it permanently removes that provider's agent.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"
FAULTS = ["THERMAL_WARNING", "OOM", "CRASH_MID_CHECKPOINT", "CORRUPT_CHECKPOINT", "DISCONNECT", "HEARTBEAT_LOSS", "KILL_CONTAINER", "KILL_AGENT"]
# What must be seen for the fault to count as handled. None means the job shouldn't fail over at all.
EXPECTED_CAUSE = {
    "THERMAL_WARNING": None,
    "OOM": {"OOM"},
    "CRASH_MID_CHECKPOINT": {"PROCESS_EXIT"},
    "CORRUPT_CHECKPOINT": {"BAD_CHECKPOINT"},
    "DISCONNECT": {"DISCONNECT"},
    "HEARTBEAT_LOSS": {"HEARTBEAT_LOST"},
    "KILL_CONTAINER": {"PROCESS_EXIT"},
    "KILL_AGENT": {"DISCONNECT", "HEARTBEAT_LOST"},
}


def call(method: str, path: str, body=None, timeout: float = 30):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"null")


def wait(check, timeout: float, every: float = 1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (urllib.error.URLError, OSError, ValueError):
            pass  # Not ready yet: try again until the deadline.
        time.sleep(every)
    return None


def running_containers() -> list[str]:
    out = subprocess.run(["docker", "ps", "--filter", "name=gpuchain-run-", "--format", "{{.Names}}"], capture_output=True, text=True, check=False).stdout
    return out.split()


def run_fault(kind: str, kill_at: int, python: str) -> dict:
    result: dict = {"fault": kind, "problems": []}
    started = subprocess.run([python, str(ROOT / "experiments" / "start_demo_job.py")], capture_output=True, text=True, timeout=300, check=False)
    if started.returncode != 0:
        result["problems"].append(f"job didn't start: {started.stderr[-300:]}")
        return result
    job = call("GET", "/jobs?limit=1")[0]["job_id"]
    result["job_id"] = job

    def events(kind_=None):
        found = call("GET", f"/jobs/{job}/timeline")
        return [e for e in found if kind_ is None or e["type"] == kind_]

    def progress():
        rows = events("training.progress")
        return max((e["data"].get("step") or 0 for e in rows), default=0)

    if not wait(lambda: progress() >= kill_at, 300):
        result["problems"].append(f"never reached step {kill_at}")
        return result
    t0 = time.monotonic()
    response = call("POST", "/demo/faults", {"job_id": job, "kind": kind})
    result["delivered"] = response.get("delivered")
    if not response.get("delivered"):
        result["problems"].append("fault wasn't delivered")

    final = wait(lambda: (lambda s: s if s in ("SETTLED", "REFUNDED") else None)(call("GET", f"/jobs/{job}")["state"]), 720, 3)
    result["seconds_after_fault"] = round(time.monotonic() - t0, 1)
    state = call("GET", f"/jobs/{job}")
    result["state"] = state["state"]
    result["attempts"] = state["attempt"]
    all_events = events()
    failures = [e["data"] for e in all_events if e["type"] == "failure.detected"]
    result["causes"] = [f["cause"] for f in failures]
    result["warnings"] = [e["data"].get("rule") for e in all_events if e["type"] == "failure.warning"]
    result["rejected_steps"] = [e["data"]["step"] for e in all_events if e["type"] == "checkpoint.rejected"]
    result["fault_events"] = [e["data"].get("fault_kind") for e in all_events if e["type"] == "fault.injected"]
    preview = call("GET", f"/jobs/{job}/settlement-preview")
    result["steps"] = preview.get("steps")
    result["payouts"] = preview.get("payouts")
    result["refund"] = preview.get("refund")
    settlement = call("GET", f"/jobs/{job}/settlement")
    result["settlement"] = settlement.get("status")
    ids = [e["id"] for e in all_events]
    first_detect = next((e for e in all_events if e["type"] == "failure.detected"), None)
    fault_event = next((e for e in all_events if e["type"] == "fault.injected"), None)
    if first_detect and fault_event:
        result["seconds_to_detect"] = round((datetime.fromisoformat(first_detect["ts"]) - datetime.fromisoformat(fault_event["ts"])).total_seconds(), 2)
    resumed = next((e for e in all_events if e["type"] == "recovery.step" and e["data"]["stage"] == "RESUMED"), None)
    if resumed and fault_event:
        result["seconds_to_first_resume"] = round((datetime.fromisoformat(resumed["ts"]) - datetime.fromisoformat(fault_event["ts"])).total_seconds(), 1)

    # Checks that hold for every fault.
    if not final:
        result["problems"].append(f"didn't finish within the time limit: state {state['state']}")
    if final == "SETTLED":
        total = call("GET", f"/jobs/{job}")["spec"]["total_logical_steps"]
        if sum((preview.get("steps") or {}).values()) != total:
            result["problems"].append(f"accepted steps {preview.get('steps')} don't add up to {total}")
        if any("injected fault" in name.lower() for name, amount in (preview.get("payouts") or {}).items() if float(amount) > 0):
            result["problems"].append(f"the cheating provider was paid: {preview.get('payouts')}")
        if settlement.get("status") != "SETTLED":
            result["problems"].append(f"settlement status {settlement.get('status')}")
    if len(ids) != len(set(ids)):
        result["problems"].append("duplicate event numbers")

    expected = EXPECTED_CAUSE[kind]
    if expected is None and kind == "THERMAL_WARNING":
        if "THERMAL" not in result["warnings"]:
            result["problems"].append("no thermal warning reached the event stream")
        if result["causes"]:
            result["problems"].append(f"a warning shouldn't cause a failover: {result['causes']}")
    elif expected is not None and not (set(result["causes"][:1]) & expected):
        result["problems"].append(f"first failure was {result['causes'][:1]}, expected one of {sorted(expected)}")

    # Nothing may be left holding the GPU.
    time.sleep(3)
    leftovers = running_containers()
    if leftovers and not wait(lambda: not running_containers(), 25):
        result["problems"].append(f"containers still running: {running_containers()}")
    result["passed"] = not result["problems"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--faults", nargs="*", default=FAULTS)
    parser.add_argument("--kill-at-step", type=int, default=100)
    args = parser.parse_args()
    out = ROOT / "experiments" / "results" / f"fault_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    results = []
    for kind in args.faults:
        wait(lambda: all(p["status"] == "ONLINE" for p in call("GET", "/providers") if "Provider A" in p["name"]) or kind == "KILL_AGENT", 60)
        wait(lambda: not running_containers(), 40)
        print(f"--- {kind}", flush=True)
        try:
            result = run_fault(kind, args.kill_at_step, sys.executable)
        except Exception as exc:  # One broken scenario shouldn't lose the others' results.
            result = {"fault": kind, "passed": False, "problems": [f"{type(exc).__name__}: {exc}"]}
        results.append(result)
        out.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(json.dumps({k: result.get(k) for k in ("passed", "state", "causes", "warnings", "rejected_steps", "attempts", "seconds_to_detect", "seconds_to_first_resume", "seconds_after_fault", "steps", "refund", "problems")}), flush=True)
    passed = sum(1 for r in results if r.get("passed"))
    print(f"\n{passed} of {len(results)} faults handled. Raw results: {out}", flush=True)


if __name__ == "__main__":
    main()
