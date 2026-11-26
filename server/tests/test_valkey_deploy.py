"""The Valkey settings the cache depends on (ekido-internal #142). A guard against losing one of them."""

import unittest
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]


class ValkeyDeploy(unittest.TestCase):
    def setUp(self):
        self.script = (SERVER / "valkey.sh").read_text(encoding="utf-8")

    def test_it_is_local_capped_and_not_written_to_disk(self):
        for needle in (r"bind 127.0.0.1\n", r"maxmemory 256mb\n", r"maxmemory-policy allkeys-lru\n", r'save ""\n', r"appendonly no\n", "MemoryMax=384M"):
            self.assertIn(needle, self.script)
        self.assertNotIn("0.0.0.0", self.script)

    def test_it_needs_a_password_that_is_never_printed_or_passed_as_an_argument(self):
        self.assertIn("requirepass", self.script)
        self.assertIn("chmod 600 /etc/ekido/valkey.env", self.script)
        self.assertIn("REDISCLI_AUTH", self.script)
        self.assertNotRegex(self.script, r"valkey-cli[^\n]*\s-a\s")
        self.assertNotRegex(self.script, r"(?m)^\s*echo[^\n]*\$(password|VALKEY_URL|REDISCLI_AUTH)")

    def test_the_platform_unit_reads_the_url_and_still_starts_without_it(self):
        unit = (SERVER / "systemd" / "ekido-platform.service").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=-/etc/ekido/valkey.env", unit)  # the leading - makes it optional

    def test_the_runbook_has_the_note(self):
        text = (SERVER.parent / "RUNBOOK.md").read_text(encoding="utf-8")
        for needle in ("## Cache (Valkey)", "valkey.sh", "pip install 'redis>=5,<7'", "30 seconds"):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
