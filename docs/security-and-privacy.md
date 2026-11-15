# Security and privacy

What's protected today, and what isn't. Updated 5 Nov 2026.

**Settlement repair in progress:** the default verifier now refuses verdict signatures without independently configured chain facts and signed payout evidence. The secure producer and failure/refund paths are not fully integrated or validated. The explicitly unsafe local demo mode still trusts coordinator payouts and can be misled into signing an unrelated payee. Browser Verify pins the transaction destination to configured escrow, but checkpoint detail and prices remain coordinator-supplied metadata. See `docs/repo-review-2026-11-05.md` for historical findings; do not claim the complete payout protocol is secured yet.

Tokens are in-memory session tokens, not uniformly short-lived: verifier tokens have no expiry timer. Checkpoint downloads enforce the declared byte count and hash while streaming before making a file visible.

## Who can do what

| Who | Reaches | How it's checked |
|---|---|---|
| This laptop | The whole operator API | Client and `Host` header must both be local, and no proxy header |
| Another machine | The operator API | `Authorization: Bearer <API_TOKEN>`. With no token set, it gets nothing |
| A website open in a browser | Nothing, unless it's the dashboard | The `Origin` is checked on HTTP and WebSockets, including for DNS rebinding |
| A provider's agent | `/agent/ws` and artifacts | Signed envelopes and connection-scoped artifact tokens |
| A verifier | `/verifier/*` and artifacts | Signed sessions bound to the verifier for task claims, signed reports, and an allowlist |
| Anyone | `/healthz` | Open, and shows nothing sensitive |

The code is `apps/coordinator/ekido_coordinator/api_guard.py`, and its tests are in `apps/coordinator/tests/test_api_guard.py`.

**Before a hotspot or second-GPU session:** set a long random `API_TOKEN` in `.env`. Never share the coordinator's port through Live Share, or any forwarder that hides the outside client: that makes outside requests look local. A tunnel that adds `X-Forwarded-For`, which Cloudflare Tunnel and ngrok both do, is locked out of the operator API, as `test_a_tunnel_reaches_only_the_routes_with_their_own_proof` checks. Before relying on one, check that `GET /jobs` through it returns 401. `OPEN_API=1` turns the check off, and is only for a container whose port is published on `127.0.0.1`.

## Keys

- Keys stay in the untracked `.env`, and the demo stack passes them through the environment. On 5 Nov, a live stack had no key-sized value on any of its process command lines.
- Testnet wallets only, never real funds. Never share `.env`. Run the wallet-key scan before sending any zip.

## Money

| Risk | What stops it |
|---|---|
| An unsigned payout | The escrow needs a quorum of registered verifier signatures. Default workers require independent payout evidence; the explicitly unsafe local demo mode still trusts the coordinator's split |
| Replay on another job or chain | EIP-712 domain, job ID, one-shot state, and expiry |
| Verifiers or arbiter swapped mid-job | Both are fixed per job |
| Funds stuck | A timeout refund anyone can trigger |
| Too short an on-chain deadline | The coordinator refuses to start the job |

**Not protected:** two colluding verifiers could approve a bad result, because the team runs them all. One admin key controls future jobs. The contract is unaudited, so it's for testnets only.

## Checkpoints and workers

- Checkpoints use safetensors and canonical JSON, with no pickle and no `torch.load`.
- GPCK loading checks content hashes, tensor names, shapes, dtypes and finite values. Signature, fencing and lineage checks depend on the caller and its context; loading the files alone does not establish those checks.
- Jobs come from an allowlisted template. No user code runs.

## Privacy: what we can't promise

| Fact | So |
|---|---|
| A provider sees the training data and model in memory | **Use public data only.** Never say "private training" |
| Checkpoints aren't encrypted on disk or in transit over plain HTTP | Same. Encryption is future work, and would only protect storage |
| The evaluation set is public, so a provider could train on it | Production needs a client-held private test set |
| The Verify page reads the coordinator and an RPC node | Both see that someone asked. It sends no transaction |
| Wallet addresses and provider prices are visible | They're public on-chain once a job pays. Use fresh testnet wallets |

## Known open items

- `model.revision` is `main` in the demo spec, and the dataset fingerprint is all zeros. A pinned commit and a real fingerprint are needed before calling the spec binding.
- There's one operator token, not a login per client, so anyone holding `API_TOKEN` can cancel any job.
- No per-route rate limit on the API.
