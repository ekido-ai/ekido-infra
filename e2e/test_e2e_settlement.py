"""The complete product on a local EVM: real processes, real kills, real signatures, real transactions.

Runs on CPU with the synthetic workload. infra/run-tests.sh starts Anvil and runs this in the test image.
"""

import json
import os
import subprocess
import sys
import time
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from web3 import Web3

from ekido_core.payout_terms import job_binding, new_job_ids, provider_quote, workload_hash
from ekido_verifier.evaluate import load_eval_set
from tests.test_e2e_recovery import PYTHONPATH, ROOT, WALLETS, Cluster, job_spec

RPC = os.environ.get("ANVIL_RPC", "http://host.docker.internal:18545")
# Anvil's first default account. A public test key with no value anywhere.
DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
COORDINATOR, CLIENT, ARBITER = "0x" + "e1" * 32, "0x" + "e2" * 32, "0x" + "e3" * 32
VERIFIERS = ["0x" + c * 64 for c in "abc"]
BOND = 1_000_000
WINDOW = 3
BUDGET = 5_000_000


def artifact(name):
    return json.loads((ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))


class Settlement(Cluster):
    @classmethod
    def setUpClass(cls):
        cls.w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))
        for key in (COORDINATOR, CLIENT, ARBITER, *WALLETS.values()):
            cls.w3.provider.make_request("anvil_setBalance", [Account.from_key(key).address, hex(10**20)])
        cls.usdc = cls.deploy("MockUSDC", [])
        addresses = [Account.from_key(k).address for k in VERIFIERS]
        cls.escrow = cls.deploy("GPUChainEscrow", [cls.usdc.address, Account.from_key(DEPLOYER).address, Account.from_key(COORDINATOR).address, Account.from_key(ARBITER).address, addresses, 2, BOND])
        cls.send(cls.usdc.functions.mint(Account.from_key(CLIENT).address, 10**12), DEPLOYER)
        cls.send(cls.usdc.functions.approve(cls.escrow.address, 2**255), CLIENT)
        cls.server_env = {
            "VERIFIER_ADDRESSES": ",".join(addresses),
            "VERIFIER_QUORUM": "2",
            "RPC_URL": RPC,
            "CHAIN_ID": str(cls.w3.eth.chain_id),
            "ESCROW_ADDRESS": cls.escrow.address,
            "COORDINATOR_PRIVATE_KEY": COORDINATOR,
        }

    @classmethod
    def deploy(cls, name, args):
        data = artifact(name)
        factory = cls.w3.eth.contract(abi=data["abi"], bytecode=data["bytecode"]["object"])
        return cls.w3.eth.contract(address=cls.send(factory.constructor(*args), DEPLOYER)["contractAddress"], abi=data["abi"])

    @classmethod
    def send(cls, call, key):
        account = Account.from_key(key)
        tx = call.build_transaction({"from": account.address, "nonce": cls.w3.eth.get_transaction_count(account.address), "chainId": cls.w3.eth.chain_id})
        receipt = cls.w3.eth.wait_for_transaction_receipt(cls.w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction))
        assert receipt["status"] == 1, "transaction reverted"
        return receipt

    def setUp(self):
        super().setUp()
        self.spec = job_spec(total=400, every=25)
        self.policy = {
            "schema_version": 1, "metric": "accuracy", "min_absolute": 0.5, "non_inferiority_margin": 0.02, "confidence": 0.95, "bootstrap_resamples": 500,
            "eval_set_hash": load_eval_set(self.spec, 512).hash, "eval_version_hash": "e" * 64, "probation_steps": 50, "probation_margin": 0.05,
        }
        for index, key in enumerate(VERIFIERS[:2]):
            log = open(self.root / f"verifier_{index}.log", "wb")
            self.addCleanup(log.close)
            command = [sys.executable, "-m", "ekido_verifier.worker", "--coordinator", self.base, "--key", key, "--device", "cpu", "--eval-examples", "512"]
            if self._testMethodName.startswith("test_secure_"):
                command += ["--rpc-url", RPC, "--chain-id", str(self.w3.eth.chain_id), "--escrow-address", self.escrow.address, "--state-dir", str(self.root / f"verifier_state_{index}")]
            else:
                command += ["--unsafe-local-demo-verdicts"]
            self.processes[f"verifier_{index}"] = subprocess.Popen(
                command,
                env={**os.environ, "PYTHONPATH": PYTHONPATH, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT,
            )

    def new_job(self):
        return self.post("/jobs", {"workload": self.spec, "policy": self.policy, "budget_usdc": "5", "deadline_seconds": 1800})

    def fund(self, created, *, budget=BUDGET, spec_hash=None, seconds=7200):
        # The latest block can be minutes old if the last test ran long, and a deadline must be ahead of the next one.
        now = max(self.w3.eth.get_block("latest")["timestamp"], int(time.time()))
        self.send(
            self.escrow.functions.createJob(
                Web3.to_bytes(hexstr=created["onchain_id"]), bytes.fromhex(spec_hash or created["spec_hash"]), bytes.fromhex(created["policy_hash"]), budget, now + seconds, WINDOW
            ),
            CLIENT,
        )

    def onchain_state(self, created):
        states = ("None", "Funded", "Active", "Attested", "Disputed", "Settled", "Refunded", "Cancelled")
        return states[self.escrow.functions.getJob(Web3.to_bytes(hexstr=created["onchain_id"])).call()[2]]

    def owed(self, key):
        return self.escrow.functions.withdrawable(Account.from_key(key).address).call()

    def activate_refused(self, job_id):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post(f"/jobs/{job_id}/activate")
        return caught.exception.code, caught.exception.read().decode()

    def test_secure_real_workers_settle_after_provider_handover(self):
        providers = [("a", self.add_provider("a", 6144, "0.001", kind="REAL"), "0.001"), ("b", self.add_provider("b", 6144, "0.003"), "0.003")]
        before = {name: self.owed(WALLETS[name]) for name, _, _ in providers}
        client = Account.from_key(CLIENT).address.lower()
        job_id, onchain_id = new_job_ids(client)
        contract = self.escrow.address.lower()
        terms = []
        for name, provider_id, rate in providers:
            quote = provider_quote(job_id=job_id, onchain_id=onchain_id, chain_id=self.w3.eth.chain_id, contract=contract, provider_id=provider_id, rate=rate, workload=workload_hash(self.spec), key=WALLETS[name])
            terms.append({"provider_id": provider_id, "wallet": quote["wallet"], "price_per_step": rate, "quote": quote})
        spec = dict(self.spec, provider_terms=terms)
        binding = job_binding(job_id=job_id, onchain_id=onchain_id, chain_id=self.w3.eth.chain_id, contract=contract, spec=spec, policy=self.policy, budget="5", key=CLIENT)
        created = self.post("/jobs", {"job_id": job_id, "client_address": client, "job_binding": binding, "workload": spec, "policy": self.policy, "budget_usdc": "5", "deadline_seconds": 1800})
        self.fund(created)
        self.post(f"/jobs/{job_id}/activate")
        self.wait_for(lambda: len(self.committed(job_id)) >= 4, "secure job checkpoints", 120)
        self.post("/demo/faults", {"job_id": job_id, "kind": "KILL_CONTAINER"})
        self.wait_for(lambda: self.state(job_id) == "SETTLED", "secure settlement after handover", 360)
        self.assertEqual(self.onchain_state(created), "Settled")
        steps = self.get(f"/jobs/{job_id}/settlement-preview")["steps"]
        for name, _, rate in providers:
            self.assertGreater(steps.get(name, 0), 0)
            self.assertEqual(self.owed(WALLETS[name]) - before[name], int(Decimal(rate) * 1_000_000) * steps[name])

    def test_a_short_on_chain_deadline_is_refused_before_any_work_starts(self):
        """Arvind's F1: the client could otherwise take a refund after the work is done."""
        self.add_provider("a", 6144, "0.001", kind="REAL")
        created = self.new_job()  # The job allows 1800 s.
        self.fund(created, seconds=120)
        code, body = self.activate_refused(created["job_id"])
        self.assertEqual(code, 409)
        self.assertIn("deadline", body)
        self.assertEqual(self.onchain_state(created), "Funded", "no work, so the client can still just cancel")

    def test_the_whole_story_settles_exactly_on_chain(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        self.add_provider("c", 6144, "0.002", kind="INJECTED_FAULT", fault={"kind": "POISON", "at_step": 0})
        self.add_provider("b", 6144, "0.003")
        before = {k: self.owed(WALLETS[k]) for k in "abc"}
        client_before = self.owed(CLIENT)

        created = self.new_job()
        job = created["job_id"]
        self.fund(created)
        self.assertEqual(self.onchain_state(created), "Funded")
        self.post(f"/jobs/{job}/activate")
        self.assertEqual(self.onchain_state(created), "Active")

        self.wait_for(lambda: len(self.committed(job)) >= 4, "A's checkpoints", 120)
        self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        self.wait_for(lambda: self.events(job, "checkpoint.rejected"), "the cheater to be rejected", 240)
        self.wait_for(lambda: self.state(job) == "SETTLED", "settlement on-chain", 360)

        self.assertEqual(self.onchain_state(created), "Settled")
        steps = self.get(f"/jobs/{job}/settlement-preview")["steps"]
        earned = {k: self.owed(WALLETS[k]) - before[k] for k in "abc"}
        self.assertEqual(earned["c"], 0, "the provider that cheated must earn nothing")
        self.assertGreater(earned["a"], 0)
        self.assertGreater(earned["b"], 0)
        self.assertEqual(earned["a"], steps["a"] * 1_000, "0.001 USDC a step for every accepted step")
        self.assertEqual(earned["b"], steps["b"] * 3_000, "0.003 USDC a step for every accepted step")
        self.assertEqual(steps["a"] + steps["b"], 400)
        self.assertEqual(self.owed(CLIENT) - client_before, BUDGET - earned["a"] - earned["b"], "the rest goes back to the client")

        balance = self.usdc.functions.balanceOf(self.escrow.address).call()
        self.assertEqual(balance, self.escrow.functions.totalLocked().call() + self.escrow.functions.totalWithdrawable().call())

        txs = [e["data"] for e in self.events(job, "chain.tx")]
        self.assertEqual({t["action"] for t in txs} >= {"markActive", "anchorEvent", "submitAttestation", "finalize"}, True, msg=str(txs))
        self.assertTrue(all(t["local_chain"] for t in txs))
        self.assertEqual(self.get(f"/jobs/{job}/settlement")["status"], "SETTLED")

    def test_a_provider_whose_agent_restarted_is_still_paid_for_its_earlier_work(self):
        """A restarted agent generates a new worker key. Work signed with the old one still counts."""
        provider_a = self.add_provider("a", 6144, "0.001", kind="REAL")
        self.add_provider("b", 6144, "0.003")
        before = {k: self.owed(WALLETS[k]) for k in "ab"}
        created = self.new_job()
        job = created["job_id"]
        self.fund(created)
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: len(self.committed(job)) >= 4, "A's checkpoints", 120)
        old_key = self.provider(provider_a)["enrolled_key"]

        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.processes["a"].pid)], capture_output=True, check=False)
        self.processes["a"].kill()
        self.processes["a"].wait(timeout=10)
        log = open(self.root / "agent_a_restarted.log", "wb")
        self.addCleanup(log.close)
        self.processes["a"] = subprocess.Popen(
            [sys.executable, "-m", "ekido_agent.agent", "--coordinator", f"ws://127.0.0.1:{self.port}", "--provider-id", provider_a, "--wallet-key", WALLETS["a"], "--workdir", str(self.root / "agent_a2"), "--mode", "process", "--device", "cpu"],
            env={**os.environ, "PYTHONPATH": PYTHONPATH, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=log, stderr=subprocess.STDOUT,
        )
        self.wait_for(lambda: self.provider(provider_a)["enrolled_key"] != old_key, "A's agent to come back with a new key", 60)

        self.wait_for(lambda: self.state(job) == "SETTLED", "settlement on-chain", 300)
        steps = self.get(f"/jobs/{job}/settlement-preview")["steps"]
        earned = {k: self.owed(WALLETS[k]) - before[k] for k in "ab"}
        self.assertGreater(earned["a"], 0, "A's accepted work was signed with its old key")
        self.assertEqual(earned["a"], steps["a"] * 1_000)
        self.assertEqual(earned["b"], steps.get("b", 0) * 3_000)

    def test_no_funds_no_work(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        created = self.new_job()
        code, detail = self.activate_refused(created["job_id"])
        self.assertEqual(code, 409)
        self.assertIn("createJob", detail)
        self.assertEqual(self.state(created["job_id"]), "OPEN")

    def test_escrow_with_wrong_terms_or_budget_is_refused(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        created = self.new_job()
        self.fund(created, budget=1)
        self.assertIn("different budget", self.activate_refused(created["job_id"])[1])

        other = self.new_job()
        self.fund(other, spec_hash="f" * 64)
        self.assertIn("different job terms", self.activate_refused(other["job_id"])[1])

    def test_failed_job_refunds_the_client_in_full(self):
        self.add_provider("a", 6144, "0.001", kind="REAL")
        before, client_before = self.owed(WALLETS["a"]), self.owed(CLIENT)
        created = self.new_job()
        job = created["job_id"]
        self.fund(created)
        self.post(f"/jobs/{job}/activate")
        self.wait_for(lambda: self.committed(job), "a checkpoint", 120)
        self.post("/demo/faults", {"job_id": job, "kind": "KILL_CONTAINER"})
        self.wait_for(lambda: self.state(job) == "REFUNDED", "the refund on-chain", 240)
        self.assertEqual(self.onchain_state(created), "Refunded")
        self.assertEqual(self.owed(CLIENT) - client_before, BUDGET)
        self.assertEqual(self.owed(WALLETS["a"]), before)


if __name__ == "__main__":
    unittest.main()
