"""The on-staging queue and its check (ekido-internal #161), with GitHub faked."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


queue = load("on_staging", "server/ops/on_staging.py")
check = load("on_staging_check", "tools/on_staging_check.py")
WEB, INFRA = "ekido-ai/ekido-web", "ekido-ai/ekido-infra"


def pr(number, repo=WEB, sha=None, files=("src/a.tsx",), head_repo=None, draft=False, base="main"):
    return {"number": number, "repo": repo, "files": [{"filename": f} for f in files],
            "pr": {"draft": draft, "base": {"ref": base}, "head": {"sha": sha or f"{number:040x}", "repo": {"full_name": head_repo or repo}}}}


def github(*prs):
    def fetch(url):
        if "/search/issues" in url:
            return {"items": [{"number": p["number"], "repository_url": f"https://api.github.com/repos/{p['repo']}"} for p in prs]}
        for p in prs:
            if url.endswith(f"/repos/{p['repo']}/pulls/{p['number']}"):
                return p["pr"]
            if f"/repos/{p['repo']}/pulls/{p['number']}/files" in url:
                return p["files"]
        raise AssertionError(url)

    return fetch


def release():
    return {"repos": [{"repo": WEB, "sha": "m" * 40}, {"repo": INFRA, "sha": "i" * 40}]}


class Queue(unittest.TestCase):
    def run_with(self, *prs):
        out, notes = queue.overlay(release(), github(*prs))
        return out["repos"][0]["sha"], out["repos"][1]["sha"], " | ".join(notes)

    def test_the_lowest_number_runs_and_the_next_waits(self):
        web, _, notes = self.run_with(pr(9), pr(4))
        self.assertEqual(web, f"{4:040x}")
        self.assertIn("#9: waiting behind #4", notes)

    def test_nothing_labelled_leaves_main(self):
        self.assertEqual(self.run_with()[0], "m" * 40)

    def test_what_is_refused(self):
        cases = {"fork": pr(1, head_repo="stranger/ekido-web"), "draft": pr(1, draft=True), "base": pr(1, base="other"),
                 "migration": pr(1, files=("alembic/versions/0009.py",)), "npm": pr(1, files=("package-lock.json",)),
                 "python": pr(1, files=("apps/x/pyproject.toml",)), "sha": pr(1, sha="main"), "too many": pr(1, files=("a",) * 100)}
        for name, case in cases.items():
            with self.subTest(name):
                web, _, notes = self.run_with(case)
                self.assertEqual(web, "m" * 40)
                self.assertIn("refused", notes)

    def test_infra_is_never_deployed_from_a_pull_request(self):
        _, infra, notes = self.run_with(pr(1, repo=INFRA, files=("server/x.sh",)))
        self.assertEqual(infra, "i" * 40)
        self.assertIn("never", notes)

    def test_a_refused_one_does_not_block_the_next(self):
        self.assertEqual(self.run_with(pr(1, draft=True), pr(2))[0], f"{2:040x}")


class Check(unittest.TestCase):
    BODY = "## What changed\nx\n## Screenshots\n![staging](https://example/s.png)\n## Next\ny\n"

    def problems(self, sha="a" * 40, body=BODY, **version):
        return check.problems(WEB, sha, body, {"environment": "staging", "repos": {WEB: "a" * 40}, **version})

    def test_passes_only_with_this_commit_on_staging_and_a_screenshot(self):
        self.assertEqual(self.problems(), [])
        self.assertTrue(self.problems(sha="b" * 40))
        self.assertTrue(self.problems(body="## Screenshots\nNone: nothing visible\n## x\n![a](b)"))
        self.assertTrue(self.problems(environment="live"))


class Wiring(unittest.TestCase):
    def test_it_is_off_unless_the_operator_turns_it_on_and_only_on_staging(self):
        text = (ROOT / "server/ops/ekido-release.sh").read_text(encoding="utf-8")
        line = next(line for line in text.splitlines() if "on_staging.py" in line and not line.lstrip().startswith("#"))
        self.assertIn('[ "$mfile" = deploy.json ]', line)
        self.assertIn("/etc/ekido/on-staging", line)


if __name__ == "__main__":
    unittest.main()
