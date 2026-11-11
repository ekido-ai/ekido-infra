"""Creates testnet-only wallets and fills the empty key slots in .env. Run from the repo root:
    python infra/make_test_wallets.py

Prints addresses only, never keys. Existing values are kept, so running it twice changes nothing.
Never send real funds to these wallets: the keys sit in a plain file on a laptop.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from eth_account import Account

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"
ROLES = (
    "DEPLOYER", "COORDINATOR", "ARBITER", "VERIFIER_1", "VERIFIER_2", "VERIFIER_3",
    "CLIENT", "PROVIDER_A", "PROVIDER_B", "PROVIDER_C", "PROVIDER_P",
)


def main() -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else TEMPLATE.read_text(encoding="utf-8")
    for role in ROLES:
        name = f"{role}_PRIVATE_KEY"
        match = re.search(rf"^{name}=(.*)$", text, flags=re.M)
        if match and match.group(1).strip():
            continue
        key = "0x" + secrets.token_hex(32)
        if match:
            text = re.sub(rf"^{name}=.*$", f"{name}={key}", text, flags=re.M)
        else:
            text = text.rstrip("\n") + f"\n{name}={key}\n"
    ENV.write_text(text, encoding="utf-8")
    for role in ROLES:
        key = re.search(rf"^{role}_PRIVATE_KEY=(.*)$", text, flags=re.M).group(1).strip()
        print(f"{role:12} {Account.from_key(key).address}")


if __name__ == "__main__":
    main()
