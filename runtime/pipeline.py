"""
pipeline.py — the entire orchestration, collapsed to the minimum
ceremony discussed in the architecture review: four hard primitives
(freeze / permission_broker.derive_policy / approve / check_completion,
all in contracts.py + permission_broker.py) wired to two bounded LLM
calls (resolve_intent, build_workflow). No separate validator nodes,
no separate clarification-gate nodes, no separate fast-track branch in
a graph — those are folded into two functions with internal bounded
loops, because their looping/branching logic is not itself
security-critical and doesn't need graph ceremony around it.
"""

import base64
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

import jsonschema

from . import contracts, llm_client, sandbox, verifiers
from .audit import AuditLedger
from .run_store import RunStore
from .secrets import redact
from .permission_broker import derive_policy
from .observability import default_observability

_HERE = Path(__file__).parent
SCHEMAS_DIR = _HERE.parent / "schemas"
PROMPTS_DIR = _HERE.parent / "prompts"

INTENT_SCHEMA = json.loads((SCHEMAS_DIR / "intent_contract.schema.json").read_text())
WORKFLOW_SCHEMA = json.loads((SCHEMAS_DIR / "workflow_contract.schema.json").read_text())

MAX_CLARIFICATION_ROUNDS = 5
MAX_REPAIR_ATTEMPTS = 2
SHELL_ONLY_COMMANDS = {"echo", "set", "export", "source"}
SHELL_METATOKENS = {">", ">>", "<", "|", "||", "&&", ";"}


