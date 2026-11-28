"""ekido-web-deploy.sh replaces the app's files: what the repository no longer has must not stay on the site.

The real script runs against a temporary app directory, with sudo, systemctl, chown, npx and curl replaced by stubs.
"""
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "ekido-web-deploy.sh"
STUBS = {
    "sudo": 'shift 2; exec "$@"',  # sudo -u ekido <command>
    "systemctl": "echo active",
    "chown": "exit 0",
    "curl": "echo 200",
    "sleep": "exit 0",
    "npx": 'exit "${FAKE_BUILD_EXIT:-0}"',
}


def write(root: Path, name: str, text: str = "x") -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")


class WebDeploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.app, self.backups, release, stubs = (self.tmp / n for n in ("web", "backups", "release", "stubs"))
        for name, text in (("src/page.tsx", "old"), (".next/BUILD_ID", "old"), ("next.config.ts", "old"),
                           ("public/keep.txt", "old"), ("public/samples/gone.csv", "old")):
            write(self.app, name, text)
        for name, text in (("src/page.tsx", "new"), ("next.config.ts", "new"), ("public/keep.txt", "new")):
            write(release, name, text)
        self.archive = self.tmp / "web-src.tgz"
        with tarfile.open(self.archive, "w:gz") as tar:
            for name in ("src", "next.config.ts", "public"):
                tar.add(release / name, arcname=name)
        for name, body in STUBS.items():
            write(stubs, name, f"#!/usr/bin/env bash\n{body}\n")
            (stubs / name).chmod(0o755)
        script = SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n")
        for real, fake in (("/opt/ekido-web-backups", self.backups), ("/opt/ekido/web", self.app),
                           ("/var/log/ekido-deploy", self.tmp / "logs"), ("/etc/ekido/site.env", self.tmp / "no-site.env")):
            script = script.replace(real, str(fake))
        self.script = self.tmp / "deploy.sh"
        self.script.write_text(script, encoding="utf-8")
        self.path = f"{stubs}{os.pathsep}{os.environ['PATH']}"

    def run_deploy(self, build_exit: int = 0) -> subprocess.CompletedProcess:
        env = dict(os.environ, PATH=self.path, FAKE_BUILD_EXIT=str(build_exit))
        return subprocess.run(["bash", str(self.script), str(self.archive)], env=env, capture_output=True, text=True, timeout=60)

    def saved(self, pattern: str) -> list:
        return sorted(self.backups.glob(f"*/{pattern}"))

    def test_a_file_removed_from_the_repository_leaves_the_site_and_is_kept_in_the_backups(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.app / "public/samples/gone.csv").exists())
        self.assertEqual((self.app / "public/keep.txt").read_text(encoding="utf-8"), "new")
        [backup] = self.saved("public.bak-*")
        self.assertEqual((backup / "samples/gone.csv").read_text(encoding="utf-8"), "old")

    def test_a_failed_build_puts_the_previous_public_folder_back(self):
        result = self.run_deploy(build_exit=1)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.app / "public/keep.txt").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.app / "public/samples/gone.csv").read_text(encoding="utf-8"), "old")
        self.assertEqual((self.app / "src/page.tsx").read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
