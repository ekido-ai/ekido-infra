"""End-to-end check of on-chain provider listing against a throwaway local Anvil chain.

Deploys ProviderRegistry from contracts/out, then lists a provider through the same code the provider kit
uses (gpuchain_agent.onchain), re-runs it, changes the price, and tries a wallet with no gas.
Nothing touches Base Sepolia. Run: python experiments/registry_listing_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / "gpuchain_core"), str(ROOT / "workers" / "agent"), str(ROOT / "workers" / "runtime")]

from eth_account import Account  # noqa: E402
from eth_abi import encode  # noqa: E402

from gpuchain_agent.onchain import Rpc, list_on_chain, read_listing, wait_receipt  # noqa: E402
from gpuchain_core.provider_meta import metadata_hash, provider_metadata  # noqa: E402

ANVIL = ROOT / ".cache" / "foundry" / ("anvil.exe" if sys.platform == "win32" else "anvil")
PORT = 8547
# Anvil's well-known development accounts, public test keys that only exist on a local chain.
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
PROVIDER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def deploy(rpc: Rpc) -> str:
    artifact = json.loads((ROOT / "contracts/out/ProviderRegistry.sol/ProviderRegistry.json").read_text())
    owner = Account.from_key(OWNER_KEY).address
    data = artifact["bytecode"]["object"] + encode(["address", "address"], [owner, owner]).hex()
    tx = {"type": 2, "chainId": 31337, "nonce": int(rpc.call("eth_getTransactionCount", [owner, "pending"]), 16), "value": 0, "data": data,
          "gas": 2_000_000, "maxFeePerGas": 10**10, "maxPriorityFeePerGas": 10**9}
    signed = Account.sign_transaction(tx, OWNER_KEY)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    receipt = wait_receipt(rpc, rpc.call("eth_sendRawTransaction", ["0x" + bytes(raw).hex()]), 30)
    return receipt["contractAddress"]


def main() -> int:
    node = subprocess.Popen([str(ANVIL), "--port", str(PORT), "--silent"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        rpc = Rpc(f"http://127.0.0.1:{PORT}")
        for _ in range(50):
            try:
                rpc.call("eth_chainId", [])
                break
            except Exception:
                time.sleep(0.2)
        registry = deploy(rpc)
        wallet = Account.from_key(PROVIDER_KEY).address
        first = metadata_hash(provider_metadata("Local RTX 3060", wallet, "0.000004", 6144))
        results = []

        out = list_on_chain(rpc, 31337, registry, PROVIDER_KEY, first)
        results.append(("first join registers", out.status == "registered" and read_listing(rpc, registry, wallet) == (True, first)))
        out = list_on_chain(rpc, 31337, registry, PROVIDER_KEY, first)
        results.append(("joining again sends nothing", out.status == "already" and out.tx is None))
        second = metadata_hash(provider_metadata("Local RTX 3060", wallet, "0.000005", 6144))
        out = list_on_chain(rpc, 31337, registry, PROVIDER_KEY, second)
        results.append(("a new price updates the commitment", out.status == "updated" and read_listing(rpc, registry, wallet)[1] == second))
        empty = Account.create()
        out = list_on_chain(rpc, 31337, registry, empty.key.hex(), first)
        results.append(("no gas means a clear hint, no crash", out.status == "no-gas"))
        out = list_on_chain(rpc, 84532, registry, PROVIDER_KEY, first)
        results.append(("wrong chain sends nothing", out.status == "failed" and out.tx is None))
        out = list_on_chain(Rpc("http://127.0.0.1:1"), 31337, registry, PROVIDER_KEY, first)
        results.append(("an unreachable RPC doesn't crash join", out.status == "failed"))

        for name, ok in results:
            print(("PASS " if ok else "FAIL ") + name)
        return 0 if all(ok for _, ok in results) else 1
    finally:
        node.terminate()
        node.wait(10)


if __name__ == "__main__":
    sys.exit(main())
