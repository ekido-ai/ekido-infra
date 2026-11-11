"""Settlement on a real local EVM (Anvil): real transactions, with verdicts signed by the Python code.

Needs the contracts built (`forge build`), web3 in the Python environment, and anvil from
infra/setup-host.sh or from Docker. From the repo root:
    set PYTHONPATH=apps/coordinator;packages/gpuchain_core;workers/verifier
    python -m unittest tests/test_e2e_chain.py
"""

import json
import subprocess
import time
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from web3 import Web3
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gpuchain_core.settlement import build_verdict
from gpuchain_core.signing import order_signatures, sign_document, sign_verdict, verdict_digest
from gpuchain_core.canonical import document_hash
from gpuchain_core.payout_terms import job_binding, new_job_ids, provider_quote, workload_hash
from gpuchain_coordinator.chain import ChainError, EscrowClient
from gpuchain_coordinator.models import Base, Job
from gpuchain_coordinator.settlement_service import SettlementService
from gpuchain_verifier.trusted_chain import ChainReadError, read_chain_context
from gpuchain_verifier.worker import Verifier, verdict_from_inputs

ROOT = Path(__file__).resolve().parents[1]
PORT = 18545
RPC = f"http://127.0.0.1:{PORT}"
ANVIL = "gpuchain-anvil-test"
# Anvil's first default account. A public test key with no value anywhere.
DEPLOYER = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
COORDINATOR, CLIENT, ARBITER = "0x" + "c1" * 32, "0x" + "c2" * 32, "0x" + "c3" * 32
VERIFIERS = ["0x" + c * 64 for c in "abc"]
A, B = Account.from_key("0x" + "d1" * 32).address, Account.from_key("0x" + "d2" * 32).address
BUDGET = Decimal("5")
WINDOW = 5


