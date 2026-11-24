"""The deploy files must carry what the job-key change needs (ticket A12): pyrage installed, and a KMS wrapper chosen. A guard against losing either."""

import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]


class JobKeyDeploy(unittest.TestCase):
    def test_pyrage_is_pinned_in_the_server_requirements(self):
        text = (SERVER / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^pyrage==\d+\.\d+\.\d+$")

    def test_staging_sets_the_kms_wrapper_once_and_never_overwrites_a_choice(self):
        lines = (SERVER / "staging.sh").read_text(encoding="utf-8").splitlines()
        setting = [line for line in lines if "JOB_KEY_WRAP" in line and not line.lstrip().startswith("#")]
        self.assertEqual(len(setting), 1, setting)
        self.assertIn("grep -q '^JOB_KEY_WRAP='", setting[0])  # only when the line is missing
        self.assertIn("JOB_KEY_WRAP=kms", setting[0])
        self.assertIn(">> /etc/ekido/coordinator.env", setting[0])  # appended, never rewritten
        self.assertIn("printf 'JOB_KEY_WRAP=kms\\n'", setting[0])  # exactly this value: never empty, never local

    def test_the_wrapper_line_comes_before_the_files_are_locked_down_and_before_the_service_starts(self):
        text = (SERVER / "staging.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("JOB_KEY_WRAP"), text.index("chmod 600 /etc/ekido/coordinator.env"))
        self.assertLess(text.index("JOB_KEY_WRAP"), text.index("systemctl enable --now ekido-coordinator"))

    def test_the_runbook_tells_the_operator_what_to_do_and_what_not_to_promise(self):
        text = (SERVER.parent / "RUNBOOK.md").read_text(encoding="utf-8")
        for needle in ("pip install 'pyrage==", "JOB_KEY_WRAP=kms", "kms:Decrypt", "older database backups"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()