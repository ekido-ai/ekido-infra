"""Registers a provider on another machine, prints the command to run there, and qualifies it once it connects.

Run it on the coordinator's laptop, with demo_stack.py --lan running. --wallet is the address of a wallet on the
other machine, never its key:

    python experiments/add_remote_provider.py --name "Vaibhav RTX 4060" --wallet 0xABC... --lan-ip 192.168.1.20

The price decides when the scheduler picks this provider. The default is cheaper than Provider B,
so after Provider P is rejected, the job moves here instead of to B. See docs/second-gpu-provider.md.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(base: str, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--wallet", required=True, help="The address of the wallet on the other machine.")
    parser.add_argument("--lan-ip", help="This laptop's address on a shared network.")
    parser.add_argument("--public-url", help="A tunnel's address, such as wss://name.trycloudflare.com, for a provider on another network.")
    parser.add_argument("--capacity-mib", type=int, help="Only needed to cap this provider, or for a CPU, which has no memory reading.")
    parser.add_argument("--price", default="0.0025", help="USDC per accepted step.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--device", default="cuda", help="cuda, or cpu to test the connection first.")
    parser.add_argument("--wait-seconds", type=int, default=600)
    args = parser.parse_args()
    if bool(args.lan_ip) == bool(args.public_url):
        parser.error("give exactly one of --lan-ip or --public-url")
    if args.public_url and not args.public_url.startswith("wss://"):
        parser.error("--public-url must start with wss://, so the connection is encrypted")
    coordinator = args.public_url.rstrip("/") if args.public_url else f"ws://{args.lan_ip}:{args.port}"
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", args.wallet):
        parser.error("--wallet must be an address. If that's a private key, keep it on the other machine")

    base = f"http://127.0.0.1:{args.port}"
    workload = json.loads((ROOT / "experiments" / "demo_stack_info.json").read_text(encoding="utf-8"))["new_job_body"]["workload"]
    created = call(base, "POST", "/providers", {"name": args.name, "wallet_address": args.wallet, "kind": "REAL", "capacity_mib": args.capacity_mib, "price_per_step": args.price})
    provider_id = created["provider_id"]
    print(f"Registered {args.name}.\n\nOn the other machine, from the repo root, with the wallet's key in PROVIDER_WALLET_KEY:\n")
    print(f"  python -m ekido_agent.agent --coordinator {coordinator} --provider-id {provider_id} --workdir agent_runs --mode process --device {args.device}\n", flush=True)

    deadline = time.monotonic() + args.wait_seconds
    while next(p for p in call(base, "GET", "/providers") if p["provider_id"] == provider_id)["status"] != "ONLINE":
        if time.monotonic() > deadline:
            raise SystemExit(f"{args.name} never connected. Check the firewall and the address, then see docs/second-gpu-provider.md.")
        time.sleep(2)
    print(f"{args.name} connected. Qualifying providers: it runs a real probe on each.", flush=True)
    for row in call(base, "POST", "/providers/qualify", {"workload": workload}):
        outcome = f"{row['exec']['micro_batch']} x {row['exec']['accumulation']}" if row["approved"] else f"refused: {row['code']}"
        print(f"  {row['name']}: {outcome}")


if __name__ == "__main__":
    main()
