# intent-workflow-runtime

## v3 — fixes from external code review (findings #1–7)

| # | Finding | Fix |
|---|---|---|
| 1 (critical) | `deny_all_network=False` gave the container a plain `bridge` network — full internet, regardless of `allowed_network_hosts` | `sandbox.py`: partially-open policies now get a dedicated Docker network with an iptables egress chain built by `build_egress_ruleset()` — default-deny, ACCEPT only for resolved IPs of explicitly allowed hosts, unconditional DROP after |
| 2 (critical) | `AuditLedger` silently generated `os.urandom(32)` when no key was given — a second process (any real verify-later scenario) got a different key and `verify_chain()` always reported tampering, even with none | `audit.py`: constructor now requires an explicit `signing_key` / `AUDIT_SIGNING_KEY` env var, or an explicit `ephemeral_ok=True` opt-in for same-process tests/demos — no more silent fallback |
| 3 (critical) | Mechanical check in the example was `exit_code == 0`, which proves the process didn't crash, nothing about actual file-level side effects | `sandbox.snapshot_workspace()` / `diff_workspace()` added; `example_run.py`'s mechanical check now fails if a step touched anything outside its declared `expected_outputs`, not just on nonzero exit |
| 4 (important) | `execute_workflow` ran steps in the LLM's raw list order, not topological order — a `human_gate` could sit after a step that should have waited for it | `contracts.topological_order()` added (reuses the existing dependency graph); `execute_workflow` now executes in dependency order while preserving the original list order in the returned artifact |
| 5 (important) | `forbidden_side_effects_observed` outside the sandboxed network/filesystem perimeter is pure self-report | Explicitly documented in `check_completion()`'s docstring as a known, unfixed residual gap — not silently left ambiguous |
| 6 (minor) | `run_step_local_unsafe` could be reached silently just because Docker was unavailable | Now requires `allow_unsafe=True` explicitly; `execute_workflow` requires `allow_unsafe_fallback=True` to reach it at all |
| 7 (minor) | Docker container escapes exist; not a VM boundary | Already documented in v2's README; unchanged, not a blocker, noted here for completeness |

Findings #1–4 are load-bearing fixes to code, not just documentation
changes, and are covered by new tests (`test_sandbox.py`,
`test_topological_order_*` in `test_contracts.py`,
`test_execute_workflow_follows_dependency_order_not_list_order`, and
`test_ledger_requires_explicit_key_by_default` / cross-process signature
tests in `test_audit.py`).



## v2 — closing gaps found via mova-compact/mova-api's own security audit

Four additions, each a direct response to a specific pattern found (either
as a good practice to borrow, or as a real bug class to guard against) in
that repository's `security_audit_report.md`:

| Addition | Where | Borrowed from / guards against |
|---|---|---|
| `owner_id` on intent + `actor_id` check at approve/execute/verify | `schemas/intent_contract.schema.json`, `contracts.py` | MOVA's **AUTHN-003** (contract silently overwritten/operated on by a non-owner) — there was no ownership binding at all before this |
| `manual_justification` required whenever a required criterion is `automation: manual` | `schemas/intent_contract.schema.json`, `contracts.check_acceptance_criteria_consistency()` | MOVA's `confidence_hint=high requires evidence_basis=dynamic` cross-field rule — a self-report label must come with a falsifiable reason, not just a tag |
| `kind: human_gate` as a distinct, structurally-enforced step type | `schemas/workflow_contract.schema.json`, `pipeline.execute_workflow` / `approve_human_gate()` | MOVA's `HUMAN_GATE` node type + "auto-escalation forbidden" rule — escalation is a type the executor cannot silently skip, not a prompt instruction |
| Hash-chained, HMAC-signed audit ledger | `runtime/audit.py` | MOVA's episode store + signed audit receipts — tampering by the executor itself is now detectable, not just assumed absent |
| Structural checks (ownership, forbidden side effects, step terminality, human-gate integrity) evaluated **before** any report-derived branch | `contracts.check_completion()` reordered | Direct fix for MOVA's **BF-001/BF-002** — a special-case branch evaluated before the permission/allowed-transition check let declared constraints get silently bypassed by code order, not by anyone breaking the schema |

None of this closes the residual gap discussed at length in the design
conversation this package came out of: a `manual_justification` can still
be a plausible-sounding lie, and `owner_id`/`actor_id` only proves identity
was checked, not that the actor's judgment was sound. What it closes is
narrower and more honest: the specific mechanical bypasses (unowned
contracts, unlabeled self-report, order-of-operations bugs, silent
retroactive log edits) that a real audit of a similar system actually
found in practice.


A lightweight runtime for the Intent Contract / Workflow Contract
architecture: four hard, non-LLM primitives, wired to two bounded LLM
calls, with no separate graph/validator/gate nodes for anything that
isn't security-critical.

## Architecture

