"""Creates a job, funds its escrow on-chain, and starts it. Needs demo_stack.py running.

    python experiments/start_demo_job.py [--budget 5] [--window 10]

Prints the job ID for the dashboard. On Base Sepolia this spends test tokens only, from the client wallet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

from eth_account import Account
from web3 import Web3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/gpuchain_core"))
from gpuchain_core.payout_terms import job_binding, new_job_ids, provider_quote, workload_hash  # noqa: E402
LOCAL_CLIENT_KEY = "0x" + "c2" * 32  # Public demo key, for the local chain only.
LOCAL_PROVIDER_KEYS = {name: "0x" + value * 32 for name, value in {"a": "d1", "b": "d2", "c": "d3", "p": "d4"}.items()}
LOCAL_PROVIDER_RATES = {"a": "0.001", "b": "0.003", "c": "0.0005", "p": "0.002"}
ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable", "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
]


def post(base: str, path: str, body: dict, timeout: float = 600) -> dict:
    request = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def client_key(testnet: bool) -> str:
    if not testnet:
        return LOCAL_CLIENT_KEY
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CLIENT_PRIVATE_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("CLIENT_PRIVATE_KEY is missing from .env")


def local_signed_body(info: dict, budget: str, client_private_key: str) -> dict:
    """Build independently verifiable terms using public local-demo keys only."""
    chain = info["chain"]
    if info.get("testnet") or chain["chain_id"] != 31337:
        raise ValueError("automatic demo signing is restricted to local chain 31337")
    original = info["new_job_body"]
    spec = dict(original["workload"])
    if "provider_terms" in spec:
        raise ValueError("demo workload already has provider terms")
    client = Account.from_key(client_private_key).address.lower()
    job_id, onchain_id = new_job_ids(client)
    terms = []
    for name, provider_id in info["providers"].items():
        quote = provider_quote(job_id=job_id, onchain_id=onchain_id, chain_id=chain["chain_id"], contract=chain["escrow"], provider_id=provider_id, rate=LOCAL_PROVIDER_RATES[name], workload=workload_hash(spec), key=LOCAL_PROVIDER_KEYS[name])
        terms.append({"provider_id": provider_id, "wallet": quote["wallet"], "price_per_step": LOCAL_PROVIDER_RATES[name], "quote": quote})
    spec["provider_terms"] = terms
    binding = job_binding(job_id=job_id, onchain_id=onchain_id, chain_id=chain["chain_id"], contract=chain["escrow"], spec=spec, policy=original["policy"], budget=budget, key=client_private_key)
    return dict(original, workload=spec, budget_usdc=budget, job_id=job_id, client_address=client, job_binding=binding)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", default="5", help="In USDC.")
    parser.add_argument("--window", type=int, default=10, help="Dispute window in seconds. Short for a demo, longer in production.")
    args = parser.parse_args()

    info = json.loads((ROOT / "experiments" / "demo_stack_info.json").read_text(encoding="utf-8"))
    chain = info["chain"]
    w3 = Web3(Web3.HTTPProvider(chain["rpc"]))
    account = Account.from_key(client_key(info["testnet"]))
    data = json.loads((ROOT / "contracts" / "out" / "GPUChainEscrow.sol" / "GPUChainEscrow.json").read_text(encoding="utf-8"))
    escrow = w3.eth.contract(address=chain["escrow"], abi=data["abi"])
    usdc = w3.eth.contract(address=chain["usdc"], abi=ERC20_ABI)
    budget = int(Decimal(args.budget) * 1_000_000)

    balance = usdc.functions.balanceOf(account.address).call()
    if balance < budget:
        raise SystemExit(f"client {account.address} has {balance / 1e6} USDC, but the budget is {args.budget}")

    def send(call):
        tx = call.build_transaction({"from": account.address, "nonce": w3.eth.get_transaction_count(account.address, "pending"), "chainId": chain["chain_id"]})
        receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction), timeout=180)
        if receipt["status"] != 1:
            raise SystemExit("transaction reverted")
        return "0x" + receipt["transactionHash"].hex().removeprefix("0x")

    if info["testnet"]:
        raise SystemExit("Testnet demo requires separately collected signed provider quotes and a client binding; use the signed payout setup.")
    body = local_signed_body(info, args.budget, client_key(False))
    created = post(info["api"], "/jobs", body)
    job = created["job_id"]
    print(f"job {job}", flush=True)

    if info["testnet"]:
        print("approve", send(usdc.functions.approve(chain["escrow"], budget)), flush=True)
    # A local chain's clock only moves with new blocks, so it can lag far behind after sitting idle.
    now = max(w3.eth.get_block("latest")["timestamp"], int(time.time()))
    tx = send(escrow.functions.createJob(
        Web3.to_bytes(hexstr=created["onchain_id"]), bytes.fromhex(created["spec_hash"]), bytes.fromhex(created["policy_hash"]), budget, now + body["deadline_seconds"], args.window,
    ))
    print(f"funded {args.budget} USDC: {tx}", flush=True)
    if chain.get("explorer"):
        print(f"  {chain['explorer'].rstrip('/')}/tx/{tx}", flush=True)
    print("state:", post(info["api"], f"/jobs/{job}/activate", {})["state"], flush=True)
    print(f"\nLive events: {info['ws_job'].replace('<job_id>', job)}\nKill: POST {info['api']}/demo/faults {json.dumps({'job_id': job, 'kind': 'KILL_CONTAINER'})}", flush=True)


if __name__ == "__main__":
    main()