class PipelineStop(Exception):
    """Raised for any terminal stop condition. `reason` matches the stop
    reasons from the original workflow.yaml design 1:1, so behavior is
    traceable back to that spec even though the graph ceremony is gone."""

    def __init__(self, reason: str, details: Optional[dict] = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


def _validate_literal_commands(workflow: dict) -> None:
    """Reject command arrays that cannot work with shell=False.

    Python `-c` payloads are compiled without executing them. This turns a
    common model formatting failure into repair feedback before approval and
    before any sandbox side effect.
    """
    for step in workflow.get("steps", []):
        command = step.get("command")
        file_writes = step.get("file_writes")
        if command and file_writes:
            raise contracts.ContractError(
                "ambiguous_step_execution",
                {"step_id": step.get("id"), "reason": "use command or file_writes, not both"},
            )
        if file_writes:
            expected = set(step.get("expected_outputs", []))
            paths = []
            for entry in file_writes:
                path = Path(entry.get("path", ""))
                if path.is_absolute() or ".." in path.parts or str(path) not in expected:
                    raise contracts.ContractError(
                        "invalid_file_write_path",
                        {"step_id": step.get("id"), "path": str(path)},
                    )
                paths.append(str(path))
            if len(paths) != len(set(paths)):
                raise contracts.ContractError(
                    "duplicate_file_write_path", {"step_id": step.get("id")},
                )
        if not command:
            continue
        executable = Path(command[0]).name.lower()
        if executable in SHELL_ONLY_COMMANDS or any(arg in SHELL_METATOKENS for arg in command):
            raise contracts.ContractError(
                "invalid_literal_command",
                {"step_id": step.get("id"), "reason": "shell syntax is unavailable"},
            )
        if executable in {"python", "python3", "python.exe"} and len(command) >= 3 and command[1] == "-c":
            lowered = command[2].lower()
            placeholders = ("todo", "implementation here", "analysis logic here", "tests here")
            if any(marker in lowered for marker in placeholders):
                raise contracts.ContractError(
                    "placeholder_python_command",
                    {"step_id": step.get("id"), "reason": "executable contains placeholder code"},
                )
            try:
                compile(command[2], f"<{step.get('id', 'workflow-step')}>", "exec")
            except SyntaxError as exc:
                raise contracts.ContractError(
                    "invalid_python_command",
                    {
                        "step_id": step.get("id"),
                        "line": exc.lineno,
                        "offset": exc.offset,
                        "message": exc.msg,
                    },
                ) from exc


def _file_write_command(file_writes: list) -> list:
    """Compile structured file contents into deterministic sandbox argv."""
    payload = json.dumps(file_writes, ensure_ascii=False).encode("utf-8")
    encoded = base64.b64encode(payload).decode("ascii")
    code = (
        "import base64,json,pathlib;"
        f"items=json.loads(base64.b64decode('{encoded}'));"
        "[(pathlib.Path(x['path']).parent.mkdir(parents=True,exist_ok=True),"
        "pathlib.Path(x['path']).write_text(x['content'],encoding='utf-8')) for x in items]"
    )
    return ["python", "-c", code]


def resolve_intent_bounded(
    raw_request: str,
    owner_id: str,
    ask_user: Callable[[str], str] = input,
    ledger: Optional[AuditLedger] = None,
) -> dict:
    """
    Collapses: resolve_intent -> validate -> repair -> classify_intent_gate
    -> check_clarification_budget -> request_clarification -> loop, into
    one bounded loop. Returns a FROZEN intent (with owner_id bound) or
    raises PipelineStop.
    """
    if not owner_id or not owner_id.strip():
        raise PipelineStop("missing_owner_id")

    prompt = (PROMPTS_DIR / "resolve_intent.md").read_text()
    context: Dict = {"raw_request": raw_request}
    clarification_round = 0
    repair_attempts = 0

    while True:
        raw = llm_client.call_structured(
            prompt, json.dumps(context, ensure_ascii=False),
            schema=INTENT_SCHEMA, schema_name="intent_contract",
        )
        # owner_id is an identity concern, not something the model decides —
        # inject it after generation, same reasoning as intent_digest not
        # being something build_workflow computes itself.
        raw["owner_id"] = owner_id
        # Lifecycle authority belongs to the runtime, not the provider.
        # Every model response is only a draft until freeze_intent succeeds.
        raw["status"] = "draft"

        try:
            jsonschema.validate(raw, INTENT_SCHEMA)
        except jsonschema.ValidationError as e:
            repair_attempts += 1
            if repair_attempts > MAX_REPAIR_ATTEMPTS:
                if ledger:
                    ledger.append("intent_contract_invalid", {"errors": str(e)}, severity="tier1")
                raise PipelineStop("intent_contract_invalid", {"last_errors": str(e)})
            context["previous_contract"] = raw
            context["validation_errors"] = str(e)
            continue

        blocking = raw.get("uncertainties", {}).get("blocking", [])
        unconfirmed = [
            a for a in raw.get("assumptions", [])
            if a.get("materiality") in ("material", "blocking") and not a.get("confirmed")
        ]

        if not blocking and not unconfirmed:
            if not raw.get("acceptance_criteria"):
                raise PipelineStop("intent_contract_invalid", {"reason": "no_acceptance_criteria"})
            frozen = contracts.freeze_intent(raw)
            if ledger:
                ledger.append("intent_frozen", {
                    "owner_id": owner_id,
                    "digest": contracts.compute_intent_digest(frozen),
                }, severity="tier0")
            return frozen

        if clarification_round >= MAX_CLARIFICATION_ROUNDS:
            if ledger:
                ledger.append("clarification_budget_exhausted", {
                    "owner_id": owner_id, "blocking": blocking,
                }, severity="tier1")
            raise PipelineStop(
                "blocking_uncertainty_unresolved_after_max_clarification_rounds",
                {"blocking": blocking, "unconfirmed_assumptions": unconfirmed},
            )

        clarification_round += 1
        questions = [u["description"] for u in blocking][:3] or \
            [f"Confirm: {a['description']}" for a in unconfirmed][:3]
        answer = ask_user("\n".join(questions) + "\n> ")

        context["raw_request"] = (
            raw_request
            + f"\n\n[Clarification round {clarification_round}]\nQ: {questions}\nA: {answer}"
        )
        context.pop("previous_contract", None)
        context.pop("validation_errors", None)
        repair_attempts = 0


def build_workflow_bounded(
    intent: dict,
    actor_id: Optional[str] = None,
    ledger: Optional[AuditLedger] = None,
) -> dict:
    """
    Collapses: build_workflow -> validate -> repair -> classify_complexity
    -> route_by_complexity -> build_workflow_lite, into one function.
    Fast-track is a prompt parameter, not a separate graph branch.
    """
    prompt = (PROMPTS_DIR / "build_workflow.md").read_text()
    digest = contracts.compute_intent_digest(intent)

    deliverables = intent.get("deliverables", [])
    ac = intent.get("acceptance_criteria", [])
    constraints = intent.get("constraints", [])
    requested_side_effects = intent.get("side_effects", {}).get("requested", [])
    fast_track = (
        len(deliverables) <= 1
        and len(ac) <= 3
        and len(constraints) <= 1
        and not requested_side_effects
    )

    context = {"frozen_intent": intent, "intent_digest": digest, "fast_track": fast_track}
    attempts = 0

    while True:
        raw = llm_client.call_structured(
            prompt, json.dumps(context, ensure_ascii=False),
            schema=WORKFLOW_SCHEMA, schema_name="workflow_contract",
        )
        # Approval is granted only by contracts.approve_workflow below.
        raw["status"] = "draft"
        try:
            jsonschema.validate(raw, WORKFLOW_SCHEMA)
            _validate_literal_commands(raw)
            approved = contracts.approve_workflow(intent, raw, actor_id=actor_id)
            if ledger:
                ledger.append("workflow_approved", {
                    "owner_id": intent.get("owner_id"),
                    "digest": approved["intent_digest"],
                    "step_count": len(approved["steps"]),
                }, severity="tier0")
            return approved
        except jsonschema.ValidationError as e:
            attempts += 1
            if attempts > MAX_REPAIR_ATTEMPTS:
                if ledger:
                    ledger.append("workflow_contract_invalid", {"errors": str(e)}, severity="tier1")
                raise PipelineStop("workflow_contract_invalid", {"errors": str(e)})
            context["previous_workflow"] = raw
            context["validation_errors"] = str(e)
        except contracts.ContractError as e:
            if e.reason in ("intent_digest_mismatch", "actor_mismatch"):
                if ledger:
                    ledger.append(e.reason, e.details, severity="tier2")
                raise PipelineStop("frozen_intent_requires_revision"
                                    if e.reason == "intent_digest_mismatch"
                                    else "actor_mismatch", e.details)
            attempts += 1
            if attempts > MAX_REPAIR_ATTEMPTS:
                if ledger:
                    ledger.append("workflow_contract_invalid", e.details, severity="tier1")
                raise PipelineStop("workflow_contract_invalid", e.details)
            context["previous_workflow"] = raw
            context["validation_errors"] = {"reason": e.reason, "details": e.details}


def _workflow_for_repair(workflow: dict) -> dict:
    """Remove runtime-only execution evidence before sending a workflow
    through the contract schema again."""
    cleaned = {k: v for k, v in workflow.items() if not k.startswith("_")}
    cleaned["steps"] = [
        {k: v for k, v in step.items() if not k.startswith("_")}
        for step in workflow.get("steps", [])
    ]
    return cleaned


def _validate_workflow_revision(previous: dict, candidate: dict) -> None:
    previous_revision = previous.get("revision")
    expected_revision = previous_revision + 1 if isinstance(previous_revision, int) else None
    if candidate.get("revision") != expected_revision:
        raise contracts.ContractError(
            "invalid_repair_revision",
            {"expected": expected_revision, "found": candidate.get("revision")},
        )
    if candidate.get("supersedes") != previous_revision:
        raise contracts.ContractError(
            "invalid_repair_supersedes",
            {"expected": previous_revision, "found": candidate.get("supersedes")},
        )
    history = candidate.get("revision_history", [])
    if not history or history[-1].get("revision") != expected_revision:
        raise contracts.ContractError(
            "missing_repair_history",
            {"expected_revision": expected_revision},
        )


def repair_workflow_bounded(
    intent: dict, previous_workflow: dict, mechanical_failure: dict,
    actor_id: Optional[str] = None, ledger: Optional[AuditLedger] = None,
) -> dict:
    """Build and approve a bounded workflow revision from mechanical facts.

    The intent remains frozen. A repair must increment revision metadata,
    retain the same intent digest, pass schema/coverage/DAG checks, and pass
    literal-command validation before it can execute.
    """
    prompt = (PROMPTS_DIR / "repair_workflow.md").read_text()
    previous = _workflow_for_repair(previous_workflow)
    context = {
        "frozen_intent": intent,
        "intent_digest": contracts.compute_intent_digest(intent),
        "previous_workflow": previous,
        "mechanical_failure": mechanical_failure,
    }
    attempts = 0

    while True:
        raw = llm_client.call_structured(
            prompt, json.dumps(context, ensure_ascii=False),
            schema=WORKFLOW_SCHEMA, schema_name="workflow_repair",
        )
        raw["status"] = "draft"
        for step in raw.get("steps", []):
            if step.get("kind", "action") == "action":
                step["status"] = "pending"
        try:
            jsonschema.validate(raw, WORKFLOW_SCHEMA)
            _validate_workflow_revision(previous, raw)
            _validate_literal_commands(raw)
            approved = contracts.approve_workflow(intent, raw, actor_id=actor_id)
            if ledger:
                ledger.append("workflow_repair_approved", {
                    "owner_id": intent.get("owner_id"),
                    "revision": approved["revision"],
                    "supersedes": approved.get("supersedes"),
                    "failure": mechanical_failure,
                }, severity="tier1")
            return approved
        except jsonschema.ValidationError as exc:
            error = {"reason": "workflow_contract_invalid", "details": str(exc)}
        except contracts.ContractError as exc:
            if exc.reason in ("intent_digest_mismatch", "actor_mismatch"):
                if ledger:
                    ledger.append(exc.reason, exc.details, severity="tier2")
                raise PipelineStop(exc.reason, exc.details) from exc
            error = {"reason": exc.reason, "details": exc.details}

        attempts += 1
        if attempts > MAX_REPAIR_ATTEMPTS:
            if ledger:
                ledger.append("workflow_repair_invalid", error, severity="tier1")
            raise PipelineStop("workflow_repair_invalid", error)
        context["previous_repair_proposal"] = raw
        context["validation_errors"] = error


def execute_with_mechanical_repair(
    intent: dict, workflow: dict, workspace_dir: str,
    mechanical_check: Optional[Callable[[dict, dict, str], dict]] = None,
    actor_id: Optional[str] = None, ledger: Optional[AuditLedger] = None,
    use_docker: bool = True, allow_unsafe_fallback: bool = False,
    max_repair_rounds: int = 2,
    verifier_specs: Optional[Dict[str, dict]] = None,
    transactional: bool = True,
    run_store: Optional[RunStore] = None, run_id: Optional[str] = None,
    observability=None,
) -> dict:
    """Execute, independently check, and boundedly repair a workflow.

    Production callers should provide trusted `verifier_specs`; the registry
    computes pass/fail and structural evidence. `mechanical_check` remains as
    a compatibility/testing hook and is mutually exclusive with specs.
    """
    if (mechanical_check is None) == (verifier_specs is None):
        raise ValueError("provide exactly one of mechanical_check or verifier_specs")
    current = workflow
    history = []
    target_workspace = Path(workspace_dir).resolve()
    target_workspace.mkdir(parents=True, exist_ok=True)
    effective_run_id = run_id or (ledger.run_id if ledger else None) or str(uuid.uuid4())
    if ledger and run_id and ledger.run_id != run_id:
        raise ValueError("run_id must match the audit ledger run_id")
    if run_store is not None:
        if effective_run_id is not None:
            run_record = run_store.begin_or_resume(effective_run_id, {
                "workspace": str(target_workspace),
                "workflow_revision": workflow.get("revision"),
            })
        else:
            run_record = run_store.begin(metadata={
                "workspace": str(target_workspace),
                "workflow_revision": workflow.get("revision"),
            })
        effective_run_id = run_record["run_id"]
    run_finished = False
    retained_stage: Optional[Path] = None
    execute_step_ids: Optional[set] = None
    obs = observability or default_observability
    trace = obs.span("mechanical_execution", run_id=effective_run_id)
    trace.__enter__()
    obs.event("workflow_execution_started", revision=workflow.get("revision"))

    try:
        for repair_round in range(max_repair_rounds + 1):
            attempt_workspace = target_workspace
            stage_dir = None
            if transactional:
                stage_dir = Path(tempfile.mkdtemp(
                    prefix=f".iw-stage-r{repair_round}-", dir=target_workspace.parent,
                ))
                base = retained_stage or target_workspace
                shutil.copytree(base, stage_dir, dirs_exist_ok=True)
                attempt_workspace = stage_dir
                if execute_step_ids:
                    _clear_step_outputs(attempt_workspace, current, execute_step_ids)
            executed = execute_workflow(
                intent, current, str(attempt_workspace), use_docker=use_docker,
                allow_unsafe_fallback=allow_unsafe_fallback,
                execute_step_ids=execute_step_ids if transactional else None,
            )
            if verifier_specs is not None:
                check = verifiers.run_mechanical_verifiers(
                    intent, executed, str(attempt_workspace), verifier_specs,
                )
            else:
                check = mechanical_check(intent, executed, str(attempt_workspace))
            results = check.get("results", {})
            failed_steps = [
                {"id": step.get("id"), "status": step.get("status"),
                 "stderr": redact(step.get("_result", {}).get("stderr", ""))}
                for step in executed.get("steps", [])
                if step.get("status") not in ("completed", "skipped")
            ]
            required_mechanical = [
                ac["id"] for ac in intent.get("acceptance_criteria", [])
                if ac.get("required") and ac.get("automation") == "mechanical"
            ]
            failed_criteria = {
                ac_id: results.get(ac_id, "unknown")
                for ac_id in required_mechanical
                if results.get(ac_id) != "passed"
            }
            attempt = {
                "round": repair_round, "revision": executed.get("revision"),
                "failed_steps": failed_steps, "failed_criteria": failed_criteria,
                "evidence": redact(check.get("evidence", {})),
            }
            history.append(attempt)
            obs.event(
                "mechanical_round_completed",
                level="warning" if failed_steps or failed_criteria else "info",
                round=repair_round, revision=executed.get("revision"),
                failed_step_count=len(failed_steps),
                failed_criterion_count=len(failed_criteria),
            )
            if run_store is not None:
                run_store.checkpoint(effective_run_id, {
                    "phase": "mechanical_check", "round": repair_round,
                    "revision": executed.get("revision"),
                    "failed_steps": failed_steps,
                    "failed_criteria": failed_criteria,
                })

            if not failed_steps and not failed_criteria:
                if transactional and stage_dir is not None:
                    _promote_workspace(stage_dir, target_workspace)
                    stage_dir = None
                response = {
                    "workflow": executed, "mechanical_results": results,
                    "repair_history": history, "workspace": str(target_workspace),
                    "run_id": effective_run_id,
                }
                if run_store is not None:
                    run_store.finish(effective_run_id, "completed", {
                        "revision": executed.get("revision"),
                        "mechanical_results": results,
                    })
                    run_finished = True
                obs.event(
                    "workflow_execution_completed", revision=executed.get("revision"),
                    repair_rounds=repair_round,
                )
                return response
            if repair_round >= max_repair_rounds:
                if ledger:
                    ledger.append("mechanical_repair_exhausted", attempt, severity="tier1")
                obs.event(
                    "mechanical_repair_exhausted", level="error",
                    round=repair_round, revision=executed.get("revision"),
                )
                raise PipelineStop(
                    "mechanical_repair_exhausted",
                    {"attempts": history, "last_workflow": executed},
                )

            repaired = repair_workflow_bounded(
                intent, executed, attempt, actor_id=actor_id, ledger=ledger,
            )
            if transactional:
                execute_step_ids = _affected_step_ids(repaired, attempt)
                if retained_stage is not None:
                    shutil.rmtree(retained_stage, ignore_errors=True)
                retained_stage = stage_dir
                stage_dir = None
            current = repaired
    except Exception as exc:
        if run_store is not None and not run_finished:
            reason = exc.reason if isinstance(exc, PipelineStop) else type(exc).__name__
            run_store.finish(effective_run_id, "failed", {"reason": reason})
        obs.event(
            "workflow_execution_failed", level="error",
            error_type=type(exc).__name__,
        )
        raise
    finally:
        if retained_stage is not None:
            shutil.rmtree(retained_stage, ignore_errors=True)
        if "stage_dir" in locals() and stage_dir is not None:
            shutil.rmtree(stage_dir, ignore_errors=True)
        trace.__exit__(*__import__("sys").exc_info())


def _affected_step_ids(workflow: dict, failure: dict) -> set:
    """Return failed/criterion-owning steps and their downstream closure."""
    steps = workflow.get("steps", [])
    known = {step.get("id") for step in steps}
    affected = {
        item.get("id") for item in failure.get("failed_steps", [])
        if item.get("id") in known
    }
    failed_criteria = set(failure.get("failed_criteria", {}))
    for step in steps:
        coverage = step.get(
            "acceptance_criteria", step.get("covers_acceptance_criteria", [])
        )
        if failed_criteria.intersection(coverage):
            affected.add(step["id"])
    if not affected:
        return known
    changed = True
    while changed:
        changed = False
        for step in steps:
            dependencies = step.get("dependencies", step.get("depends_on", []))
            if step["id"] not in affected and affected.intersection(
                dependencies
            ):
                affected.add(step["id"])
                changed = True
    return affected


def _clear_step_outputs(workspace: Path, workflow: dict, step_ids: set) -> None:
    """Remove declared outputs of steps that will be rerun, without path escape."""
    workspace = workspace.resolve()
    for step in workflow.get("steps", []):
        if step.get("id") not in step_ids:
            continue
        for relative in step.get("expected_outputs", []):
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts:
                raise PipelineStop("unsafe_expected_output", {"path": relative})
            candidate = workspace.joinpath(rel)
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink(missing_ok=True)
            elif candidate.is_dir():
                shutil.rmtree(candidate)


def _promote_workspace(stage: Path, target: Path) -> None:
    """Atomically promote a same-filesystem staging directory with rollback."""
    if stage.parent != target.parent:
        raise ValueError("stage and target must share a parent for atomic promotion")
    backup = target.parent / f".iw-backup-{target.name}-{os.getpid()}"
    if backup.exists():
        raise RuntimeError(f"workspace backup already exists: {backup}")
    os.replace(target, backup)
    try:
        os.replace(stage, target)
    except Exception:
        os.replace(backup, target)
        raise
    shutil.rmtree(backup)


def execute_workflow(
    intent: dict, workflow: dict, workspace_dir: str,
    use_docker: bool = True, allow_unsafe_fallback: bool = False,
    execute_step_ids: Optional[set] = None,
) -> dict:
    """
    Runs each step's declared command inside the sandbox configured from
    the frozen intent's constraints. `execute_workflow.invariants` from
    the original design (forbidden_side_effects_must_not_occur, etc.) are
    no longer text a step is supposed to remember — they are compiled by
    permission_broker.derive_policy into what the sandbox physically
    permits, before any step runs.

    `allow_unsafe_fallback` must be set explicitly to use
    `run_step_local_unsafe` when `use_docker=False` — it is NOT a
    security boundary and this call site is where that fact has to be
    acknowledged out loud, not assumed from Docker merely being
    unavailable.
    """
    policy = derive_policy(intent, workspace_dir)
    if use_docker:
        runner = sandbox.run_step
    else:
        def runner(command, policy, workspace_dir):
            return sandbox.run_step_local_unsafe(
                command, policy, workspace_dir, allow_unsafe=allow_unsafe_fallback
            )

    executed = dict(workflow)
    steps_by_id = {s["id"]: dict(s) for s in executed["steps"]}
    # Execute in dependency order, not the order the LLM happened to list
    # steps in — a human_gate or any other step must actually wait for
    # what it depends on, not just be listed after it (finding #4).
    ordered_steps = contracts.topological_order(list(steps_by_id.values()))

    for step in ordered_steps:
        if execute_step_ids is not None and step["id"] not in execute_step_ids:
            step["status"] = "completed"
            step["_reused"] = True
            steps_by_id[step["id"]] = step
            continue
        if step.get("kind") == "human_gate":
            # Never auto-executed, regardless of `command`. This is a
            # structural halt, not a prompt instruction to "remember" —
            # the loop simply does not run a runner for this kind.
            step["status"] = "blocked"
            steps_by_id[step["id"]] = step
            break

        command = step.get("command")
        if step.get("file_writes"):
            command = _file_write_command(step["file_writes"])
        if not command:
            step["status"] = "skipped"
            steps_by_id[step["id"]] = step
            continue

        before = sandbox.snapshot_workspace(workspace_dir)
        result = runner(command, policy, workspace_dir)
        after = sandbox.snapshot_workspace(workspace_dir)

        step["status"] = "completed" if result["exit_code"] == 0 else "failed"
        step["_result"] = result
        # Real fact about what the step touched, independent of exit_code
        # or the model's later self-report — closes finding #3's gap
        # where "exit_code==0" was being treated as proof of file-level
        # side effects it never actually checked.
        step["_workspace_diff"] = sandbox.diff_workspace(before, after)
        steps_by_id[step["id"]] = step

        if step["status"] == "failed":
            break

    # Preserve the workflow's original declared step order in the
    # returned artifact (that's what the schema/approval digest refers
    # to) — only the *execution* order was topological, not the stored
    # representation.
    executed["steps"] = [steps_by_id[s["id"]] for s in workflow["steps"]]
    return executed


def approve_human_gate(workflow: dict, step_id: str, approver_id: str, ledger: Optional[AuditLedger] = None) -> dict:
    """
    The only way a `kind: human_gate` step's status can become
    'completed'. There is deliberately no path from execute_workflow
    itself to set this — it must come from an explicit call naming the
    approver, so it always shows up as a distinct, attributable action
    in the audit trail rather than something a step transitions itself
    into.
    """
    updated = dict(workflow)
    steps = [dict(s) for s in updated["steps"]]
    found = False
    for step in steps:
        if step["id"] == step_id:
            if step.get("kind") != "human_gate":
                raise contracts.ContractError("not_a_human_gate", {"step_id": step_id})
            step["status"] = "completed"
            step["_approved_by"] = approver_id
            found = True
            break
    if not found:
        raise contracts.ContractError("unknown_step_id", {"step_id": step_id})

    updated["steps"] = steps
    if ledger:
        ledger.append("human_gate_approved", {
            "step_id": step_id, "approver_id": approver_id,
        }, severity="tier1")
    return updated


def verify(
    intent: dict,
    workflow: dict,
    completion_report: dict,
    mechanical_results: Optional[Dict[str, str]] = None,
    actor_id: Optional[str] = None,
    ledger: Optional[AuditLedger] = None,
) -> dict:
    """Raises contracts.ContractError on any failed/unknown required
    criterion, incomplete required step, unapproved human_gate, actor
    mismatch, or observed forbidden side effect. Returns the completion
    summary (including which criteria are pending_human_review) on
    success."""
    try:
        result = contracts.check_completion(
            intent, workflow, completion_report, mechanical_results, actor_id=actor_id
        )
        if ledger:
            ledger.append("completion_verified", {
                "owner_id": intent.get("owner_id"),
                "pending_human_review": result["pending_human_review"],
            }, severity="tier0" if not result["pending_human_review"] else "tier1")
        return result
    except contracts.ContractError as e:
        if ledger:
            severity = "tier2" if e.reason in (
                "forbidden_side_effects_observed", "actor_mismatch", "human_gate_not_approved"
            ) else "tier1"
            ledger.append(f"completion_rejected:{e.reason}", e.details, severity=severity)
        raise