```
resolve_intent_bounded()  →  contracts.freeze_intent()
        │                            │
        │ (LLM, schema-checked,      │ (pure function: rejects missing
        │  bounded clarification     │  fields, blocking uncertainty,
        │  loop, max 5 rounds)       │  unconfirmed material assumptions)
        ▼                            ▼
build_workflow_bounded()  →  contracts.approve_workflow()
        │                            │
        │ (LLM, schema-checked,      │ (pure function: intent_digest
        │  fast_track is a prompt    │  match, dependency-cycle
        │  param, not a graph        │  detection, required-AC
        │  branch)                   │  coverage in both directions)
        ▼                            ▼
execute_workflow()         →  permission_broker.derive_policy()
        │                            │
        │ (runs each step's          │ (pure function: reads frozen
        │  declared `command`        │  intent's forbidden side effects
        │  inside sandbox.py)        │  and scope, compiles into a
        │                            │  SandboxPolicy — network closed
        │                            │  by default, only opened for
        │                            │  explicitly named hosts)
        ▼
verify()                   →  contracts.check_completion()
        (LLM report, but            (pure function: for any criterion
         mechanical criteria         marked `automation: mechanical`,
         are independently           the model's report is NEVER
         re-checked, never           trusted — pass/fail comes only
         trusted from the report)   from mechanical_results computed
                                     by the runtime itself)
```

Four primitives (`freeze_intent`, `approve_workflow`, `derive_policy`,
`check_completion`, all pure functions in `runtime/contracts.py` +
`runtime/permission_broker.py`) are the only things that cannot be
talked around by a convincing model response. Everything else is soft
LLM judgment, and is allowed to be wrong cheaply, because nothing it
produces is trusted until it passes through one of the four.

## What this closes, from the design discussion

| Gap identified | Where it's closed |
|---|---|
| "Nice contract text, but nothing enforces it" | `contracts.py` — pure functions, unit-tested, no LLM in the loop |
| Forbidden side effects as text the executor must remember (the Hugging Face failure mode) | `permission_broker.derive_policy()` compiles them into a `SandboxPolicy` that `sandbox.py` enforces mechanically — network closed by default, opened only for explicitly named hosts |
| Unbounded clarification loop | `resolve_intent_bounded()` — hard stop after `MAX_CLARIFICATION_ROUNDS = 5` |
| Model's self-report trusted for checkable facts | `check_completion()` — `automation: mechanical` criteria are NEVER taken from the model's report, only from `mechanical_results` computed by the runtime |
| Graph ceremony (~20 nodes) for non-security-critical branching | Collapsed into two bounded functions; fast-track is a prompt parameter, not a graph branch |

## What this does NOT close — read before trusting it with anything real

- **`sandbox.run_step` uses a single Docker container, not a VM.**
  Container escapes exist. For genuinely high-stakes execution, harden
  further (seccomp, gVisor/Kata, a real firewalled network namespace)
  or use a dedicated VM per run.
- **`sandbox.run_step_local_unsafe` is NOT a security boundary at all.**
  It exists only so you can test the workflow *logic* on a machine
  without Docker. Do not use it when the executor's trustworthiness is
  in question — see the conversation this runtime came out of for why
  that distinction matters.
- **`automation: manual` criteria are still self-reported.** The
  runtime flags them for human review (`pending_human_review` in the
  completion summary) instead of silently trusting them, but it cannot
  mechanically verify genuinely subjective judgment. Push as many
  criteria to `mechanical` as you honestly can; accept the residual for
  the rest.
- **The LLM steps (`resolve_intent`, `build_workflow`) can still be
  wrong in ways schema validation won't catch** — e.g., a plausible but
  incorrect `objective`. Nothing here fixes garbage-in on the judgment
  side; it only guarantees that judgment errors are caught at the
  mechanical gates before they become executed side effects, and that
  execution itself can't exceed what the frozen intent explicitly
  permitted.

## Setup

```bash
pip install -r requirements.txt
```

The LLM layer is provider-neutral and loads `.env` automatically. Configure one
of these modes:

```dotenv
# OpenAI-compatible provider (OpenRouter, local gateway, etc.)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=...
LLM_MODEL=provider/model-name
```

```dotenv
# Native OpenAI Responses API
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
LLM_MODEL=gpt-5.6-terra
```

```dotenv
# Anthropic (retained for backward compatibility)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=claude-sonnet-4-6
```

`RUNTIME_MODEL` remains accepted as a deprecated alias for `LLM_MODEL`.

LLM reliability controls default to three attempts, exponential backoff with
jitter, and a 16,000-token per-call ceiling. They can be configured with
`LLM_MAX_ATTEMPTS`, `LLM_RETRY_BASE_SECONDS`, and `LLM_MAX_OUTPUT_TOKENS`.
OpenAI-compatible gateways must use `LLM_STRUCTURED_OUTPUT_MODE=json_schema`
for intent/workflow calls; `json_object` is rejected when a full schema is
required. Returned objects are validated locally even when the provider claims
to enforce the schema.

