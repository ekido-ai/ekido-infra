"""How far does honest training dip between checkpoints? This sets the margin for mid-training quality checks.

    python experiments/probation_noise.py [--seeds 8]             # Synthetic, CPU. Says nothing about the real model.
    python experiments/probation_noise.py --real --seeds 3 --device cuda --identity experiments/identity_agnews.json

Trains honest runs, then evaluates each checkpoint against the one before it, as a probation check does.
On 5 Nov, a rehearsal refused an honest provider this way: accuracy went from 96.6% to 95.05%, one checkpoint apart.
Raw results go to experiments/results/.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for part in ("packages/gpuchain_core", "workers/runtime", "workers/verifier", "experiments"):
    sys.path.insert(0, str(ROOT / part))

import torch  # noqa: E402
from gpuchain_runtime.run import run  # noqa: E402
from gpuchain_core.data_identity import apply_identity  # noqa: E402
from gpuchain_runtime.synthetic import exec_config, synthetic_spec  # noqa: E402
from gpuchain_verifier.evaluate import evaluate_checkpoint, load_eval_set  # noqa: E402

JOB = "00000000-0000-0000-0000-00000000f002"
MARGINS = (0.02, 0.03, 0.05, 0.08, 0.10, 0.15)


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
    parser.add_argument("--steps", type=int, default=6000, help="Synthetic only. The real model uses the demo's 400.")
    parser.add_argument("--eval-examples", type=int, default=2000)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--micro-batch", type=int, default=32)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--identity", type=Path, help="Identity JSON from experiments/prepare_identity.py for a new real run.")
    args = parser.parse_args()
    if args.real and not args.identity:
        parser.error("real new runs need --identity from experiments/prepare_identity.py")
    if not args.real and args.identity:
        parser.error("--identity is only for the real workload")
    if args.seeds < 1 or args.eval_examples < 1:
        parser.error("--seeds and --eval-examples must be positive")
    if args.device.startswith("cuda") and not torch.cuda.is_available():
        parser.error("CUDA was requested but no CUDA GPU is available")
    identity = json.loads(args.identity.read_text(encoding="utf-8")) if args.identity else None

    torch.set_num_threads(4)  # Leaves the rest of the laptop usable.
    rows = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        for seed in range(1, args.seeds + 1):
            if args.real:
                from quality_comparison import amp_for, exec_for, real_spec

                spec = real_spec(400, seed)
                spec = apply_identity(spec, identity)
                run_exec = exec_for(args.micro_batch, spec["logical_batch_size"], amp_for(args.device))
            else:
                # The same workload the CPU demo runs.
                spec = synthetic_spec(total_steps=args.steps, seed=seed)
                spec.update(template="synthetic-linear/v1", checkpoint_every_steps=max(25, args.steps // 30))
                run_exec = exec_config(args.micro_batch)
            workdir = Path(tmp) / f"seed_{seed}"
            code = run({"job_id": JOB, "attempt": 1, "fencing_token": 1, "workdir": str(workdir), "device": args.device, "spec": spec, "exec": run_exec, "signer": "0x" + "a" * 40, "progress_seconds": 600})
            if code != 0:
                print(f"seed {seed}: training exited with {code}", flush=True)
                return 1
            saved = checkpoints_of(workdir)
            steps = sorted(saved)
            eval_set = load_eval_set(spec, examples=args.eval_examples)
            policy = {
                "schema_version": 1, "metric": "accuracy", "min_absolute": 0.0, "non_inferiority_margin": 0.02, "confidence": 0.95,
                "bootstrap_resamples": 2000, "eval_set_hash": eval_set.hash, "eval_version_hash": "e" * 64,
            }
            for reference, candidate in zip(steps, steps[1:]):
                report = evaluate_checkpoint(job_id=JOB, spec=spec, policy=policy, eval_set=eval_set, checkpoint_dir=saved[candidate], reference_dir=saved[reference], expected_signer=None, is_final=False, device=args.device)
                comparison = report["comparison"]
                rows.append({"seed": seed, "reference_step": reference, "candidate_step": candidate, "eval_set_hash": eval_set.hash, "reference_accuracy": comparison["reference_accuracy"], "candidate_accuracy": report["metrics"]["accuracy"], "difference": comparison["difference"], "ci_lower": comparison["ci_lower"]})
            print(f"seed {seed} done, {time.monotonic() - started:.0f} s so far", flush=True)

    total_seeds = len({r["seed"] for r in rows})
    if not rows:
        raise RuntimeError("no checkpoint pairs were produced; no result can be reported")
    by_margin = {}
    for margin in MARGINS:
        refused = [r for r in rows if r["ci_lower"] < -margin]
        by_margin[str(margin)] = {
            "pairs_refused": len(refused),
            "of_pairs": len(rows),
            "seeds_with_any_refusal": len({r["seed"] for r in refused}),
            "of_seeds": total_seeds,
            "refused_at_steps": sorted({r["candidate_step"] for r in refused})[:12],
        }
    worst = min(rows, key=lambda r: r["ci_lower"])
    result = {
        "experiment": "probation_noise",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "device": args.device,
        "workload": "distilbert-lora-agnews, the real model" if args.real else "synthetic-linear/v1. Not DistilBERT on AG News",
        "torch": torch.__version__,
        "environment": {"python": platform.python_version(), "os": platform.platform(), "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(args.device) if args.device.startswith("cuda") else None, "compute_capability": list(torch.cuda.get_device_capability(args.device)) if args.device.startswith("cuda") else None, "memory_mib": torch.cuda.get_device_properties(args.device).total_memory // 2**20 if args.device.startswith("cuda") else None},
        "identity": identity,
        "eval_examples": args.eval_examples,
        "seeds": total_seeds,
        "honest_runs_only": True,
        "by_margin": by_margin,
        "worst": worst,
        "rows": rows,
    }
    label = "real" if args.real else "synthetic"
    out = ROOT / "experiments" / "results" / f"probation_noise_{label}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(by_margin, indent=1), flush=True)
    print("worst pair:", json.dumps(worst), flush=True)
    print(f"raw results: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
