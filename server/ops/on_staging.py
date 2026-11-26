#!/usr/bin/env python3
"""The `on-staging` label (ekido-internal #161): staging runs a labelled pull request's commit instead of that repo's main.

    on_staging.py <resolved release.json>      rewrites the file in place; prints what it chose and what it refused

Anonymous GitHub reads only: no credential is stored, and nothing in GitHub can reach into the server. One pull request per repo, the
lowest number first; the next one waits until the label is removed or it is closed.

Refused, because staging could not undo it or it would run with too much power: a pull request from a fork; a draft; one not against
main; one for ekido-infra (its scripts run as root); one that changes database migrations (unlabelling would not migrate back) or
Python or npm dependencies. If GitHub cannot be read, the release is left as it was, which is main.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from typing import Any, Callable

API = "https://api.github.com"
ORG = "ekido-ai"
LABEL = "on-staging"
NEVER = {f"{ORG}/ekido-infra", f"{ORG}/ekido-internal"}
UNSAFE_FILE = re.compile(r"(^|/)(alembic/|pyproject\.toml$|requirements[^/]*\.txt$|package(-lock)?\.json$|Cargo\.(toml|lock)$)")


def get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "ekido-autodeploy"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def refusal(repo: str, pr: dict, files: list[dict]) -> str | None:
    if repo in NEVER:
        return "this repo is never deployed from a pull request"
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo:
        return "it comes from a fork"
    if pr.get("draft"):
        return "it is a draft"
    if (pr.get("base") or {}).get("ref") != "main":
        return "it is not against main"
    if not re.fullmatch(r"[0-9a-f]{40}", str((pr.get("head") or {}).get("sha", ""))):
        return "it has no commit"
    if len(files) >= 100:
        return "it changes too many files to check"
    bad = [f["filename"] for f in files if UNSAFE_FILE.search(f["filename"]) or UNSAFE_FILE.search(f.get("previous_filename", ""))]
    if bad:
        return f"it changes migrations or dependencies ({bad[0]})"
    return None


def choose(repos: set[str], fetch: Callable[[str], Any] = get_json) -> tuple[dict[str, dict], list[str]]:
    """({repo: {number, sha}}, notes) for the repos in this release."""
    found = fetch(f"{API}/search/issues?q=org:{ORG}+is:pr+is:open+label:{LABEL}&per_page=50")
    chosen: dict[str, dict] = {}
    notes: list[str] = []
    for item in sorted(found.get("items", []), key=lambda i: i["number"]):
        repo = item["repository_url"].split("/repos/", 1)[1]
        number = int(item["number"])
        if repo not in repos:
            notes.append(f"{repo}#{number}: refused, not part of the release")
            continue
        if repo in chosen:
            notes.append(f"{repo}#{number}: waiting behind #{chosen[repo]['number']}")
            continue
        pr = fetch(f"{API}/repos/{repo}/pulls/{number}")
        files = fetch(f"{API}/repos/{repo}/pulls/{number}/files?per_page=100")
        why = refusal(repo, pr, files)
        if why:
            notes.append(f"{repo}#{number}: refused, {why}")
            continue
        chosen[repo] = {"number": number, "sha": pr["head"]["sha"]}
    return chosen, notes


def overlay(release: dict, fetch: Callable[[str], Any] = get_json) -> tuple[dict, list[str]]:
    chosen, notes = choose({entry["repo"] for entry in release["repos"]}, fetch)
    for entry in release["repos"]:
        pick = chosen.get(entry["repo"])
        if pick:
            entry["sha"], entry["pull_request"] = pick["sha"], pick["number"]
            notes.append(f"{entry['repo']}#{pick['number']}: staging runs {pick['sha'][:7]}")
    return release, notes


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        release = json.load(handle)
    try:
        release, notes = overlay(release)
    except Exception as exc:  # GitHub down, rate limited, an unexpected answer: deploy main, as without the label
        print(f"on-staging: could not read GitHub ({type(exc).__name__}); using the branches", file=sys.stderr)
        return 0
    for note in notes:
        print(f"on-staging: {note}", file=sys.stderr)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(release, handle)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
