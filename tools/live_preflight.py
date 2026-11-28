"""Read-only checks to run just before showing the live network. Sends GET requests only and uses no key.

    python infra/live_preflight.py                      # https://api.ekido.xyz
    python infra/live_preflight.py --host example.org   # another host
    python infra/live_preflight.py --min-providers 3    # when friends have joined

It prints one line per check and exits 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone

PAGES = ["/", "/dashboard", "/jobs/new", "/verify", "/chain", "/settlement", "/provide", "/kit", "/status"]
ESCROW = "0x1BF65f755457a92850009c88AFb337Ec57745a22"
RPCS = ["https://base-sepolia-rpc.publicnode.com", "https://sepolia.base.org"]
failures = 0


def report(ok: bool, text: str) -> None:
    global failures
    failures += 0 if ok else 1
    print(("ok    " if ok else "FAIL  ") + text)


def get(url: str, timeout: float = 20):
    request = urllib.request.Request(url, headers={"user-agent": "curl/8.5.0"})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read(), time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="api.ekido.xyz")
    parser.add_argument("--min-providers", type=int, default=2)
    args = parser.parse_args()
    base = f"https://{args.host}"

    try:
        status, body, seconds = get(f"{base}/sepolia/readyz")
        ready = json.loads(body)
        bad = [name for name, check in ready.get("checks", {}).items() if not check.get("ok")]
        report(status == 200 and ready.get("status") == "ready" and not bad, f"coordinator readyz: {ready.get('status')}" + (f" (failing: {', '.join(bad)})" if bad else ""))
        gas = ready.get("checks", {}).get("coordinator_gas", {}).get("detail", "")
        report(bool(gas), f"coordinator gas: {gas}")
    except Exception as exc:
        report(False, f"coordinator readyz: {exc}")

    try:
        _, body, _ = get(f"{base}/sepolia/providers")
        providers = json.loads(body)
        online = [p for p in providers if p.get("status") == "ONLINE"]
        report(len(online) >= args.min_providers, f"providers online: {len(online)} of {len(providers)} (need {args.min_providers}): " + ", ".join(p["name"] for p in online))
    except Exception as exc:
        report(False, f"providers: {exc}")

    for page in PAGES:
        try:
            status, _, seconds = get(base + page)
            report(status == 200 and seconds < 5, f"{page} -> {status} in {seconds:.1f}s")
        except Exception as exc:
            report(False, f"{page}: {exc}")

    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2  # never accept an older protocol, even from a server that offers one
        with socket.create_connection((args.host, 443), timeout=15) as sock, context.wrap_socket(sock, server_hostname=args.host) as tls:
            expires = datetime.strptime(tls.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (expires - datetime.now(timezone.utc)).days
        report(days > 14, f"certificate valid for {days} more days")
    except Exception as exc:
        report(False, f"certificate: {exc}")

    code = ""
    for rpc in RPCS:
        try:
            payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [ESCROW, "latest"]}).encode()
            request = urllib.request.Request(rpc, payload, {"content-type": "application/json", "user-agent": "curl/8.5.0"})
            with urllib.request.urlopen(request, timeout=20) as response:
                code = json.load(response)["result"]
            break
        except Exception:
            continue
    report(len(code) > 4, f"escrow contract on Base Sepolia: {(len(code) - 2) // 2} bytes of code")

    print("\n" + ("ready to show" if failures == 0 else f"{failures} check(s) failed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