def artifact(name):
    return json.loads((ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json").read_text(encoding="utf-8"))


class Chain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # anvil from infra/setup-host.sh needs no Docker. Otherwise the Foundry image provides it.
        local = next((p for p in (ROOT / ".cache/foundry/anvil.exe", ROOT / ".cache/foundry/anvil") if p.is_file()), None)
        cls.local_anvil = subprocess.Popen([str(local), "--port", str(PORT), "--silent"]) if local else None
        if not local:
            subprocess.run(["docker", "rm", "-f", ANVIL], capture_output=True, check=False)
            subprocess.run(
                ["docker", "run", "-d", "--rm", "--name", ANVIL, "-p", f"127.0.0.1:{PORT}:8545", "--memory", "256m", "--entrypoint", "anvil", "ghcr.io/foundry-rs/foundry:latest", "--host", "0.0.0.0", "--silent"],
                check=True, capture_output=True,
            )
        cls.w3 = Web3(Web3.HTTPProvider(RPC))
        for _ in range(60):
            try:
                if cls.w3.is_connected():
                    break
            except Exception:  # Still starting.
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("anvil didn't start")
        cls.deployer = Account.from_key(DEPLOYER)
        for key in (COORDINATOR, CLIENT, ARBITER):
            cls.w3.provider.make_request("anvil_setBalance", [Account.from_key(key).address, hex(10**20)])
        cls.usdc = cls.deploy("MockUSDC", [])
        verifiers = [Account.from_key(k).address for k in VERIFIERS]
        cls.escrow = cls.deploy("GPUChainEscrow", [cls.usdc.address, cls.deployer.address, Account.from_key(COORDINATOR).address, Account.from_key(ARBITER).address, verifiers, 2, 1_000_000])
        cls.chain_id = cls.w3.eth.chain_id
        cls.client = EscrowClient(RPC, cls.escrow.address, COORDINATOR)
        cls.send(cls.usdc.functions.mint(Account.from_key(CLIENT).address, 1_000_000_000), DEPLOYER)
        cls.send(cls.usdc.functions.approve(cls.escrow.address, 2**255), CLIENT)
        cls.counter = 0

    @classmethod
    def tearDownClass(cls):
        if cls.local_anvil:
            cls.local_anvil.kill()
            cls.local_anvil.wait(timeout=10)
        else:
            subprocess.run(["docker", "rm", "-f", ANVIL], capture_output=True, check=False)

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
        if receipt["status"] != 1:
            raise AssertionError("transaction reverted")
        return receipt

    def new_job(self):
        Chain.counter += 1
        job_id = "0x" + f"{Chain.counter:064x}"
        now = self.w3.eth.get_block("latest")["timestamp"]
        self.send(self.escrow.functions.createJob(Web3.to_bytes(hexstr=job_id), b"\x01" * 32, b"\x02" * 32, 5_000_000, now + 3600, WINDOW), CLIENT)
        return job_id

    def passing_verdict(self, job_id, reports=None):
        now = self.w3.eth.get_block("latest")["timestamp"]
        return build_verdict(
            onchain_job_id=job_id, passed=True, final_checkpoint_hash="1" * 64, lineage=["1" * 64],
            reports=reports or [{"verdict": "PASS", "signer": "x"}], payouts={A: Decimal("0.15"), B: Decimal("0.75")},
            refund=Decimal("4.1"), budget=BUDGET, expiry=now + 3600,
        )

    def signatures(self, verdict, keys=VERIFIERS[:2]):
        return order_signatures(verdict, self.chain_id, self.escrow.address, [sign_verdict(verdict, self.chain_id, self.escrow.address, k) for k in keys])

    def advance(self, seconds):
        self.w3.provider.make_request("evm_increaseTime", [seconds])
        self.w3.provider.make_request("evm_mine", [])

    def test_python_and_contract_compute_the_same_digest(self):
        verdict = self.passing_verdict(self.new_job())
        self.assertEqual(self.client.verdict_digest(verdict), verdict_digest(verdict, self.chain_id, self.escrow.address))

    def test_independent_chain_reader_checks_live_escrow_terms_and_verifiers(self):
        job = self.new_job()
        self.client.ensure_active(job)
        signer_addresses = [Account.from_key(key).address for key in VERIFIERS[:2]]
        facts = read_chain_context(RPC, self.chain_id, self.escrow.address, job, signer_addresses)
        self.assertEqual(facts["budget"], "5")
        self.assertEqual(facts["client"], Account.from_key(CLIENT).address.lower())
        self.assertEqual(facts["spec_hash"], "01" * 32)
        self.assertEqual(facts["policy_hash"], "02" * 32)
        self.assertEqual(facts["quorum"], 2)
        self.assertEqual(set(facts["verifiers"]), {a.lower() for a in signer_addresses})
        with self.assertRaises(ChainReadError):
            read_chain_context(RPC, self.chain_id + 1, self.escrow.address, job, signer_addresses)

    def test_timeout_refund_uses_chain_deadline_and_is_idempotent(self):
        job = self.new_job()
        self.client.ensure_active(job)
        escrow_before = self.usdc.functions.balanceOf(self.escrow.address).call()
        client_before = self.escrow.functions.withdrawable(Account.from_key(CLIENT).address).call()
        self.assertFalse(self.client.timeout_refund_status(job)["eligible"])
        with self.assertRaises(ChainError):
            self.client.ensure_timeout_refund(job)
        self.advance(7201)
        self.assertTrue(self.client.timeout_refund_status(job)["eligible"])
        self.assertEqual(self.client.ensure_timeout_refund(job).status, "CONFIRMED")
        self.assertEqual(self.client.state(job), "Refunded")
        self.assertEqual(self.escrow.functions.withdrawable(Account.from_key(CLIENT).address).call() - client_before, 5_000_000)
        self.assertEqual(self.usdc.functions.balanceOf(self.escrow.address).call(), escrow_before)
        self.assertEqual(self.client.ensure_timeout_refund(job).status, "ALREADY_DONE")

    def test_pending_settlement_restores_after_restart_against_real_chain(self):
        onchain_id = self.new_job()
        self.client.ensure_active(onchain_id)
        engine = create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            job = Job(onchain_id=onchain_id, spec={}, spec_hash="01" * 32, policy={}, policy_hash="02" * 32, budget=Decimal("5"), deadline=datetime.now(timezone.utc) + timedelta(hours=1), state="FAILED")
            session.add(job)
            session.flush()
            job_id = job.id
            session.commit()
        addresses = {Account.from_key(key).address for key in VERIFIERS}
        first = SettlementService(engine, None, self.client, self.chain_id, self.escrow.address, addresses, 2, None)
        verdict = first.propose(job_id)
        restarted = SettlementService(engine, None, self.client, self.chain_id, self.escrow.address, addresses, 2, None)
        self.assertEqual(restarted.restore_pending(), 1)
        self.assertEqual(restarted.tasks[job_id].verdict, verdict)
        self.assertEqual(restarted.claim(next(iter(addresses)))["onchain_job_id"], onchain_id)
        self.assertEqual(self.client.state(onchain_id), "Active")

    def test_checked_worker_signatures_settle_real_escrow_and_refuse_forgery(self):
        def signed(body, key):
            return dict(body, signature=sign_document(body, key)[1])

        client = Account.from_key(CLIENT).address.lower()
        provider_key, worker_key = "0x" + "d1" * 32, "0x" + "e1" * 32
        provider, worker = Account.from_key(provider_key).address.lower(), Account.from_key(worker_key).address.lower()
        job_id, onchain_id = new_job_ids(client)
        contract = self.escrow.address.lower()
        quote = provider_quote(job_id=job_id, onchain_id=onchain_id, chain_id=self.chain_id, contract=contract, provider_id="provider", rate="0.001", workload=workload_hash({"total_logical_steps": 50}), key=provider_key)
        spec = {"total_logical_steps": 50, "provider_terms": [{"provider_id": "provider", "wallet": provider, "price_per_step": "0.001", "quote": quote}]}
        policy = {"metric": "accuracy", "eval_set_hash": "e" * 64, "eval_version_hash": "f" * 64}
        binding = job_binding(job_id=job_id, onchain_id=onchain_id, chain_id=self.chain_id, contract=contract, spec=spec, policy=policy, budget="5", key=CLIENT)
        now = self.w3.eth.get_block("latest")["timestamp"]
        self.send(self.escrow.functions.createJob(Web3.to_bytes(hexstr=onchain_id), bytes.fromhex(document_hash(spec)), bytes.fromhex(document_hash(policy)), 5_000_000, now + 3600, WINDOW), CLIENT)
        self.client.ensure_active(onchain_id)
        manifest = signed({"job_id": job_id, "immutable_hash": document_hash(spec), "logical_step": 50, "parent_manifest_hash": None, "signer": worker}, worker_key)
        digest = document_hash(manifest)
        delegation = signed({"kind": "gpuchain.enrollment.v1", "provider_id": "provider", "worker_key": worker}, provider_key)
        reports = [signed({"job_id": job_id, "checkpoint_hash": digest, "reference_checkpoint_hash": None, "policy_hash": document_hash(policy), "eval_set_hash": policy["eval_set_hash"], "eval_version_hash": policy["eval_version_hash"], "is_final": True, "verdict": "PASS", "signer": Account.from_key(key).address.lower()}, key) for key in VERIFIERS[:2]]
        inputs = {"job_id": job_id, "onchain_job_id": onchain_id, "chain_id": self.chain_id, "contract": contract, "job_binding": binding, "passed": True, "final_checkpoint_hash": digest, "lineage": [digest], "reports": reports, "payout_evidence": {"manifests": [manifest], "delegations": {worker: {"provider_id": "provider", "signature": delegation["signature"]}}}, "payouts": {provider: "0.05"}, "refund": "4.95", "budget": "5", "expiry": now + 3600}
        signatures = []
        for key, report in zip(VERIFIERS[:2], reports):
            verifier = Verifier("http://unused", key, "cpu", 512, rpc_url=RPC, chain_id=self.chain_id, escrow_address=contract)
            report_key = (job_id, digest)
            verifier.final_context[report_key] = (spec, policy)
            verifier.final_reports[report_key] = document_hash(report)
            forged = dict(inputs, payouts={"0x" + "9" * 40: "5"}, refund="0")
            self.assertIsNone(verifier.sign_verdict_task(forged))
            signature = verifier.sign_verdict_task(inputs)
            self.assertIsNotNone(signature)
            signatures.append(signature)
        verdict = verdict_from_inputs(inputs)
        self.client.ensure_attested(verdict, order_signatures(verdict, self.chain_id, contract, signatures))
        self.advance(WINDOW + 1)
        self.client.ensure_finalized(onchain_id)
        self.assertEqual(self.client.state(onchain_id), "Settled")

    def test_full_settlement_pays_exactly(self):
        job = self.new_job()
        self.assertEqual(self.client.state(job), "Funded")
        self.assertEqual(self.client.ensure_active(job).status, "CONFIRMED")
        self.assertEqual(self.client.anchor(job, 1, 2, "0x" + "ab" * 32).status, "CONFIRMED")

        verdict = self.passing_verdict(job)
        result = self.client.ensure_attested(verdict, self.signatures(verdict))
        self.assertRegex(result.tx_hash, r"^0x[0-9a-f]{64}$")
        self.assertEqual(self.client.state(job), "Attested")

        self.advance(WINDOW + 1)
        self.client.ensure_finalized(job)
        self.assertEqual(self.client.state(job), "Settled")

        before = self.escrow.functions.withdrawable(A).call()
        self.assertGreaterEqual(before, 150_000)
        self.w3.provider.make_request("anvil_setBalance", [A, hex(10**20)])
        self.w3.provider.make_request("anvil_impersonateAccount", [A])
        tx = self.escrow.functions.withdraw().transact({"from": A})
        self.w3.eth.wait_for_transaction_receipt(tx)
        self.assertEqual(self.usdc.functions.balanceOf(A).call(), before)
        self.assertEqual(self.escrow.functions.withdrawable(A).call(), 0)

    def test_every_step_is_safe_to_retry(self):
        job = self.new_job()
        self.client.ensure_active(job)
        self.assertEqual(self.client.ensure_active(job).status, "ALREADY_DONE")
        verdict = self.passing_verdict(job)
        signatures = self.signatures(verdict)
        self.client.ensure_attested(verdict, signatures)
        self.assertEqual(self.client.ensure_attested(verdict, signatures).status, "ALREADY_DONE")
        self.advance(WINDOW + 1)
        self.client.ensure_finalized(job)
        paid = self.escrow.functions.withdrawable(B).call()
        self.assertEqual(self.client.ensure_finalized(job).status, "ALREADY_DONE")
        self.assertEqual(self.escrow.functions.withdrawable(B).call(), paid)

    def test_verifier_order_does_not_change_the_verdict_they_sign(self):
        job = self.new_job()
        self.client.ensure_active(job)
        reports = [{"verdict": "PASS", "signer": "1"}, {"verdict": "PASS", "signer": "2"}]
        first, second = self.passing_verdict(job, reports), self.passing_verdict(job, reports[::-1])
        second["expiry"] = first["expiry"]
        self.assertEqual(first, second)

    def test_one_signature_is_refused_by_the_contract(self):
        job = self.new_job()
        self.client.ensure_active(job)
        verdict = self.passing_verdict(job)
        with self.assertRaises(Exception):
            self.client.ensure_attested(verdict, self.signatures(verdict, VERIFIERS[:1]))
        self.assertEqual(self.client.state(job), "Active")

    def test_failed_job_refunds_the_client_fully(self):
        job = self.new_job()
        self.client.ensure_active(job)
        before = self.escrow.functions.withdrawable(Account.from_key(CLIENT).address).call()
        now = self.w3.eth.get_block("latest")["timestamp"]
        verdict = build_verdict(onchain_job_id=job, passed=False, final_checkpoint_hash=None, lineage=[], reports=[{"verdict": "FAIL", "signer": "x"}], payouts={}, refund=BUDGET, budget=BUDGET, expiry=now + 3600)
        self.client.ensure_attested(verdict, self.signatures(verdict))
        self.advance(WINDOW + 1)
        self.client.ensure_finalized(job)
        self.assertEqual(self.client.state(job), "Refunded")
        self.assertEqual(self.escrow.functions.withdrawable(Account.from_key(CLIENT).address).call() - before, 5_000_000)

    def test_finalizing_early_is_refused(self):
        job = self.new_job()
        self.client.ensure_active(job)
        verdict = self.passing_verdict(job)
        self.client.ensure_attested(verdict, self.signatures(verdict))
        with self.assertRaises(Exception):
            self.client.ensure_finalized(job)
        self.assertEqual(self.client.state(job), "Attested")

    def test_cannot_finalize_a_job_with_no_verdict(self):
        job = self.new_job()
        self.client.ensure_active(job)
        with self.assertRaises(ChainError):
            self.client.ensure_finalized(job)


if __name__ == "__main__":
    unittest.main()
