"""Every injectable fault, with real processes, on CPU. Safe for a laptop: no GPU, and Docker caps its memory.

Each test checks the job survives, the checkpoint chain stays intact, and the old worker really stops.
"""

import unittest

from tests.test_e2e_recovery import Cluster, job_spec

TOTAL = 4000


class Faults(Cluster):
    def start(self, providers=("a", "b")):
        ids = {}
        prices = {"a": "0.0005", "b": "0.0006", "c": "0.0007"}
        for name in providers:
            ids[name] = self.add_provider(name, 6144, prices[name], kind="REAL" if name == "a" else "CAPPED_PROFILE")
        job = self.create_job(job_spec(total=TOTAL, every=25))
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: len(self.committed(job)) >= 2, "two checkpoints", 90)
        return job, ids

    def causes(self, job):
        return [e["data"]["cause"] for e in self.events(job, "failure.detected")]

    def finish(self, job):
        self.wait_for(lambda: self.state(job) in ("VALIDATING", "COMPLETED"), "the job to finish", 300, 0.5)

    def assert_chain_intact(self, job):
        """The final checkpoint must lead back to the first through unbroken parents, with steps rising."""
        checkpoints = self.committed(job)
        by_hash = {c["manifest_hash"]: c for c in checkpoints}
        final = max(checkpoints, key=lambda c: c["logical_step"])
        self.assertEqual(final["logical_step"], TOTAL)
        chain, node = [], final
        while node:
            chain.append(node["logical_step"])
            node = by_hash.get(node["parent_hash"]) if node["parent_hash"] else None
        self.assertEqual(chain, sorted(chain, reverse=True), "steps must rise along the chain")
        self.assertLessEqual(chain[-1], 25, "the chain must reach back to the first checkpoint")

    def agent_log(self, name):
        return (self.root / f"agent_{name}.log").read_text(errors="replace")

    def inject(self, job, kind):
        response = self.post("/demo/faults", {"job_id": job, "kind": kind})
        self.assertTrue(response["delivered"], f"{kind} wasn't delivered")

    def test_oom_fails_over_and_is_labelled(self):
        job, ids = self.start()
        self.inject(job, "OOM")
        self.wait_for(lambda: self.causes(job), "detection", 30)
        detected = self.events(job, "failure.detected")[0]["data"]
        self.assertEqual((detected["cause"], detected["provider_id"], detected["injected"]), ("OOM", ids["a"], True))
        self.finish(job)
        self.assert_chain_intact(job)

    def test_crash_while_writing_a_checkpoint_resumes_from_the_one_before(self):
        job, ids = self.start()
        self.inject(job, "CRASH_MID_CHECKPOINT")
        self.wait_for(lambda: self.causes(job), "detection", 30)
        self.assertEqual(self.causes(job)[0], "PROCESS_EXIT")
        self.finish(job)
        self.assert_chain_intact(job)

    def test_corrupt_checkpoint_is_refused_and_fails_over(self):
        job, ids = self.start()
        self.inject(job, "CORRUPT_CHECKPOINT")
        self.wait_for(lambda: self.causes(job), "detection", 30)
        self.assertEqual(self.causes(job)[0], "BAD_CHECKPOINT")
        rules = [e["data"]["rule"] for e in self.events(job, "failure.warning")]
        self.assertIn("CHECKPOINT_UPLOAD_FAILED", rules)
        self.finish(job)
        self.assert_chain_intact(job)

    def test_disconnect_fails_over_and_the_old_worker_is_stopped(self):
        job, ids = self.start()
        self.inject(job, "DISCONNECT")
        self.wait_for(lambda: self.causes(job), "detection", 30)
        self.assertEqual(self.causes(job)[0], "DISCONNECT")
        self.finish(job)
        self.assert_chain_intact(job)
        # Either its next checkpoint is refused (its upload token died with the socket) or its next message is fenced.
        self.wait_for(lambda: "stopping run" in self.agent_log("a"), "the old worker to stop", 30)
        self.wait_for(lambda: self.provider(ids["a"])["status"] == "ONLINE", "the agent to reconnect", 30)

    def test_silent_worker_is_declared_dead_and_fenced_when_it_returns(self):
        job, ids = self.start()
        self.inject(job, "HEARTBEAT_LOSS")
        self.wait_for(lambda: self.causes(job), "detection", 30)
        self.assertEqual(self.causes(job)[0], "HEARTBEAT_LOST")
        self.finish(job)
        self.assert_chain_intact(job)
        # When its 25 s of silence ends, its late writes are refused and it must kill its own run.
        self.wait_for(lambda: "stopping run" in self.agent_log("a"), "the fenced worker to stop", 60)
        self.assertEqual(len(self.causes(job)), 1, "its late messages mustn't look like a new failure")

    def test_thermal_warning_saves_a_checkpoint_without_failing_over(self):
        job, ids = self.start()
        self.inject(job, "THERMAL_WARNING")
        self.wait_for(lambda: self.events(job, "failure.warning"), "the warning", 20)
        warning = self.events(job, "failure.warning")[0]["data"]
        self.assertEqual((warning["rule"], warning["injected"]), ("THERMAL", True))
        self.finish(job)
        self.assertEqual(self.causes(job), [], "a warning isn't a failure")
        self.assertTrue([c for c in self.committed(job) if c["logical_step"] % 25], "the warning should force an extra checkpoint")
        self.assert_chain_intact(job)

    def test_faults_reach_the_replacement_too(self):
        job, ids = self.start(("a", "b", "c"))
        self.inject(job, "OOM")
        self.wait_for(lambda: self.events(job, "training.progress")[-1]["data"]["provider_id"] == ids["b"], "B to take over", 60)
        self.inject(job, "OOM")
        self.wait_for(lambda: len(self.causes(job)) >= 2, "the second failure", 30)
        self.assertEqual([e["data"]["provider_id"] for e in self.events(job, "failure.detected")], [ids["a"], ids["b"]])
        self.finish(job)
        self.assert_chain_intact(job)


if __name__ == "__main__":
    unittest.main()
