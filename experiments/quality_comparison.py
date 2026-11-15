"""Does recovery hurt quality? An uninterrupted run against one resumed with a different micro-batch.

    # The real question, on a GPU. About 40 minutes, so ask before running it on the demo laptop:
    python experiments/quality_comparison.py --seeds 5 --device cuda --identity experiments/identity_agnews.json
    # Checks the script itself, on CPU, in seconds. Says nothing about the real model:
    python experiments/quality_comparison.py --synthetic --seeds 2 --steps 300 --resume-step 150 --eval-examples 512

Uses the product's own runtime, checkpoint format and evaluation code. The baseline completes, then
another process resumes its intermediate checkpoint. No process kill is injected. Dropout stays as the demo job has it, because
that's what is being asked about, so different micro-batches also draw different dropout masks.

A claim is only written at 5+ seeds and 2,000+ examples on the real model, and only ever in the form
"no degradation detected beyond X at this sample size". Raw results go to experiments/results/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATHS = [str(ROOT / p) for p in ("packages/ekido_core", "workers/runtime", "workers/verifier")]
sys.path[:0] = PATHS

import torch  # noqa: E402
from ekido_core.compiler import choose_amp  # noqa: E402
from ekido_core.data_identity import apply_identity  # noqa: E402
from ekido_runtime.synthetic import synthetic_spec  # noqa: E402
from ekido_verifier.evaluate import evaluate_checkpoint, load_eval_set  # noqa: E402

MARGIN = 0.02
JOB = "00000000-0000-0000-0000-00000000c001"
# Two-sided 95% t values, by degrees of freedom. Five seeds is 2.776, not the normal 1.96.
T_95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}


def real_spec(steps: int, seed: int) -> dict:
    """Same as the demo job in demo_stack.py, apart from the length and seed."""
    return {
        "schema_version": 1,
        "template": "distilbert-lora-agnews/v1",
        "model": {"id": "distilbert-base-uncased", "revision": "main"},
        "dataset": {"id": "fancyzhx/ag_news", "split": "train", "fingerprint": "0" * 64},
        "train_examples": 20000,
        "lora": {"r": 8, "alpha": 16, "dropout": 0.1, "target_modules": ["q_lin", "v_lin"]},
        "optimizer": {"name": "adamw", "lr": 5e-4, "weight_decay": 0.01, "betas": [0.9, 0.999], "eps": 1e-8, "max_grad_norm": 1.0},
        "lr_schedule": {"name": "linear_warmup_linear_decay", "warmup_steps": max(1, steps // 10)},
        "logical_batch_size": 32,
        "total_logical_steps": steps,
        "checkpoint_every_steps": 25,
        "max_seq_length": 128,
        "seed": seed,
    }


def test_spec(steps: int, seed: int) -> dict:
    spec = synthetic_spec(total_steps=steps, seed=seed, lr=0.01)
    spec.update(template="synthetic-linear/v1", checkpoint_every_steps=25)
    return spec


def amp_for(device: str) -> str:
    if not device.startswith("cuda"):
        return "none"
    major, minor = torch.cuda.get_device_capability(0)
    return choose_amp(f"{major}.{minor}")[0]


def exec_for(micro: int, batch: int, amp: str) -> dict:
    return {"micro_batch": micro, "accumulation": batch // micro, "amp": amp, "grad_checkpointing": False, "attention_impl": "sdpa", "loader_workers": 0}


def run_attempt(workdir: Path, spec: dict, exec_config: dict, device: str, *, attempt: int, resume_from: Path | None = None, capacity_mib: int | None = None, stop_at_step: int | None = None) -> dict:
    """A separate process each time, so GPU memory starts clean and a resume is a real restart."""
    workdir.mkdir(parents=True, exist_ok=True)
    config = {
        "job_id": JOB, "attempt": attempt, "fencing_token": attempt, "workdir": str(workdir), "device": device, "spec": spec,
        "exec": exec_config, "signer": "0x" + "a" * 40, "progress_seconds": 30, "capacity_mib": capacity_mib,
    }
    if resume_from:
        config["resume_from"] = str(resume_from)
    if stop_at_step:
        config["stop_at_step"] = stop_at_step
    path = workdir / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    started = time.monotonic()
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(PATHS)}
    with open(workdir / "runtime.log", "ab") as log:
        code = subprocess.run([sys.executable, "-m", "ekido_runtime.run", str(path)], env=env, stdout=log, stderr=subprocess.STDOUT, check=False).returncode
    events = [json.loads(line) for line in (workdir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    if code != 0:
        raise SystemExit(f"attempt {attempt} exited with {code}: {events[-1]}. See {workdir / 'runtime.log'}")
    return {
        "seconds": round(time.monotonic() - started, 1),
        "checkpoints": {e["step"]: Path(e["path"]) for e in events if e["kind"] == "checkpoint.ready"},
        "canary": next(({"saved": e["canary_saved"], "measured": e["canary_measured"]} for e in events if e["kind"] == "run.restored"), None),
        "peak_mem_mib": max((e["peak_mem_mib"] for e in events if e["kind"] == "run.progress" and e.get("peak_mem_mib") is not None), default=None),
    }


def seed_statistics(differences: list[float]) -> dict:
    n = len(differences)
    mean = sum(differences) / n
    if n < 2:
        return {"seeds": n, "mean_difference": mean, "ci_95": None, "note": "One seed gives no interval"}
    deviation = math.sqrt(sum((d - mean) ** 2 for d in differences) / (n - 1))
    half = T_95.get(n - 1, 2.0) * deviation / math.sqrt(n)
    return {"seeds": n, "mean_difference": mean, "standard_deviation": deviation, "ci_95": [mean - half, mean + half], "worst_seed": min(differences)}


def claim_from(stats: dict, seeds: int, examples: int, synthetic: bool) -> str | None:
    if synthetic or seeds < 5 or examples < 2000 or not stats.get("ci_95"):
        return None
    low, high = stats["ci_95"]
    interval = f"mean {stats['mean_difference']:+.4f}, 95% CI [{low:+.4f}, {high:+.4f}], {seeds} seeds, {examples} examples"
    if low >= -MARGIN:
        return f"No degradation detected beyond {MARGIN * 100:.0f} accuracy points at this sample size ({interval})."
    return f"Degradation beyond {MARGIN * 100:.0f} accuracy points can't be ruled out at this sample size ({interval})."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--steps", type=int, default=400)
    parser.add_argument("--resume-step", "--kill-step", dest="kill_step", type=int, default=150, help="Checkpoint step to resume; this comparison does not kill a process.")
    parser.add_argument("--micro-base", type=int, default=32)
    parser.add_argument("--micro-adapted", type=int, default=8)
    parser.add_argument("--capacity-adapted", type=int, help="Memory cap in MiB for the resumed run. Real on a GPU.")
    parser.add_argument("--eval-examples", type=int, default=2000)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--synthetic", action="store_true", help="Tiny CPU workload, only to check this script works.")
    parser.add_argument("--identity", type=Path, help="Identity JSON from experiments/prepare_identity.py for a new real run.")
    args = parser.parse_args()
    if not args.synthetic and not args.identity:
        parser.error("real new runs need --identity from experiments/prepare_identity.py")
    if args.synthetic and args.identity:
        parser.error("--identity is only for the real workload")
    identity = json.loads(args.identity.read_text(encoding="utf-8")) if args.identity else None
    if args.kill_step % 25:
        parser.error("--resume-step must be a multiple of 25, where checkpoints exist")

    amp = amp_for(args.device)
    make_spec = test_spec if args.synthetic else real_spec
    trials = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(1, args.seeds + 1):
            spec = make_spec(args.steps, seed)
            if identity is not None:
                spec = apply_identity(spec, identity)
            batch = spec["logical_batch_size"]
            baseline = run_attempt(Path(tmp) / f"baseline_{seed}", spec, exec_for(args.micro_base, batch, amp), args.device, attempt=1)
            resumed = run_attempt(
                Path(tmp) / f"resumed_{seed}", spec, exec_for(args.micro_adapted, batch, amp), args.device, attempt=2,
                resume_from=baseline["checkpoints"][args.kill_step], capacity_mib=args.capacity_adapted,
            )
            eval_set = load_eval_set(spec, examples=args.eval_examples)
            policy = {
                "schema_version": 1, "metric": "accuracy", "min_absolute": 0.0, "non_inferiority_margin": MARGIN, "confidence": 0.95,
                "bootstrap_resamples": 2000, "eval_set_hash": eval_set.hash, "eval_version_hash": "e" * 64,
            }
            report = evaluate_checkpoint(
                job_id=JOB, spec=spec, policy=policy, eval_set=eval_set, checkpoint_dir=resumed["checkpoints"][args.steps],
                reference_dir=baseline["checkpoints"][args.steps], expected_signer=None, is_final=True, device=args.device,
            )
            comparison = report["comparison"]
            trials.append({
                "seed": seed, "baseline_accuracy": comparison["reference_accuracy"], "resumed_accuracy": report["metrics"]["accuracy"], "eval_set_hash": eval_set.hash,
                "difference": comparison["difference"], "paired_ci_lower": comparison["ci_lower"], "quality_guard_verdict": report["verdict"],
                "canary": resumed["canary"], "baseline_seconds": baseline["seconds"], "resumed_seconds": resumed["seconds"],
                "baseline_peak_mib": baseline["peak_mem_mib"], "resumed_peak_mib": resumed["peak_mem_mib"],
            })
            print(f"seed {seed}: baseline {comparison['reference_accuracy']:.4f}, resumed {report['metrics']['accuracy']:.4f}, difference {comparison['difference']:+.4f}", flush=True)

    stats = seed_statistics([t["difference"] for t in trials])
    result = {
        "experiment": "quality_comparison",
        "evidence_type": "SCRIPT CHECK ONLY, synthetic workload" if args.synthetic else "REAL MODEL",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"device": args.device, "gpu": torch.cuda.get_device_name(0) if args.device.startswith("cuda") else None, "torch": torch.__version__, "amp": amp},
        "protocol": {**{k: getattr(args, k.replace("-", "_")) for k in ("steps", "kill_step", "micro_base", "micro_adapted", "capacity_adapted", "eval_examples", "seeds")}, "resume_step": args.kill_step, "actual_process_kill": False},
        "margin": MARGIN,
        "identity": identity,
        "note": "Both arms ran on one device. The baseline completed and the resumed arm started from its intermediate checkpoint; no process was killed. This compares a changed micro-batch, not two kinds of GPU.",
        "statistics": stats,
        "claim": claim_from(stats, args.seeds, args.eval_examples, args.synthetic),
        "trials": trials,
    }
    prefix = "quality_check_synthetic" if args.synthetic else "quality_comparison"
    out = ROOT / "experiments" / "results" / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"statistics": stats, "claim": result["claim"]}, indent=2), flush=True)
    print(f"raw results: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
