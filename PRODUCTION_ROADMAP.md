# Production hardening roadmap

## Definition of production-ready

A run is production-ready only when untrusted model output cannot expand
authority, every required criterion has independently produced evidence,
execution is isolated and reproducible, repair is transactional, audit
receipts survive process loss, and operators can detect and recover failure.

## P0 — trust boundaries

1. [x] Typed mechanical verifier registry; no caller-supplied pass/fail claims.
2. [x] Transactional repair workspace with rollback and dependency-subgraph retry.
3. [x] Hardened sandbox: immutable image digests, non-root execution, seccomp,
   resource/output quotas, and enforceable network policy on Windows/WSL.
4. [x] External audit signer/verifier with persistent run IDs and crash recovery.

## P1 — operational reliability

5. [x] Provider capability negotiation, bounded retries/backoff, quotas, and
   stable error taxonomy.
6. [x] OS/CI secret storage and mandatory redaction.
7. [x] Structured logs, metrics, traces, health/readiness endpoints and alerts.

## P2 — delivery controls

8. [x] CI gates for unit, Docker E2E, fuzz/security tests, dependency locking,
   SBOM/signing, migration checks, and documented release/rollback procedure.

Each phase must add negative tests proving the protected invariant fails
closed, not only happy-path tests.

## Implementation notes

- Items 1–2 are covered by trusted typed verifiers, read-only verifier
  containers, atomic workspace promotion, failed-attempt rollback, and
  downstream-only retry tests.
- Item 3 is covered: execution and verifier images are digest-pinned and
  run as UID/GID 65534 with bounded CPU, memory, PIDs, file descriptors, and
  a size-limited noexec tmpfs. Combined output quotas and hard timeout cleanup
  are enforced. Closed networking uses Docker's `none` mode. Partial policies
  put the worker on an internal-only network and use a constrained dual-homed
  sidecar proxy with an exact hostname ACL; direct egress remains unreachable.
- Item 4 is covered by UUID run records with atomic checkpoints and terminal
  states, incomplete-run discovery/resume, durable locked audit appends, and
  pluggable local or HTTPS out-of-process signer/verifier implementations.
- Item 5 is covered by explicit provider capability profiles, mandatory full
  JSON Schema support when a schema is supplied, local response validation,
  bounded exponential retry with jitter, output-token quotas, and stable
  retryable/non-retryable error codes.
- Item 6 is covered by OS-keyring-first local resolution, environment-only CI
  injection, a non-echoing credential CLI, and recursive redaction before
  audit records, run checkpoints, repair evidence, and LLM error persistence.
  The local API key has been migrated out of `.env`.
- Item 7 is covered by redacted JSON event logs, run/trace/span correlation,
  Prometheus counters and duration aggregates, live/ready/metrics HTTP
  endpoints, Docker/LLM/audit readiness probes, and explicit critical alert
  signals for exhausted repair/retry and invalid audit chains.
- Item 8 is covered by cross-platform hash-locked CI, property tests, Ruff,
  Bandit, pip-audit, real Docker enforcement E2E, validated CycloneDX SBOMs,
  commit-pinned GitHub Actions, Sigstore keyless release signing, immutable
  tag archives, and documented verification/release/rollback procedures.
