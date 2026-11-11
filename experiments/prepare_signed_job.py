"""Prepare signed payout documents offline; this script makes no network calls.

Use `ids` to choose client IDs, `quote` on each provider's own machine, then
assemble provider_terms in the workload and use `bind` with the client key.
If the coordinator charges a protocol fee (GET /fees), pass the same --protocol-fee-bps and
--protocol-fee-recipient to quote, bind and request, so every signature covers the same terms.
Keys are read only from environment variables, never from command arguments.
"""
from __future__ import annotations

import argparse
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path

from eth_account import Account
from gpuchain_core.payout_terms import job_binding, new_job_ids, provider_quote, validate_terms, workload_hash
from gpuchain_core.protocol_fee import FEE_KEY, fee_problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    ids = subs.add_parser("ids")
    ids.add_argument("--client", required=True)
    quote = subs.add_parser("quote")
    bind = subs.add_parser("bind")
    request = subs.add_parser("request", help="Generate the complete JSON request for the website.")
    for command in (quote, bind, request):
        command.add_argument("--job-id", required=True)
        command.add_argument("--onchain-id", required=True)
        command.add_argument("--chain-id", required=True, type=int)
        command.add_argument("--escrow", required=True)
    quote.add_argument("--provider-id", required=True)
    quote.add_argument("--rate", required=True)
    quote.add_argument("--spec", required=True, type=Path, help="Workload JSON before provider terms are attached.")
    for command in (bind, request):
        command.add_argument("--spec", required=True, type=Path)
        command.add_argument("--policy", required=True, type=Path)
        command.add_argument("--budget", required=True)
    request.add_argument("--deadline-seconds", required=True, type=int)
    for command in (quote, bind, request):
        command.add_argument("--protocol-fee-bps", type=int, help="The coordinator's fee from GET /fees, such as 800.")
        command.add_argument("--protocol-fee-recipient", help="The fee recipient from GET /fees.")
    args = parser.parse_args()

    def with_fee(spec: dict) -> dict:
        """Puts the fee into the workload before anything is hashed or signed."""
        bps, recipient = getattr(args, "protocol_fee_bps", None), getattr(args, "protocol_fee_recipient", None)
        if bps is None and recipient is None:
            return spec
        if bps is None or recipient is None:
            parser.error("pass both --protocol-fee-bps and --protocol-fee-recipient, or neither")
        spec = {**spec, FEE_KEY: {"bps": bps, "recipient": recipient}}
        problems = fee_problems(spec)
        if problems:
            parser.error("; ".join(problems))
        return spec

    if args.command == "ids":
        job_id, onchain_id = new_job_ids(args.client)
        result = {"job_id": job_id, "onchain_id": onchain_id}
    else:
        env_name = "PROVIDER_PRIVATE_KEY" if args.command == "quote" else "CLIENT_PRIVATE_KEY"
        key = os.environ.get(env_name)
        if not key:
            parser.error(f"set {env_name} in the environment")
        client = Account.from_key(key).address  # Validate locally; do not print the key.
        common = dict(job_id=args.job_id, onchain_id=args.onchain_id, chain_id=args.chain_id, contract=args.escrow, key=key)
        if args.command == "quote":
            result = provider_quote(**common, provider_id=args.provider_id, rate=args.rate, workload=workload_hash(with_fee(json.loads(args.spec.read_text(encoding="utf-8")))))
        else:
            spec = with_fee(json.loads(args.spec.read_text(encoding="utf-8")))
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            budget_text = args.budget
            if args.command == "request":
                try:
                    budget = Decimal(args.budget)
                    if not budget.is_finite() or not 0 < budget < 10**14 or budget.as_tuple().exponent < -6:
                        raise ValueError("enter a positive budget with at most six decimal places")
                    if not 1 <= args.deadline_seconds <= 604800:
                        raise ValueError("deadline must be 1–604800 seconds")
                    budget_text = format(budget, "f")
                except (ValueError, InvalidOperation) as exc:
                    parser.error(str(exc))
            result = job_binding(**common, spec=spec, policy=policy, budget=budget_text)
            if args.command == "request":
                try:
                    validate_terms(job_id=args.job_id, client=client, binding=result, spec=spec, policy=policy,
                                   budget=budget, chain_id=args.chain_id, contract=args.escrow)
                except ValueError as exc:
                    parser.error(str(exc))
                result = {
                    "job_id": args.job_id,
                    "client_address": client,
                    "workload": spec,
                    "policy": policy,
                    "budget_usdc": budget_text,
                    "deadline_seconds": args.deadline_seconds,
                    "job_binding": result,
                }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
