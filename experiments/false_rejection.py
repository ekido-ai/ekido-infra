"""How often does the Quality Guard refuse honest work? CPU and the synthetic workload only: no GPU.

    python experiments/false_rejection.py [--seeds 8] [--steps 6000]

Trains honest runs, then asks the real evaluation code about pairs of their checkpoints, at two
evaluation-set sizes and with the reference near to and far from the candidate.

This measures the synthetic workload. It says nothing direct about DistilBERT on AG News, which
needs its own runs on a GPU. Raw results go to experiments/results/.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in ("packages/gpuchain_core", "workers/runtime", "workers/verifier"):
    sys.path.insert(0, str(ROOT / part))

import torch  # noqa: E402
from gpuchain_runtime.run import run  # noqa: E402
from gpuchain_runtime.synthetic import exec_config, synthetic_spec  # noqa: E402
from gpuchain_verifier.evaluate import evaluate_checkpoint, load_eval_set  # noqa: E402

JOB = "00000000-0000-0000-0000-00000000f001"
EVAL_SIZES = (512, 2000)
MARGIN = 0.02


def spec_for(seed: int, steps: int) -> dict:
    spec = synthetic_spec(total_steps=steps, seed=seed, lr=0.01)
    spec.update(template="synthetic-linear/v1", checkpoint_every_steps=max(25, steps // 30))
    return spec


def policy_for(eval_set) -> dict:
    return {
        "schema_version": 1, "metric": "accuracy", "min_absolute": 0.5, "non_inferiority_margin": MARGIN, "confidence": 0.95,
        "bootstrap_resamples": 2000, "eval_set_hash": eval_set.hash, "eval_version_hash": "e" * 64,
    }


def checkpoints_of(workdir: Path) -> dict[int, Path]:
    found = {}
    for line in (workdir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event["kind"] == "checkpoint.ready":
            found[event["step"]] = Path(event["path"])
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--steps", type=int, default=6000)
    args = parser.parse_args()

    torch.set_num_threads(4)  # Leaves the rest of the laptop usable.
    rows = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(1, args.seeds + 1):
            spec = spec_for(seed, args.steps)
            workdir = Path(tmp) / f"seed_{seed}"
            config = {
                "job_id": JOB, "attempt": 1, "fencing_token": 1, "workdir": str(workdir), "device": "cpu",
                "spec": spec, "exec": exec_config(32), "signer": "0x" + "a" * 40, "progress_seconds": 600,
            }
            code = run(config)
            if code != 0:
                print(f"seed {seed}: training exited with {code}", flush=True)
                return 1
            saved = checkpoints_of(workdir)
            steps = sorted(saved)
            final = steps[-1]
            # Far: an early accepted reference, as in the rejected run. Near: the checkpoint just before.
            early = steps[max(0, len(steps) * 2 // 15)]
            for size in EVAL_SIZES:
                eval_set = load_eval_set(spec, examples=size)
                policy = policy_for(eval_set)
                for gap, reference in (("far", early), ("near", steps[-2])):
                    report = evaluate_checkpoint(
                        job_id=JOB, spec=spec, policy=policy, eval_set=eval_set, checkpoint_dir=saved[final],
                        reference_dir=saved[reference], expected_signer=None, is_final=True,
                    )
                    comparison = report["comparison"]
                    rows.append({
                        "seed": seed, "eval_examples": size, "gap": gap, "reference_step": reference, "candidate_step": final,
                        "verdict": report["verdict"], "reasons": report["reasons"], "candidate_accuracy": report["metrics"]["accuracy"],
                        "reference_accuracy": comparison["reference_accuracy"], "difference": comparison["difference"], "ci_lower": comparison["ci_lower"],
                    })
            print(f"seed {seed} done, {time.monotonic() - started:.0f} s so far", flush=True)

    summary = {}
    for size in EVAL_SIZES:
        for gap in ("far", "near"):
            chosen = [r for r in rows if r["eval_examples"] == size and r["gap"] == gap]
            failed = [r for r in chosen if r["verdict"] != "PASS"]
            summary[f"{size}_examples_{gap}"] = {"runs": len(chosen), "refused": len(failed), "worst_ci_lower": min(r["ci_lower"] for r in chosen)}
    result = {
        "experiment": "false_rejection",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device": "cpu",
        "workload": "synthetic-linear/v1. Not DistilBERT on AG News",
        "torch": torch.__version__,
        "steps": args.steps,
        "margin": MARGIN,
        "seeds": args.seeds,
        "honest_runs_only": True,
        "summary": summary,
        "rows": rows,
    }
    out = ROOT / "experiments" / "results" / f"false_rejection_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    print(f"raw results: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
