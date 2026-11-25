#!/usr/bin/env python3
"""Publish a finished model so the Inference service can answer with it (ticket #153, server side).

    sudo python3 publish_model.py publish   --model-id <id> --job-id <job> --gguf <file> --sha256 <hex> [--mode test|live]
    sudo python3 publish_model.py unpublish --model-id <id>
    sudo python3 publish_model.py list

What publish does: checks the file against the expected SHA-256 (from the job's signed manifest), copies it to the model folder, gives it
a loopback port and an engine unit (ekido-engine@<id>), waits for the engine to answer, and only then writes the manifest the Platform
reads from INFERENCE_MODELS_ROOT. A model is never listed before its engine works.

unpublish stops the engine and renames the manifest so the Platform no longer sees it. It deletes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MODELS = Path("/var/lib/ekido-models/customers")
ENGINES = Path("/etc/ekido/engines")
REGISTRY = Path("/var/lib/ekido-inference-models")  # Platform's INFERENCE_MODELS_ROOT
PORTS = range(8100, 8150)
MODEL_ID = re.compile(r"[A-Za-z0-9_-]{1,120}")
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,36}")


def manifest(model_id: str, job_id: str, port: int, mode: str) -> dict:
    """Exactly what ekido-platform's `model_manifest` accepts (docs/inference-authority.md)."""
    return {"job_id": job_id, "engine_url": f"http://127.0.0.1:{port}", "engine_model": model_id, "mode": mode}


def check_manifest(item: dict) -> None:
    """The Platform's own rules, so a bad manifest is caught here and not silently ignored there."""
    if not all(isinstance(item.get(k), str) and item[k] for k in ("job_id", "engine_url", "engine_model")):
        raise ValueError("job_id, engine_url and engine_model are required")
    if len(item["job_id"]) > 36 or len(item["engine_url"]) > 512 or len(item["engine_model"]) > 200 or item.get("mode") not in ("test", "live"):
        raise ValueError("manifest field out of range")
    url = urlsplit(item["engine_url"])
    if url.scheme != "http" or url.port is None or url.path not in ("", "/") or not ipaddress.ip_address(url.hostname or "").is_loopback:
        raise ValueError("engine_url must be a numeric loopback origin with a port")


def pick_port(engines: Path, model_id: str, ports: range = PORTS) -> int:
    """The model's existing port, or the lowest free one."""
    taken: dict[str, int] = {}
    for env in engines.glob("*.env"):
        match = re.search(r"^ENGINE_PORT=(\d+)$", env.read_text(encoding="utf-8"), re.M)
        if match:
            taken[env.stem] = int(match.group(1))
    if model_id in taken:
        return taken[model_id]
    for port in ports:
        if port not in taken.values():
            return port
    raise RuntimeError(f"no free engine port in {ports.start}-{ports.stop - 1}")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_request(model_id: str, job_id: str, gguf: Path, expected_sha256: str) -> None:
    if not MODEL_ID.fullmatch(model_id):
        raise ValueError("model id must be letters, digits, - or _")
    if not JOB_ID.fullmatch(job_id):
        raise ValueError("job id must be letters, digits, - or _")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    if not gguf.is_file():
        raise ValueError("the GGUF file does not exist")
    with gguf.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
    if sha256_of(gguf) != expected_sha256:
        raise ValueError("the file's SHA-256 differs from the expected one; refusing to serve it")


def write_atomic(path: Path, data: str, mode: int = 0o640) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(data, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def healthy(port: int, seconds: int = 180) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(2)
    return False


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True)


def publish(model_id: str, job_id: str, gguf: Path, expected_sha256: str, mode: str, *, models: Path = MODELS, engines: Path = ENGINES,
            registry: Path = REGISTRY, run=systemctl, wait=healthy) -> int:
    check_request(model_id, job_id, gguf, expected_sha256)
    for folder in (models, engines, registry):
        folder.mkdir(parents=True, exist_ok=True)
    port = pick_port(engines, model_id)
    item = manifest(model_id, job_id, port, mode)
    check_manifest(item)
    target = models / f"{model_id}.gguf"
    if not target.exists() or sha256_of(target) != expected_sha256:
        shutil.copyfile(gguf, target.with_suffix(".tmp"))
        os.chmod(target.with_suffix(".tmp"), 0o644)
        os.replace(target.with_suffix(".tmp"), target)
    write_atomic(engines / f"{model_id}.env", f"ENGINE_MODEL={target.as_posix()}\nENGINE_PORT={port}\n", 0o644)
    run("enable", f"ekido-engine@{model_id}")
    run("restart", f"ekido-engine@{model_id}")
    if not wait(port):
        run("disable", "--now", f"ekido-engine@{model_id}")
        raise RuntimeError("the engine did not become healthy; the model was not listed")
    write_atomic(registry / f"{model_id}.json", json.dumps(item) + "\n", 0o644)
    return port


def unpublish(model_id: str, *, registry: Path = REGISTRY, run=systemctl) -> None:
    if not MODEL_ID.fullmatch(model_id):
        raise ValueError("model id must be letters, digits, - or _")
    listed = registry / f"{model_id}.json"
    if listed.exists():  # first, so no request is sent to an engine that is stopping
        os.replace(listed, registry / f"{model_id}.json.disabled")
    run("disable", "--now", f"ekido-engine@{model_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("publish")
    add.add_argument("--model-id", required=True)
    add.add_argument("--job-id", required=True)
    add.add_argument("--gguf", required=True, type=Path)
    add.add_argument("--sha256", required=True)
    add.add_argument("--mode", choices=("test", "live"), default="test")
    remove = sub.add_parser("unpublish")
    remove.add_argument("--model-id", required=True)
    sub.add_parser("list")
    args = parser.parse_args(argv)
    try:
        if args.command == "publish":
            port = publish(args.model_id, args.job_id, args.gguf, args.sha256, args.mode)
            print(f"published {args.model_id} on 127.0.0.1:{port} ({args.mode})")
        elif args.command == "unpublish":
            unpublish(args.model_id)
            print(f"unpublished {args.model_id}")
        else:
            for path in sorted(REGISTRY.glob("*.json")):
                print(path.stem, path.read_text(encoding="utf-8").strip())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
