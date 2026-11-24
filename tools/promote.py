"""Promote to live: write deploy.live.json from what staging is running right now.

    python tools/promote.py                       # staging's version.json -> deploy.live.json, with a summary
    python tools/promote.py --check               # exit 1 if live's file differs from staging (nothing is written)
    python tools/promote.py --from-url URL        # another version.json (for example live's own, to start the file)

Live runs the exact commits listed in deploy.live.json (see ekido-release.sh), and nothing else. This tool copies the
commit staging reports for every repo into that file, keeping each repo's checks and file map from deploy.json. The
change goes through a pull request that Piyush reviews (CODEOWNERS), so "live changes" always means "Piyush said haan",
with the list of what moves in the pull request. It prints a GitHub compare link for every repo that moves.

Only HTTPS is used, and the version.json must list a commit for every repo in deploy.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = "https://ekido-staging.duckdns.org/deploy/version.json"
SHA = re.compile(r"^[0-9a-f]{40}$")


def build_live_manifest(deploy: dict, version: dict) -> dict:
    """deploy.json's repos, each pinned to the commit `version` says it runs."""
    repos = version.get("repos")
    if not isinstance(repos, dict):
        raise ValueError("version.json has no 'repos'")
    live = {"_about": ("What live runs: exact commits, moved only by a reviewed promotion (tools/promote.py). "
                       "Same checks and file map as deploy.json."),
            "_promoted_from": {"release": version.get("sha"), "environment": version.get("environment"),
                               "deployed_at": version.get("deployed_at")},
            "repos": []}
    for entry in deploy["repos"]:
        sha = repos.get(entry["repo"])
        if not isinstance(sha, str) or not SHA.match(sha):
            raise ValueError(f"version.json has no commit for {entry['repo']}")
        live["repos"].append({**entry, "ref": sha})
    return live


def summary(old: dict | None, new: dict) -> list[str]:
    before = {e["repo"]: e["ref"] for e in (old or {}).get("repos", [])}
    lines = []
    for entry in new["repos"]:
        repo, sha = entry["repo"], entry["ref"]
        was = before.get(repo)
        if was == sha:
            lines.append(f"  {repo}: unchanged ({sha[:7]})")
        elif was and SHA.match(was):
            lines.append(f"  {repo}: {was[:7]} -> {sha[:7]}  https://github.com/{repo}/compare/{was[:12]}...{sha[:12]}")
        else:
            lines.append(f"  {repo}: new at {sha[:7]}")
    return lines


def fetch(url: str) -> dict:
    if not url.startswith("https://"):
        raise ValueError("only https URLs are used")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ekido-promote"}), timeout=30) as response:
        body = response.read(1_000_001)
    if len(body) > 1_000_000:
        raise ValueError("version.json is too large")
    return json.loads(body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-url", default=STAGING)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    deploy = json.loads((ROOT / "deploy.json").read_text(encoding="utf-8"))
    path = ROOT / "deploy.live.json"
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    version = fetch(args.from_url)
    if version.get("failed_sha") and version.get("failed_sha") == version.get("sha"):
        raise ValueError("that server refused its newest release; promote something it actually runs")
    new = build_live_manifest(deploy, version)
    lines = summary(old, new)
    same = old is not None and [(e["repo"], e["ref"]) for e in old["repos"]] == [(e["repo"], e["ref"]) for e in new["repos"]]
    print(f"{'Live already matches' if same else 'Live would move to'} {args.from_url} (release {version.get('sha')}):")
    print("\n".join(lines))
    if args.check:
        return 0 if same else 1
    if same:
        print("Nothing to promote.")
        return 0
    path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {path.name}. Open a pull request with it; Piyush's approval is the 'haan'.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(f"promote: {error}", file=sys.stderr)
        sys.exit(2)
