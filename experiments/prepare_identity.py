"""Resolve model/dataset commits and hash full ordered AG News splits for NEW runs."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/gpuchain_core"))
from gpuchain_core.data_identity import hash_text_labels, require_commit  # noqa: E402


def main():
    from datasets import load_dataset
    from huggingface_hub import HfApi

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("output exists; choose a new file")
    model, dataset = "distilbert-base-uncased", "fancyzhx/ag_news"
    api = HfApi()
    model_sha = require_commit(api.model_info(model, revision="main").sha, "model revision")
    dataset_sha = require_commit(api.dataset_info(dataset, revision="main").sha, "dataset revision")
    result = {"model_id": model, "model_revision": model_sha, "dataset_id": dataset,
              "dataset_revision": dataset_sha}
    for split in ("train", "test"):
        rows = load_dataset(dataset, split=split, revision=dataset_sha)
        result[f"{split}_sha256"] = hash_text_labels(rows)
        result[f"{split}_rows"] = len(rows)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
