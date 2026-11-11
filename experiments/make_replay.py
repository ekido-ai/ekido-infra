"""Builds the dashboard's replay file from a real run, marking every event as RECORDED.

    python experiments/make_replay.py --api http://127.0.0.1:8000     newest job on a running stack
    python experiments/make_replay.py experiments/results/gpu_story_20261104_230131.json
"""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "apps" / "web" / "public" / "replay.json"
FROM_FILE = {
    "a": ("Provider A", "REAL", None, "0.001"),
    "p": ("Provider P", "INJECTED_FAULT", 1200, "0.002"),
    "b": ("Provider B", "CAPPED_PROFILE", 768, "0.003"),
    "c": ("Provider C", "CAPPED_PROFILE", 300, "0.0005"),
}


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.loads(response.read())


def from_api(base: str) -> tuple[list, list, str]:
    job = get(base, "/jobs?limit=1")[0]
    return get(base, "/providers"), get(base, f"/jobs/{job['job_id']}/timeline"), f"Job {job['job_id']} in state {job['state']}"


def from_file(path: Path) -> tuple[list, list, str]:
    run = json.loads(path.read_text(encoding="utf-8"))
    providers = [
        {"provider_id": run["providers"][key], "name": name, "kind": kind, "status": "ONLINE", "capacity_mib": cap, "price_per_step": price}
        for key, (name, kind, cap, price) in FROM_FILE.items()
    ]
    return providers, run["events"], f"From {path.name}"


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--api":
        providers, events, note = from_api(sys.argv[2])
    elif len(sys.argv) == 2:
        providers, events, note = from_file(Path(sys.argv[1]))
    else:
        raise SystemExit(__doc__)
    day = datetime.fromisoformat(events[0]["ts"]).strftime("%d %b %Y").lstrip("0")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps({"recorded_at": day, "note": note, "providers": providers, "events": [dict(e, provenance="RECORDED") for e in events]}), encoding="utf-8")
    print(f"wrote {OUTPUT}: {len(events)} events. {note}")


if __name__ == "__main__":
    main()
