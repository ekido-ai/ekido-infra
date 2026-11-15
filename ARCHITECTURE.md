# Architecture — Ekido

A short map of the system. The full analysis (requirements, state ownership, failure modes, bottlenecks and scaling) is in [`docs/system-design.md`](docs/system-design.md).

## Overview

Ekido coordinates fine-tuning jobs across untrusted GPU providers. Three properties hold together:

1. **Continuity:** a job survives the death of the GPU running it.
2. **Quality:** work is paid only after it passes a test agreed before training.
3. **Settlement:** money waits in an escrow contract and moves only under that contract's rules.

The chain is the ledger for money. The coordinator's database is the orchestration log. The browser re-checks money against the chain, so the coordinator never has to be trusted with balances.

## Components

| Component | Path | Responsibility |
|---|---|---|
| Shared core | `packages/ekido_core/` | Job specs and policies, canonical JSON (RFC 8785), signing, the GPCK checkpoint format, the elastic compiler, quality statistics, the protocol fee, provider metadata |
| Coordinator | `apps/coordinator/` | Job state machine, recovery orchestrator, scheduler, agent gateway (WebSocket), artifact store, quality and settlement services, chain indexer, rate limits, `/readyz` |
| Provider agent | `workers/agent/` | Joins with a wallet-signed document, reports NVML telemetry, spawns the runtime (process or Docker), signs checkpoints, lists the provider on-chain |
| Training runtime | `workers/runtime/` | Logical-step training loop, deterministic data order, checkpoint save and restore, memory probe |
| Verifier | `workers/verifier/` | Claims evaluation tasks with a lease, evaluates in FP32, recomputes payouts, signs EIP-712 verdicts |
| Contracts | `contracts/` | `GPUChainEscrow` (budgets, verdicts, disputes, refunds, pull payments) and `ProviderRegistry` (providers' own terms) |
| Web app | `apps/web-combined/` | Landing page, mission control, recovery and quality views, on-chain books, accounts, owner console, provider onboarding |
| Infra | `infra/` | Provider kit, server deployment (AWS, earlier GCP), backups and monitoring, Docker images, test runners |

## Data flow

```
Client ──createJob(budget, spec hash, policy hash)──► Escrow
  │
  ▼
Coordinator ──assign(fencing token, exec config)──► Provider A
  │                                                    │ signed checkpoints
  │ ◄──────────────────────────────────────────────────┘
  │   Provider A dies: detect → fence → pick checkpoint → re-test it
  │   → compile + memory probe on Provider B → restore → canary loss → resume
  ▼
Verifiers (2 of 3) ── evaluate each candidate against the last accepted checkpoint
  │
  ▼
Settlement: payouts from the accepted lineage + 8% fee + refund = budget
  │  verifiers recompute and sign (EIP-712)
  ▼
Escrow: submitAttestation → dispute window → finalize → each payee withdraws
```

## The logical-step invariant

Everything that defines the maths of a training step is fixed for the whole job: model and revision, LoRA config, optimizer, learning-rate schedule, logical batch size, seed and data order. Only execution adapts to the GPU: micro-batch, gradient accumulation, autocast precision and gradient checkpointing. Master weights and optimizer state stay in FP32. This is what lets a run move between GPU types.

## Where state lives

| State | Store | Source of truth |
|---|---|---|
| Budgets, payouts, refunds, balances | Base Sepolia | The escrow contract |
| Provider terms | Base Sepolia | The provider's own registry entry |
| Jobs, transitions, fencing tokens, checkpoint manifests | SQLite (Postgres planned) | The coordinator |
| Checkpoint tensors | Content-addressed disk, backed up off-site | The coordinator's artifact store |
| Live connections, in-flight evaluations | Memory | Rebuilt on reconnect |

## Security controls

| Threat | Control |
|---|---|
| A provider returns bad or lazy work | Pre-agreed statistical test, 2-of-3 verifier quorum, rollback, zero pay for that segment |
| A dead worker comes back and writes | Fencing tokens: stale writes get a 409; a dead-man lease stops orphaned runtimes |
| A malicious checkpoint file | safetensors and JSON only, never pickle; signature, fencing, lineage, hash, size, tensor allowlist and NaN checks before any load |
| The coordinator overreaches | It can start jobs and anchor events, never move funds |
| The operator disappears | Anyone can trigger the timeout refund after the deadline |
| Signature replay | EIP-712 domain bound to chain, contract, job and expiry; signers strictly ascending |
| Abuse of the API | Per-client token buckets (240 reads, 40 writes a minute), proxy-aware IPs, at most 5 providers per IP |
| Web attacks | Strict CSP with a `connect-src` allowlist, HSTS, `X-Frame-Options: DENY`, `nosniff`, wallet sign-in (EIP-4361), no passwords |

The full threat model is in [`THREAT_MODEL.md`](THREAT_MODEL.md).

## Deployment

One AWS EC2 instance (Mumbai) runs the coordinator, two verifiers, the web app and a CPU provider as systemd units behind Caddy (HTTPS via Let's Encrypt). Operations, backups and alerts are in [`RUNBOOK.md`](RUNBOOK.md).
