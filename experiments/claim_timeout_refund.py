"""Inspect or explicitly claim the escrow's existing timeout refund.

Dry run by default. --submit sends a transaction and requires REFUND_CALLER_KEY.
The refund is credited to the on-chain client; the client must withdraw separately.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps/coordinator"), str(ROOT / "packages/gpuchain_core")]

from gpuchain_coordinator.chain import ChainError, EscrowClient  # noqa: E402

READ_ONLY_KEY = "0x" + "01" * 32  # Public dummy; used only to instantiate a read-only client.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpc-url", required=True)
    parser.add_argument("--chain-id", required=True, type=int)
    parser.add_argument("--escrow", required=True)
    parser.add_argument("--job-id", required=True, help="On-chain bytes32 job ID.")
    parser.add_argument("--submit", action="store_true", help="Send claimTimeoutRefund if eligible; uses REFUND_CALLER_KEY.")
    args = parser.parse_args()
    if args.chain_id <= 0:
        parser.error("chain ID must be positive")
    key = os.environ.get("REFUND_CALLER_KEY") if args.submit else READ_ONLY_KEY
    if not key:
        parser.error("--submit requires REFUND_CALLER_KEY in the environment")
    client = EscrowClient(args.rpc_url, args.escrow, key)
    actual_chain = client.w3.eth.chain_id
    if actual_chain != args.chain_id:
        raise ChainError(f"configured chain {args.chain_id} does not match RPC chain {actual_chain}")
    status = client.timeout_refund_status(args.job_id)
    print(json.dumps({"chain_id": actual_chain, "escrow": client.contract.address, "onchain_job_id": args.job_id, "budget_base_units": status.pop("budget"), **status, "submitted": False}, indent=2), flush=True)
    if args.submit:
        result = client.ensure_timeout_refund(args.job_id)
        print(json.dumps({"action": result.action, "status": result.status, "tx_hash": result.tx_hash}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
