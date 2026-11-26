"""The required check for a pull request labelled `on-staging` (ekido-internal #161): staging runs its latest commit, and the PR report
has staging screenshots. Reads REPO, HEAD_SHA, PR_BODY and STAGING_URL from the environment."""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request


def problems(repo: str, head_sha: str, body: str, version: dict) -> list[str]:
    out = []
    running = (version.get("repos") or {}).get(repo)
    if version.get("environment") != "staging":
        out.append("the server that answered is not staging")
    if running != head_sha:
        out.append(f"staging runs {str(running)[:7]}, not this commit {head_sha[:7]}")
    found = re.search(r"^## Screenshots\s*$(.*?)(?=^## |\Z)", body or "", re.M | re.S)
    if not found or not re.search(r"!\[|<img\s", found.group(1)):
        out.append("the PR report has no staging screenshot under '## Screenshots'")
    return out


def main() -> int:
    url = os.environ["STAGING_URL"].rstrip("/") + "/deploy/version.json"
    with urllib.request.urlopen(url, timeout=30) as response:
        version = json.load(response)
    found = problems(os.environ["REPO"], os.environ["HEAD_SHA"], os.environ.get("PR_BODY", ""), version)
    for problem in found:
        print(f"::error::{problem}")
    if not found:
        print("staging runs this commit and the report has screenshots")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
