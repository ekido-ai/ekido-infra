"""Start the hosted coordinator after checking its signing role on Base Sepolia."""
from __future__ import annotations

import sys

import uvicorn
from web3 import Web3

from ekido_coordinator.chain import EscrowClient
from ekido_coordinator.main import Settings, create_app

BASE_SEPOLIA = 84532
TEST_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"


def verify_config(settings: Settings) -> None:
    if settings.chain_id != BASE_SEPOLIA or settings.demo_mode or settings.open_api:
        raise ValueError("Hosted Sepolia requires chain 84532, demo mode off and the API guard enabled.")
    if not (settings.rpc_url and settings.escrow_address and settings.coordinator_private_key and settings.api_token):
        raise ValueError("Configure the RPC, escrow, coordinator signer and operator token.")
    if settings.database_url in ("sqlite://", "sqlite:///:memory:"):
        raise ValueError("The coordinator requires a persistent database.")
    verifiers = {Web3.to_checksum_address(address.strip()) for address in settings.verifier_addresses.split(",") if address.strip()}
    if not 2 <= settings.verifier_quorum <= len(verifiers):
        raise ValueError("Configure at least two independent verifier wallets and a valid quorum.")
    chain = EscrowClient(settings.rpc_url, settings.escrow_address, settings.coordinator_private_key)
    if chain.rpc_chain_id() != BASE_SEPOLIA:
        raise ValueError("The configured RPC returned a different chain.")
    escrow = chain.contract.functions
    if escrow.token().call().lower() != TEST_USDC.lower():
        raise ValueError("The escrow token is not Base Sepolia test USDC.")
    if escrow.coordinator().call().lower() != chain.account.address.lower():
        raise ValueError("The configured signer does not hold the escrow coordinator role.")
    verifier_set = escrow.currentVerifierSet().call()
    if escrow.quorumOf(verifier_set).call() != settings.verifier_quorum or any(
        not escrow.isVerifier(verifier_set, address).call() for address in verifiers
    ):
        raise ValueError("The verifier addresses or quorum differ from the escrow configuration.")


def main() -> None:
    try:
        settings = Settings(_env_file=None)
        verify_config(settings)
        app = create_app(settings)
    except Exception:
        # RPC URLs and Pydantic validation errors can contain environment values.
        sys.exit("Sepolia coordinator startup failed. Check the configured network, escrow, signer role, verifier set and database.")
    uvicorn.run(app, host="127.0.0.1", port=8003, log_level="info")


if __name__ == "__main__":
    main()
