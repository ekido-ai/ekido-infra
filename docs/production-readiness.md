# Production readiness

What separates the current testnet prototype from a production service. Written 9 Nov 2026. "Done" means built and checked in this repo, not audited.

## Done

- **Payment rules on-chain.** An escrow on Base Sepolia pays out only with 2 of 3 verifier signatures. Payouts plus the refund must equal the budget. Refunds after a timeout are permissionless, and payments are pull-based. 99 contract tests, including fuzz and invariant tests.
- **Settlement recovery.** A lost receipt, an expired proposal or a near-expiry verdict is rebuilt or re-signed instead of getting stuck.
- **Public reads, locked writes.** The API guard opens GET requests and job streams; operator routes need loopback access or a bearer token.
- **Self-service provider join.** It's wallet-signed and rate-limited per real client address (proxy-aware). Names are unique and printable. Each address can add at most 5 providers, and the whole network at most 200.
- **Provider kit** for Windows, macOS and Linux, published with its SHA-256. It contains source only, no keys.
- **On-chain transparency.** Live proof of reserves, a double-entry journal rebuilt from events, and a five-way reconciliation, all run in the user's browser.
- **Web hardening.** No framing, `nosniff`, a strict referrer policy and a permissions policy. No cookies or trackers.
- **Policy pages.** Privacy, terms, security disclosure and help, plus a live status page.
- **Real-GPU evidence.** 3 of 3 clean kill-and-recover rounds on an RTX 3060 (9 Nov), and a T4 to RTX 3060 to T4 handover (7 Nov).

## Must do before real money

| Area | Gap | What production needs |
|---|---|---|
| Contracts | Unaudited | An independent audit, a fix round, then a public bug bounty |
| Contract admin | Owner, coordinator and arbiter are single keys | Owner and arbiter behind a multisig, such as a Safe, plus a timelock on verifier-set changes |
| Verifiers | All three are run by our team | Independent operators with their own infrastructure, and a stake that can be slashed for a provably wrong verdict |
| Keys | Testnet keys live in root-only env files on one VM | A KMS or HSM, rotation, and separate cloud accounts per role |
| Provider isolation | The kit runs jobs as normal processes | Docker or gVisor required by default, with process mode only for trusted machines; required before any custom code |
| Data privacy | Providers can read job data; the evaluation set is public | Client-private holdouts, and TEE attestation on hardware that supports it |
| Infrastructure | One VM, one coordinator, DuckDNS name | A real domain, managed Postgres with backups, at least two coordinator instances, and edge rate limits on every route |
| Monitoring | A status page that runs in the browser | Server-side uptime checks, alerts on failed settlements, a low-gas alert on the coordinator wallet, central logs |
| Delivery | No version control remote or CI | A private repo, CI that runs all suites on every change, and signed release builds of the provider kit |
| Legal | Testnet only | Legal review of terms, privacy and payments for each country before mainnet or real USDC |

## Known limits we state openly

- Verification is statistical, not a proof that training happened as claimed.
- GPU details are self-reported.
- Demand on the test network is ours. Nobody external is paying for jobs yet.
