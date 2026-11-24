"""Staging follows branches, live follows exact commits that only move by a promotion (decision of 24 Nov 2026).

The release script tests run the real ekido-release.sh under bash with `git` and `curl` replaced by stubs, so nothing
touches the network. They need bash and jq (CI has both) and are skipped on a Windows laptop, where a stray `bash`
could start WSL.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELEASE_SH = ROOT / "server" / "ops" / "ekido-release.sh"
spec = importlib.util.spec_from_file_location("promote", ROOT / "tools" / "promote.py")
promote = importlib.util.module_from_spec(spec)
spec.loader.exec_module(promote)

MAIN = "a" * 40
PINNED = {"ekido-ai/ekido-infra": "1" * 40, "ekido-ai/ekido-core": "2" * 40, "ekido-ai/ekido-platform": "3" * 40}
DEPLOY = {"repos": [
    {"name": "infra", "repo": "ekido-ai/ekido-infra", "ref": "heads/main", "checks": ["Scripts"], "map": [["server", "infra/server"]]},
    {"name": "core", "repo": "ekido-ai/ekido-core", "ref": "tags/v0.4.3", "checks": [], "map": [["packages", "packages"]]},
    {"name": "platform", "repo": "ekido-ai/ekido-platform", "ref": "heads/main", "checks": ["Python (platform)"], "map": [[".", "apps/platform"]]},
]}
VERSION = {"sha": "abc123", "environment": "staging", "deployed_at": "2026-11-24T08:00:00Z", "repos": PINNED}


class Promote(unittest.TestCase):
    def test_every_repo_is_pinned_to_the_commit_staging_runs_and_keeps_its_checks_and_map(self):
        live = promote.build_live_manifest(DEPLOY, VERSION)
        self.assertEqual([e["ref"] for e in live["repos"]], list(PINNED.values()))
        self.assertEqual(live["repos"][2]["checks"], ["Python (platform)"])
        self.assertEqual(live["repos"][0]["map"], [["server", "infra/server"]])
        self.assertEqual(live["_promoted_from"]["release"], "abc123")

    def test_a_repo_missing_from_staging_or_a_bad_commit_is_refused(self):
        for repos in ({k: v for k, v in PINNED.items() if "platform" not in k}, {**PINNED, "ekido-ai/ekido-platform": "main"}):
            with self.subTest(repos=repos), self.assertRaises(ValueError):
                promote.build_live_manifest(DEPLOY, {**VERSION, "repos": repos})
        with self.assertRaises(ValueError):
            promote.build_live_manifest(DEPLOY, {"sha": "x"})

    def test_summary_links_the_compare_page_for_what_moves(self):
        old = promote.build_live_manifest(DEPLOY, {**VERSION, "repos": {**PINNED, "ekido-ai/ekido-platform": "4" * 40}})
        new = promote.build_live_manifest(DEPLOY, VERSION)
        lines = promote.summary(old, new)
        self.assertIn("unchanged", lines[0])
        self.assertIn("compare/" + "4" * 12 + "..." + "3" * 12, lines[2])
        self.assertIn("new at", promote.summary(None, new)[0])

    def test_only_https_is_fetched(self):
        with self.assertRaises(ValueError):
            promote.fetch("http://example.com/version.json")


@unittest.skipIf(os.name == "nt", "runs in CI; a Windows laptop's bash could be WSL")
@unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "needs bash and jq")
class ReleaseResolve(unittest.TestCase):
    HARNESS = r"""
set -euo pipefail
export EKIDO_ENV_FILE="$ENV_FILE"
. "$RELEASE_SH"
git() { if [ "$1" = ls-remote ]; then printf '%s\trefs/x\n' "$MAIN_SHA"; else command git "$@"; fi; }
curl() { local url="${*: -1}"; echo "$url" >> "$REQUESTS"; cat "$MANIFESTS/$(basename "$url")"; }
release_resolve "$OUT"
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "deploy.json").write_text(json.dumps(DEPLOY), encoding="utf-8")
        (self.tmp / "deploy.live.json").write_text(json.dumps(promote.build_live_manifest(DEPLOY, VERSION)), encoding="utf-8")

    def resolve(self, env_text):
        env_file = self.tmp / "environment"
        if env_text is not None:
            env_file.write_text(env_text, encoding="utf-8")
        run = subprocess.run(
            ["bash", "-c", self.HARNESS],
            env={**os.environ, "ENV_FILE": str(env_file), "RELEASE_SH": str(RELEASE_SH), "MAIN_SHA": MAIN,
                 "MANIFESTS": str(self.tmp), "REQUESTS": str(self.tmp / "requests"), "OUT": str(self.tmp / "out.json")},
            capture_output=True, text=True, timeout=60)
        requests = (self.tmp / "requests").read_text(encoding="utf-8") if (self.tmp / "requests").exists() else ""
        out = json.loads((self.tmp / "out.json").read_text(encoding="utf-8")) if run.returncode == 0 else None
        return run, requests, out

    def test_staging_follows_the_branches_and_the_infra_commit_it_read_the_manifest_from(self):
        for text in (None, "staging\n", "\n"):
            with self.subTest(file=text):
                run, requests, out = self.resolve(text)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertTrue(requests.strip().endswith("/deploy.json"), requests)
                self.assertEqual({e["sha"] for e in out["repos"]}, {MAIN})

    def test_live_follows_only_the_pinned_commits_including_infra(self):
        run, requests, out = self.resolve("live\n")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(requests.strip().endswith("/deploy.live.json"), requests)
        self.assertEqual([e["sha"] for e in out["repos"]], list(PINNED.values()))
        self.assertNotIn(MAIN, [e["sha"] for e in out["repos"]])

    def test_an_unknown_environment_is_refused_not_guessed(self):
        for text in ("prod", "LIVE", "live staging"):
            with self.subTest(file=text):
                run, _, out = self.resolve(text)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("unknown environment", run.stderr)
                self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
