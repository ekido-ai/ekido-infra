"""Gate G1 feasibility spike. Measures, does not assume. Writes raw JSON to experiments/results/.

Answers:
  1. Do the pinned versions work together on this GPU?
  2. Which precision modes are native?
  3. How fast is training, and how much VRAM does each micro-batch size need?
  4. Does a per-process memory cap cause a real allocator OOM?
  5. What accuracy does a short LoRA job reach, and how noisy is it between checkpoints?
  6. How big is a checkpoint (trainable + optimizer) and how long does it take to save?
  7. How long does a full evaluation take, on GPU and on CPU?

Run inside the runtime image. See experiments/README.md.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import platform
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import torch
from datasets import load_dataset
from peft import LoraConfig, TaskType, get_peft_model
from safetensors.torch import save_file
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_ID = "distilbert-base-uncased"
TARGET_MODULES = ["q_lin", "v_lin"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def seed_all(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def build_model(lora_r: int = 8) -> torch.nn.Module:
    base = AutoModelForSequenceClassification.from_pretrained(MODEL_ID, num_labels=4)
    config = LoraConfig(
        task_type=TaskType.SEQ_CLS,
        r=lora_r,
        lora_alpha=16,
        lora_dropout=0.1,
        target_modules=TARGET_MODULES,
    )
    return get_peft_model(base, config)


def tokenize(tokenizer, texts: list[str], max_len: int) -> dict[str, torch.Tensor]:
    enc = tokenizer(texts, padding="max_length", truncation=True, max_length=max_len, return_tensors="pt")
    return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"]}


def amp_dtype(mode: str):
    return {"bf16": torch.bfloat16, "fp16": torch.float16}.get(mode)


def train_step(model, optimizer, scaler, ids, mask, labels, micro_batch: int, amp: str, device: str) -> float:
    """One logical step: accumulates over micro-batches, weighted by micro-batch share."""
    n = ids.shape[0]
    total = 0.0
    optimizer.zero_grad(set_to_none=True)
    dtype = amp_dtype(amp)
    for start in range(0, n, micro_batch):
        mb_ids = ids[start:start + micro_batch].to(device)
        mb_mask = mask[start:start + micro_batch].to(device)
        mb_labels = labels[start:start + micro_batch].to(device)
        with torch.autocast("cuda", dtype=dtype, enabled=dtype is not None and device == "cuda"):
            loss = model(input_ids=mb_ids, attention_mask=mb_mask, labels=mb_labels).loss
        weighted = loss * (mb_ids.shape[0] / n)
        total += weighted.item()
        if scaler is not None:
            scaler.scale(weighted).backward()
        else:
            weighted.backward()
    params = [p for p in model.parameters() if p.requires_grad]
    if scaler is not None:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        scaler.step(optimizer)
        scaler.update()
    else:
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        optimizer.step()
    return total


@torch.no_grad()
def evaluate(model, ids, mask, labels, device: str, batch_size: int = 64) -> np.ndarray:
    model.eval()
    correct = []
    for start in range(0, ids.shape[0], batch_size):
        out = model(input_ids=ids[start:start + batch_size].to(device),
                    attention_mask=mask[start:start + batch_size].to(device)).logits
        correct.append((out.argmax(-1).cpu() == labels[start:start + batch_size]).numpy())
    model.train()
    return np.concatenate(correct)


def free_gpu() -> None:
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()


def section_environment() -> dict:
    env = {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
    }
    import peft
    import transformers
    env["transformers"] = transformers.__version__
    env["peft"] = peft.__version__
    if torch.cuda.is_available():
        cap = torch.cuda.get_device_capability(0)
        env.update(
            gpu=torch.cuda.get_device_name(0),
            total_mib=round(torch.cuda.get_device_properties(0).total_memory / 2**20),
            compute_capability=f"{cap[0]}.{cap[1]}",
            bf16_reported_default=torch.cuda.is_bf16_supported(),
            bf16_native=torch.cuda.is_bf16_supported(including_emulation=False),
            arch_list=torch.cuda.get_arch_list(),
        )
    return env


def section_memory_sweep(train, args) -> list[dict]:
    """Peak VRAM and speed for each micro-batch at a fixed logical batch."""
    rows = []
    ids, mask, labels = train
    L = args.logical_batch
    for amp in ("bf16", "fp16", "none"):
        for micro in [m for m in (32, 16, 8, 4, 2, 1) if m <= L and L % m == 0]:
            free_gpu()
            seed_all(args.seed)
            model = build_model().to("cuda")
            params = [p for p in model.parameters() if p.requires_grad]
            optimizer = torch.optim.AdamW(params, lr=args.lr)
            scaler = torch.amp.GradScaler("cuda") if amp == "fp16" else None
            steps = args.sweep_steps
            row = {"amp": amp, "micro_batch": micro, "accumulation": L // micro, "steps": steps}
            try:
                # First step allocates optimizer state, so it is excluded from timing.
                train_step(model, optimizer, scaler, ids[:L], mask[:L], labels[:L], micro, amp, "cuda")
                torch.cuda.synchronize()
                t0 = time.perf_counter()
                for s in range(steps):
                    a = (s + 1) * L
                    train_step(model, optimizer, scaler, ids[a:a + L], mask[a:a + L], labels[a:a + L], micro, amp, "cuda")
                torch.cuda.synchronize()
                elapsed = time.perf_counter() - t0
                row.update(
                    status="ok",
                    logical_steps_per_s=round(steps / elapsed, 3),
                    peak_allocated_mib=round(torch.cuda.max_memory_allocated() / 2**20, 1),
                    peak_reserved_mib=round(torch.cuda.max_memory_reserved() / 2**20, 1),
                )
            except torch.OutOfMemoryError as exc:
                row.update(status="oom", error=str(exc)[:160])
            log(f"sweep {row}")
            rows.append(row)
            del model, optimizer, scaler
    free_gpu()
    return rows


def section_memory_cap(train, args) -> list[dict]:
    """Does set_per_process_memory_fraction produce a real, catchable OOM, and can we recover in-process?"""
    ids, mask, labels = train
    L = args.logical_batch
    total = torch.cuda.get_device_properties(0).total_memory
    rows = []
    for cap_mib in args.caps_mib:
        fraction = min(1.0, cap_mib * 2**20 / total)
        torch.cuda.set_per_process_memory_fraction(fraction, 0)
        fitting = None
        tried = []
        for micro in [m for m in (32, 16, 8, 4, 2, 1) if m <= L and L % m == 0]:
            free_gpu()
            model = None
            optimizer = None
            try:
                seed_all(args.seed)
                model = build_model().to("cuda")
                params = [p for p in model.parameters() if p.requires_grad]
                optimizer = torch.optim.AdamW(params, lr=args.lr)
                train_step(model, optimizer, None, ids[:L], mask[:L], labels[:L], micro, "bf16", "cuda")
                train_step(model, optimizer, None, ids[L:2 * L], mask[L:2 * L], labels[L:2 * L], micro, "bf16", "cuda")
                peak = round(torch.cuda.max_memory_reserved() / 2**20, 1)
                tried.append({"micro_batch": micro, "status": "ok", "peak_reserved_mib": peak})
                fitting = micro
                break
            except torch.OutOfMemoryError as exc:
                tried.append({"micro_batch": micro, "status": "oom", "error": str(exc)[:120]})
            finally:
                del model, optimizer
        row = {"cap_mib": cap_mib, "first_fitting_micro_batch": fitting, "tried": tried}
        log(f"cap {row}")
        rows.append(row)
    torch.cuda.set_per_process_memory_fraction(1.0, 0)
    free_gpu()
    return rows


def section_training_curve(train, eval_set, args) -> dict:
    """A short real job: accuracy over time, fluctuation between checkpoints, checkpoint size and time."""
    ids, mask, labels = train
    e_ids, e_mask, e_labels = eval_set
    L = args.logical_batch
    free_gpu()
    seed_all(args.seed)
    model = build_model().to("cuda")
    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.01)

    trainable = sum(p.numel() for p in params)
    total_params = sum(p.numel() for p in model.parameters())

    def lr_at(step: int) -> float:
        warm = max(1, int(0.1 * args.steps))
        if step < warm:
            return args.lr * (step + 1) / warm
        return args.lr * max(0.0, (args.steps - step) / max(1, args.steps - warm))

    order = np.random.default_rng(args.seed).permutation(ids.shape[0])
    curve = []
    train_seconds = 0.0
    last_correct = evaluate(model, e_ids, e_mask, e_labels, "cuda")
    curve.append({"step": 0, "accuracy": float(last_correct.mean()), "loss": None})
    log(f"step 0 accuracy {curve[0]['accuracy']:.4f}")
    model.train()
    loss = math.nan
    for step in range(args.steps):
        for group in optimizer.param_groups:
            group["lr"] = lr_at(step)
        start = (step * L) % (ids.shape[0] - L)
        idx = torch.as_tensor(order[start:start + L])
        t0 = time.perf_counter()
        loss = train_step(model, optimizer, None, ids[idx], mask[idx], labels[idx], args.micro_batch, "bf16", "cuda")
        torch.cuda.synchronize()
        train_seconds += time.perf_counter() - t0
        if (step + 1) % args.eval_every == 0 or step + 1 == args.steps:
            correct = evaluate(model, e_ids, e_mask, e_labels, "cuda")
            point = {"step": step + 1, "accuracy": float(correct.mean()), "loss": round(loss, 4),
                     "changed_vs_previous": int((correct != last_correct).sum())}
            curve.append(point)
            log(f"step {point}")
            last_correct = correct

    # Checkpoint measurement, using the GPCK v1 layout: safetensors only.
    trainable_state = {n: p.detach().cpu().clone() for n, p in model.named_parameters() if p.requires_grad}
    names = {id(p): n for n, p in model.named_parameters() if p.requires_grad}
    optim_state = {}
    for param, state in optimizer.state.items():
        name = names[id(param)]
        optim_state[f"{name}.exp_avg"] = state["exp_avg"].detach().cpu().clone()
        optim_state[f"{name}.exp_avg_sq"] = state["exp_avg_sq"].detach().cpu().clone()
        optim_state[f"{name}.step"] = torch.as_tensor(state["step"]).detach().cpu().clone().reshape(1)
    with tempfile.TemporaryDirectory() as tmp:
        t0 = time.perf_counter()
        save_file(trainable_state, f"{tmp}/trainable.safetensors")
        save_file(optim_state, f"{tmp}/optimizer.safetensors")
        save_seconds = time.perf_counter() - t0
        trainable_bytes = Path(f"{tmp}/trainable.safetensors").stat().st_size
        optimizer_bytes = Path(f"{tmp}/optimizer.safetensors").stat().st_size

    # Evaluation time on GPU and CPU, on the same data.
    t0 = time.perf_counter()
    evaluate(model, e_ids, e_mask, e_labels, "cuda")
    torch.cuda.synchronize()
    eval_gpu_seconds = time.perf_counter() - t0
    gpu_correct = last_correct

    model = model.to("cpu")
    n_cpu = min(args.cpu_eval_examples, e_ids.shape[0])
    t0 = time.perf_counter()
    cpu_correct = evaluate(model, e_ids[:n_cpu], e_mask[:n_cpu], e_labels[:n_cpu], "cpu")
    cpu_seconds = time.perf_counter() - t0

    final = curve[-1]["accuracy"]
    accuracies = [c["accuracy"] for c in curve[1:]]
    dips = [round(b - a, 4) for a, b in zip(accuracies, accuracies[1:])]
    result = {
        "steps": args.steps,
        "micro_batch": args.micro_batch,
        "logical_batch": L,
        "trainable_params": trainable,
        "total_params": total_params,
        "train_seconds": round(train_seconds, 2),
        "logical_steps_per_s": round(args.steps / train_seconds, 3),
        "peak_reserved_mib": round(torch.cuda.max_memory_reserved() / 2**20, 1),
        "curve": curve,
        "final_accuracy": final,
        "largest_drop_between_evals": min(dips) if dips else None,
        "steps_between_evals": args.eval_every,
        "eval_examples": int(e_ids.shape[0]),
        "half_width_95_unpaired": round(1.96 * math.sqrt(final * (1 - final) / e_ids.shape[0]), 5),
        "eval_seconds_gpu": round(eval_gpu_seconds, 2),
        "eval_seconds_cpu": round(cpu_seconds, 2),
        "eval_examples_cpu": n_cpu,
        "cpu_vs_gpu_prediction_flips": int((cpu_correct != gpu_correct[:n_cpu]).sum()),
        "checkpoint_trainable_bytes": trainable_bytes,
        "checkpoint_optimizer_bytes": optimizer_bytes,
        "checkpoint_save_seconds": round(save_seconds, 3),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--logical-batch", type=int, default=32)
    parser.add_argument("--micro-batch", type=int, default=32)
    parser.add_argument("--max-len", type=int, default=128)
    parser.add_argument("--lr", type=float, default=5e-4)
    parser.add_argument("--steps", type=int, default=300)
    parser.add_argument("--eval-every", type=int, default=50)
    parser.add_argument("--sweep-steps", type=int, default=10)
    parser.add_argument("--caps-mib", type=int, nargs="+", default=[3072, 2048, 1536, 1024, 768, 512])
    parser.add_argument("--train-examples", type=int, default=20000)
    parser.add_argument("--eval-examples", type=int, default=7600)
    parser.add_argument("--cpu-eval-examples", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--only", nargs="*", default=["env", "sweep", "cap", "curve"])
    parser.add_argument("--out", default="experiments/results")
    args = parser.parse_args()

    results: dict = {"started_at": datetime.now(timezone.utc).isoformat(), "args": vars(args)}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"spike_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    def save() -> None:
        out_file.write_text(json.dumps(results, indent=2))

    results["environment"] = section_environment()
    log(json.dumps(results["environment"]))
    save()

    log("loading data")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    data = load_dataset("fancyzhx/ag_news")
    rng = np.random.default_rng(args.seed)
    picks = rng.choice(len(data["train"]), size=args.train_examples, replace=False)
    subset = data["train"].select(picks.tolist())
    subset_text = list(subset["text"])
    enc = tokenize(tokenizer, subset_text, args.max_len)
    train = (enc["input_ids"], enc["attention_mask"], torch.as_tensor(list(subset["label"])))
    test = data["test"].select(range(min(args.eval_examples, len(data["test"]))))
    # The test split is ordered arbitrarily; shuffle once with a fixed seed so CPU subsets are representative.
    test = test.shuffle(seed=args.seed)
    enc = tokenize(tokenizer, list(test["text"]), args.max_len)
    eval_set = (enc["input_ids"], enc["attention_mask"], torch.as_tensor(list(test["label"])))
    lengths = [len(tokenizer(t)["input_ids"]) for t in subset_text[:5000]]
    results["data"] = {
        "train_examples": args.train_examples,
        "eval_examples": int(eval_set[0].shape[0]),
        "max_len": args.max_len,
        "token_length_p50": int(np.percentile(lengths, 50)),
        "token_length_p99": int(np.percentile(lengths, 99)),
        "truncated_fraction": round(float(np.mean(np.array(lengths) > args.max_len)), 5),
    }
    log(json.dumps(results["data"]))
    save()

    if "sweep" in args.only:
        results["memory_sweep"] = section_memory_sweep(train, args)
        save()
    if "cap" in args.only:
        results["memory_cap"] = section_memory_cap(train, args)
        save()
    if "curve" in args.only:
        results["training_curve"] = section_training_curve(train, eval_set, args)
        save()

    results["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    log(f"wrote {out_file}")


if __name__ == "__main__":
    main()
