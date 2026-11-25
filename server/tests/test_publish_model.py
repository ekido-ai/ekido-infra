import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("publish_model", Path(__file__).resolve().parents[1] / "inference" / "publish_model.py")
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class Publish(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        root = Path(self.folder.name)
        self.dirs = {"models": root / "models", "engines": root / "engines", "registry": root / "registry"}
        self.gguf = root / "model.gguf"
        self.gguf.write_bytes(b"GGUF" + b"\0" * 64)
        self.digest = hashlib.sha256(self.gguf.read_bytes()).hexdigest()
        self.commands = []

    def publish(self, model_id="shop-tickets", digest=None, up=True, mode="test"):
        return tool.publish(model_id, "job-1", self.gguf, digest or self.digest, mode, run=lambda *a: self.commands.append(a),
                            wait=lambda port: up, **self.dirs)

    def test_a_published_model_has_a_manifest_the_platform_accepts(self):
        port = self.publish()
        item = json.loads((self.dirs["registry"] / "shop-tickets.json").read_text(encoding="utf-8"))
        tool.check_manifest(item)
        self.assertEqual(item, {"job_id": "job-1", "engine_url": f"http://127.0.0.1:{port}", "engine_model": "shop-tickets", "mode": "test"})
        self.assertIn(("restart", "ekido-engine@shop-tickets"), self.commands)

    def test_a_wrong_hash_is_refused_before_anything_is_started(self):
        with self.assertRaises(ValueError):
            self.publish(digest="0" * 64)
        self.assertEqual(self.commands, [])
        self.assertFalse(self.dirs["registry"].exists() and list(self.dirs["registry"].iterdir()))

    def test_an_engine_that_does_not_start_is_never_listed(self):
        with self.assertRaises(RuntimeError):
            self.publish(up=False)
        self.assertFalse((self.dirs["registry"] / "shop-tickets.json").exists())
        self.assertIn(("disable", "--now", "ekido-engine@shop-tickets"), self.commands)

    def test_ports_are_stable_per_model_and_not_shared(self):
        first = self.publish("one")
        second = self.publish("two")
        self.assertNotEqual(first, second)
        self.assertEqual(self.publish("one"), first)

    def test_bad_names_and_files_are_refused(self):
        for model_id in ("../etc", "a b", ""):
            with self.subTest(model_id=model_id), self.assertRaises(ValueError):
                self.publish(model_id)
        self.gguf.write_bytes(b"not a model")
        with self.assertRaises(ValueError):
            self.publish(digest=hashlib.sha256(b"not a model").hexdigest())

    def test_unpublish_hides_the_model_and_deletes_nothing(self):
        self.publish()
        tool.unpublish("shop-tickets", registry=self.dirs["registry"], run=lambda *a: self.commands.append(a))
        self.assertFalse((self.dirs["registry"] / "shop-tickets.json").exists())
        self.assertTrue((self.dirs["registry"] / "shop-tickets.json.disabled").exists())
        self.assertTrue((self.dirs["models"] / "shop-tickets.gguf").exists())


if __name__ == "__main__":
    unittest.main()