### Secrets

Local runs use the operating-system keyring. CI should set
`IW_SECRET_SOURCE=environment` and inject values through its protected secret
store. Manage local credentials without echoing their values:

```bash
python -m runtime.secret_cli set llm_api_key
python -m runtime.secret_cli status llm_api_key
python -m runtime.secret_cli delete llm_api_key
```

Supported names are `llm_api_key`, `openai_api_key`, `anthropic_api_key`, and
`audit_signing_key`. Audit details, run records, repair stderr/evidence, and
normalized LLM errors are recursively redacted before persistence. `.env`
should contain provider/model configuration and `IW_SECRET_SOURCE=keyring`,
never secret values.

### Observability

Start local health and metrics endpoints with:

```bash
python -m runtime.observability_server --host 127.0.0.1 --port 9090
```

Endpoints are `/health/live`, `/health/ready`, and `/metrics`. Readiness checks
Docker and LLM model/credential configuration without spending tokens; an
`AuditLedger` can also be registered for chain verification. Set
`IW_LOG_STDOUT=1` to emit redacted newline-delimited JSON logs. Execution and
LLM activity include run/trace/span correlation, retry and duration metrics,
and critical alert events for exhausted repair/retry or an invalid audit chain.

Docker is optional but strongly recommended — without it,
`execute_workflow` falls back to `run_step_local_unsafe`, which is not a
real sandbox (see above).

## Run the tests (no API key needed — LLM calls are mocked)

```bash
python -m pytest tests/ -v
```

## CI and releases

Runtime and development inputs live in `requirements*.in`; deployable locks are
the generated `requirements*.txt` files with SHA-256 hashes. Install them only
in an isolated environment:

```bash
python -m venv .venv
python -m pip install --require-hashes -r requirements-dev.txt
python scripts/check_release.py
python -m scripts.docker_e2e
```

GitHub CI tests Python 3.11/3.12 on Linux and Windows, runs Ruff, Bandit,
pip-audit, property tests and Docker E2E, and uploads a validated CycloneDX
SBOM. A reviewed `v*` tag produces a deterministic source archive, SBOM and
checksums, signs them with GitHub OIDC/Sigstore, and publishes an immutable
release. See [RELEASE.md](RELEASE.md) and [SECURITY.md](SECURITY.md).

## Run the end-to-end example (needs a configured LLM provider + Docker)

```bash
python examples/example_run.py
```

## Extending

### Bounded mechanical repair

Use `pipeline.execute_with_mechanical_repair()` when execution must revise a
workflow after independent checks fail. The supplied `mechanical_check`
callback returns `{"results": {"AC-1": "passed|failed|unknown"},
"evidence": {...}}`. Failed facts are sent to `repair_workflow_bounded()`,
which requires the next exact revision, `supersedes`, revision history, the
same frozen intent digest, schema/DAG/coverage approval, and command safety
checks before re-execution. The default repair budget is two revisions;
exhaustion raises `PipelineStop("mechanical_repair_exhausted")`.

For generated files, workflow steps should use structured `file_writes`
entries instead of model-authored writer commands. Runtime converts their
path/content payload into deterministic Docker argv and rejects absolute,
parent-traversal, duplicate, or undeclared output paths.

Production mechanical checks should be configured through
`runtime.verifiers.run_mechanical_verifiers()` / the `verifier_specs`
argument of `execute_with_mechanical_repair()`. Built-ins currently include
`json_equals`, `exact_files`, and read-only Docker `python_unittest`.
Missing or unknown verifier configuration fails closed, and terminal-step or
unexpected-output violations override an otherwise passing criterion.

Repair execution is transactional by default. Each revision runs in a
same-filesystem staging copy; failed stages are discarded, while a passing
stage is promoted with atomic directory replacement and rollback protection.

Production runs can use `RunStore` to persist UUID lifecycle records and a
checkpoint after every mechanical round. A new process can discover and resume
records left in `running`. Audit entries carry the same run ID, are appended
under a cross-process lock with `fsync`, and can be signed by `HTTPAuditSigner`
so the executor never receives the external service's signing key.


- Add acceptance criteria with `"automation": "mechanical"` and a
  concrete `verification_method` (e.g. a pytest path) whenever a real
  check exists — this is what keeps the completion gate honest instead
  of decorative.
- Add `authoritative_inputs` with full `https://` URLs for any external
  host a step legitimately needs — this is the only way network access
  gets opened. The worker remains on an internal-only Docker network and
  reaches exact allowlisted hostnames through a constrained proxy sidecar;
  direct internet connections are unreachable.
- Execution uses conservative fixed limits: one CPU, 512 MB memory, 128 PIDs,
  256 file descriptors, a 64 MB noexec `/tmp`, and a 1 MB combined output
  quota. It also has no capabilities, runs as UID/GID 65534, uses Docker's
  default seccomp profile, and has a read-only root filesystem.
