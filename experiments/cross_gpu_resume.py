"""One leg of the cross-GPU test: restore a checkpoint here, keep training, save, and record the evidence.

    # 1. On the 3060: train to a checkpoint, and write the job's spec next to it.
    python experiments/cross_gpu_resume.py start --out handover --steps 150 --identity experiments/identity_agnews.json
    # 2. On the other GPU (a teammate's laptop, or infra/cross_gpu_notebook.ipynb on a T4):
    python experiments/cross_gpu_resume.py resume --checkpoint handover --out from_other_gpu --steps 50 --micro-batch 8
    # 3. Back on the 3060:
    python experiments/cross_gpu_resume.py resume --checkpoint from_other_gpu --out back_home --steps 50 --micro-batch 16

Each leg writes evidence.json with what this machine really is. Bring all three back to experiments/results/.
It proves a checkpoint moved between these GPUs and kept working. It doesn't prove the runs are equivalent.
Add --synthetic to check the script on CPU. That result is labelled and is not evidence.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

from quality_comparison import MARGIN, amp_for, exec_for, real_spec, run_attempt, test_spec  # noqa: E402
import torch  # noqa: E402
from ekido_core.gpck import MANIFEST_FILE  # noqa: E402
from ekido_core.data_identity import apply_identity  # noqa: E402
from ekido_verifier.evaluate import evaluate_checkpoint, load_eval_set  # noqa: E402

JOB = "00000000-0000-0000-0000-00000000c001"
SPEC_FILE = "spec.json"


def machine(device: str) -> dict:
    info = {"device": device, "torch": torch.__version__, "python": platform.python_version(), "os": platform.platform()}
    if device.startswith("cuda"):
        major, minor = torch.cuda.get_device_capability(0)
        info.update(gpu=torch.cuda.get_device_name(0), compute_capability=f"{major}.{minor}", memory_mib=torch.cuda.get_device_properties(0).total_memory // 2**20, cuda=torch.version.cuda)
    return info


def accuracy(spec: dict, checkpoint: Path, examples: int, device: str) -> tuple[float, str]:
    eval_set = load_eval_set(spec, examples=examples)
    policy = {
        "schema_version": 1, "metric": "accuracy", "min_absolute": 0.0, "non_inferiority_margin": MARGIN, "confidence": 0.95,
        "bootstrap_resamples": 200, "eval_set_hash": eval_set.hash, "eval_version_hash": "e" * 64,
    }
    report = evaluate_checkpoint(job_id=JOB, spec=spec, policy=policy, eval_set=eval_set, checkpoint_dir=checkpoint, reference_dir=None, expected_signer=None, is_final=False, device=device)
    return report["metrics"]["accuracy"], eval_set.hash


def save_leg(out: Path, checkpoint: Path, spec: dict, evidence: dict) -> None:
    """Keeps only the newest checkpoint, so the folder is small enough to carry to the next GPU."""
    out.mkdir(parents=True, exist_ok=True)
    for item in checkpoint.iterdir():
        shutil.copy2(item, out / item.name)
    (out / SPEC_FILE).write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (out / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence, indent=2), flush=True)
    print(f"Saved to {out}. Carry this folder to the next GPU.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("leg", choices=("start", "resume"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--checkpoint", type=Path, help="Folder from the previous leg. Needed for resume.")
    parser.add_argument("--steps", type=int, default=150, help="Logical steps to train in this leg.")
    parser.add_argument("--micro-batch", type=int, default=16)
    parser.add_argument("--total-steps", type=int, default=400, help="Job length. Sets the learning-rate schedule, so it can't change later.")
    parser.add_argument("--eval-examples", type=int, default=2000)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--identity", type=Path, help="Identity JSON for a new real run")
    args = parser.parse_args()
    if args.leg == "start" and not args.synthetic and not args.identity:
        parser.error("a new real run needs --identity from experiments/prepare_identity.py")
    if args.leg == "resume" and args.identity:
        parser.error("resume inherits the original spec; do not replace its identity")
    if args.synthetic and args.identity:
        parser.error("--identity is only for the real workload")
    if args.out.exists():
        parser.error(f"{args.out} already exists, and evidence shouldn't be overwritten")
    if args.leg == "resume" and not args.checkpoint:
        parser.error("resume needs --checkpoint")
    if args.device.startswith("cuda") and not torch.cuda.is_available():
        parser.error("no GPU visible here. Evidence from a CPU would be worthless")

    amp = amp_for(args.device)
    start_step = 0
    previous = None
    if args.leg == "resume":
        spec = json.loads((args.checkpoint / SPEC_FILE).read_text(encoding="utf-8"))
        previous = json.loads((args.checkpoint / MANIFEST_FILE).read_text(encoding="utf-8"))
        start_step = previous["logical_step"]
    else:
        spec = (test_spec if args.synthetic else real_spec)(args.total_steps, 1337)
        if args.identity:
            spec = apply_identity(spec, json.loads(args.identity.read_text(encoding="utf-8")))
    stop = start_step + args.steps
    if stop % spec.get("checkpoint_every_steps", 25) and stop != spec["total_logical_steps"]:
        parser.error("start step plus --steps must land on a checkpoint: a multiple of 25")
    if stop > spec["total_logical_steps"]:
        parser.error(f"that passes the job's {spec['total_logical_steps']} steps")

    exec_config = exec_for(args.micro_batch, spec["logical_batch_size"], amp)
    with tempfile.TemporaryDirectory() as tmp:
        leg = run_attempt(Path(tmp) / "leg", spec, exec_config, args.device, attempt=(previous["attempt"] + 1) if previous else 1, resume_from=args.checkpoint, stop_at_step=stop)
        checkpoint = leg["checkpoints"][stop]
        leg_accuracy, eval_hash = accuracy(spec, checkpoint, args.eval_examples, args.device)
        evidence = {
            "experiment": "cross_gpu_resume",
            # Decided from the job itself, because a resumed leg inherits its spec without any flag saying so.
            "evidence_type": "SCRIPT CHECK ONLY, synthetic workload" if spec["template"] == "synthetic-linear/v1" else ("REAL GPU RUN" if args.device.startswith("cuda") else "CPU RUN, NOT GPU EVIDENCE"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "machine": machine(args.device),
            "leg": args.leg,
            "from_step": start_step,
            "to_step": stop,
            "precision": amp,
            "exec": exec_config,
            "previous_leg": {"gpu": previous["environment"]["gpu"], "exec": previous["exec"]} if previous else None,
            "canary": leg["canary"],
            "accuracy_after_leg": leg_accuracy,
            "eval_set_hash": eval_hash,
            "eval_examples": args.eval_examples,
            "seconds": leg["seconds"],
            "peak_mem_mib": leg["peak_mem_mib"],
            "not_claimed": "This doesn't show the result equals an uninterrupted run. experiments/quality_comparison.py measures that.",
        }
        save_leg(args.out, checkpoint, spec, evidence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
