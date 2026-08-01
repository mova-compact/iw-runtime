"""
contracts.py — the non-negotiable, non-LLM core of the runtime.

Every function here is pure (no I/O, no LLM calls) and independently
testable. Nothing downstream is allowed to bypass these checks by
self-report: freeze/approve/verify recompute everything from the actual
data, never from a model's claim about the data.
"""

import hashlib
import json
from typing import Dict, List, Optional


# Fields that constitute the user's frozen commitment. Anything outside
# this list (status, version, non-blocking/residual uncertainties) may
# change after freeze without invalidating an already-approved workflow.
FROZEN_INTENT_FIELDS = [
    "objective",
    "subject",
    "deliverables",
    "scope",
    "constraints",
    "authoritative_inputs",
    "side_effects",
    "acceptance_criteria",
]


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_intent_digest(intent: dict) -> str:
    frozen_view = {k: intent.get(k) for k in FROZEN_INTENT_FIELDS}
    payload = canonical_json(frozen_view).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


class ContractError(Exception):
    def __init__(self, reason: str, details: Optional[dict] = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


# ---------------------------------------------------------------------------
# freeze
# ---------------------------------------------------------------------------

def check_acceptance_criteria_consistency(intent: dict) -> None:
    """
    MOVA-style cross-field validator: a self-report label is only useful
    if it can't be filled in for free. A criterion marked
    automation='manual' with `required: true` must carry a non-empty
    manual_justification — a reason, not just a tag. This doesn't stop
    someone from writing a bad justification, but it turns a silent,
    zero-cost 'manual' label into a logged, falsifiable claim, which is
    the same move MOVA makes with confidence_hint requiring
    evidence_basis=dynamic.
    """
    bad = []
    for ac in intent.get("acceptance_criteria", []):
        if ac.get("required") and ac.get("automation", "manual") == "manual":
            if not (ac.get("manual_justification") or "").strip():
                bad.append(ac["id"])
    if bad:
        raise ContractError("manual_criterion_missing_justification", {"criteria": bad})


def freeze_intent(intent: dict) -> dict:
    required = [
        "objective", "subject", "deliverables", "scope",
        "acceptance_criteria", "uncertainties", "assumptions", "owner_id",
    ]
    missing = [f for f in required if f not in intent]
    if missing:
        raise ContractError("intent_missing_fields", {"missing": missing})

    if not str(intent.get("owner_id", "")).strip():
        raise ContractError("intent_missing_owner")

    check_acceptance_criteria_consistency(intent)

    blocking = intent.get("uncertainties", {}).get("blocking", [])
    if blocking:
        raise ContractError("blocking_uncertainty_present", {"count": len(blocking)})

    unconfirmed = [
        a for a in intent.get("assumptions", [])
        if a.get("materiality") in ("material", "blocking") and not a.get("confirmed")
    ]
    if unconfirmed:
        raise ContractError("unconfirmed_material_assumption", {"assumptions": unconfirmed})

    required_ac = {
        ac["id"] for ac in intent.get("acceptance_criteria", []) if ac.get("required")
    }
    if not required_ac:
        raise ContractError("no_required_acceptance_criteria")

    frozen = dict(intent)
    frozen["status"] = "frozen"
    frozen["version"] = intent.get("version", 1)
    return frozen


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def topological_order(steps: List[dict]) -> List[dict]:
    """
    Returns steps reordered so every step appears after all of its
    `dependencies`. approve_workflow already proves the graph is acyclic;
    this function is what actually makes execution respect that graph,
    instead of trusting whatever order the LLM happened to list steps in
    — which was finding #4: a human_gate could sit later in the list than
    a step that logically depended on it finishing first, and the
    executor would run steps strictly in list order regardless.
    """
    by_id = {s["id"]: s for s in steps}
    in_degree = {sid: 0 for sid in by_id}
    dependents: Dict[str, List[str]] = {sid: [] for sid in by_id}

    for s in steps:
        for dep in s.get("dependencies", []):
            if dep not in by_id:
                raise ContractError("unknown_dependency_reference", {"missing_step": dep})
            in_degree[s["id"]] += 1
            dependents[dep].append(s["id"])

    # Deterministic order: process ready nodes in their original list
    # order, not arbitrary set iteration order, so output is reproducible.
    ready = [s["id"] for s in steps if in_degree[s["id"]] == 0]
    ordered: List[str] = []

    while ready:
        ready.sort(key=lambda sid: [s["id"] for s in steps].index(sid))
        current = ready.pop(0)
        ordered.append(current)
        for dependent in dependents[current]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(steps):
        # Should be unreachable if approve_workflow's cycle check already
        # ran, but fail loudly rather than silently drop steps if it
        # didn't (e.g. this is called on an unapproved workflow).
        raise ContractError("dependency_cycle_at_execution_time", {
            "resolved": ordered, "total_steps": len(steps),
        })

    return [by_id[sid] for sid in ordered]


def detect_cycle(steps: List[dict]) -> Optional[List[str]]:
    graph = {s["id"]: s.get("dependencies", []) for s in steps}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {sid: WHITE for sid in graph}
    path: List[str] = []

    def visit(node: str) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                raise ContractError("unknown_dependency_reference", {"missing_step": dep})
            if color[dep] == GRAY:
                start = path.index(dep)
                return path[start:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        color[node] = BLACK
        return None

    for sid in graph:
        if color[sid] == WHITE:
            found = visit(sid)
            if found:
                return found
    return None


def approve_workflow(intent: dict, workflow: dict, actor_id: Optional[str] = None) -> dict:
    if intent.get("status") != "frozen":
        raise ContractError("intent_not_frozen")

    if actor_id is not None and actor_id != intent.get("owner_id"):
        raise ContractError("actor_mismatch", {
            "expected_owner": intent.get("owner_id"),
            "actor_id": actor_id,
        })

    expected = compute_intent_digest(intent)
    actual = workflow.get("intent_digest")
    if actual != expected:
        raise ContractError("intent_digest_mismatch", {
            "expected": expected,
            "found": actual,
            "meaning": "frozen intent changed, or workflow was built against "
                       "a stale version — revise the intent, don't approve.",
        })

    steps = workflow.get("steps", [])
    if not steps:
        raise ContractError("empty_workflow")

    ids = [s["id"] for s in steps]
    if len(ids) != len(set(ids)):
        raise ContractError("duplicate_step_ids")

    cycle = detect_cycle(steps)
    if cycle:
        raise ContractError("dependency_cycle", {"cycle": cycle})

    required_ac = {ac["id"] for ac in intent["acceptance_criteria"] if ac.get("required")}
    all_ac = {ac["id"] for ac in intent["acceptance_criteria"]}
    covered = set()
    for s in steps:
        covered.update(s.get("acceptance_criteria", []))

    uncovered = required_ac - covered
    if uncovered:
        raise ContractError("uncovered_required_acceptance_criteria", {"uncovered": sorted(uncovered)})

    unknown = covered - all_ac
    if unknown:
        raise ContractError("step_references_unknown_acceptance_criterion", {"unknown": sorted(unknown)})

    approved = dict(workflow)
    approved["status"] = "approved"
    return approved


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def check_completion(
    intent: dict,
    workflow: dict,
    report: dict,
    mechanical_results: Optional[Dict[str, str]] = None,
    actor_id: Optional[str] = None,
) -> dict:
    """
    For any required criterion whose verification_method is 'mechanical',
    the pass/fail MUST come from `mechanical_results` (computed by the
    runtime itself — test exit codes, file diffs, external checks) and
    NOT from the model's completion report. Trusting a model's claim for
    a criterion that could have been checked mechanically is exactly the
    self-report gap this runtime exists to close.

    For 'manual' criteria, the model's report is accepted but the
    criterion is flagged for human review rather than silently trusted
    as equivalent to a mechanical pass.

    KNOWN RESIDUAL GAP (documented, not fixed by this function):
    `report["forbidden_side_effects_observed"]` is self-reported. For
    side effects the sandbox already blocks physically (network egress
    outside the allowlist, filesystem writes outside the workspace),
    this field is redundant with real enforcement. For side effects that
    are *semantically* forbidden but technically reachable within what
    the sandbox permits (e.g. "don't touch the users table" when the
    container legitimately has DB access for other reasons), this field
    is the only signal, and it is exactly as trustworthy as the model
    that wrote it — not independently verified. Do not treat a clean
    report here as proof; treat it as one more input alongside
    out-of-band monitoring for anything where that distinction matters.
    """
    # --- Gate 0: structural checks, evaluated unconditionally and FIRST.
    # This ordering is deliberate: MOVA's BF-001/BF-002 bugs happened
    # because a report/event-derived branch was evaluated before the
    # permission/allowed-transition check, letting a special case skip
    # the gate entirely. Here, ownership, human-gate integrity, and
    # forbidden-side-effect reporting are checked before anything
    # derived from `report` or `mechanical_results` is trusted for a
    # pass/fail decision, so no downstream branch can short-circuit past
    # them.
    if actor_id is not None and actor_id != intent.get("owner_id"):
        raise ContractError("actor_mismatch", {
            "expected_owner": intent.get("owner_id"),
            "actor_id": actor_id,
        })

    if report.get("forbidden_side_effects_observed"):
        raise ContractError(
            "forbidden_side_effects_observed",
            {"observed": report["forbidden_side_effects_observed"]},
        )

    step_status = {s["id"]: s.get("status") for s in workflow.get("steps", [])}
    bad_steps = [sid for sid, st in step_status.items() if st not in ("completed", "skipped")]
    if bad_steps:
        raise ContractError("required_steps_not_terminal", {"steps": bad_steps})

    human_gate_ids = [s["id"] for s in workflow.get("steps", []) if s.get("kind") == "human_gate"]
    unapproved_gates = [
        sid for sid in human_gate_ids
        if step_status.get(sid) != "completed"
    ]
    if unapproved_gates:
        raise ContractError("human_gate_not_approved", {"gates": unapproved_gates})

    # --- Gate 1: criterion evaluation, only reached once Gate 0 passed.
    mechanical_results = mechanical_results or {}
    ac_automation = {ac["id"]: ac.get("automation", "manual") for ac in intent["acceptance_criteria"]}
    required_ac = {ac["id"] for ac in intent["acceptance_criteria"] if ac.get("required")}

    reported = {r["criterion_id"]: r["status"] for r in report.get("criterion_results", [])}

    final_status: Dict[str, str] = {}
    needs_human_review: List[str] = []

    for cid in required_ac:
        method = ac_automation.get(cid, "manual")
        if method == "mechanical":
            if cid not in mechanical_results:
                raise ContractError(
                    "mechanical_criterion_not_independently_checked",
                    {"criterion": cid},
                )
            final_status[cid] = mechanical_results[cid]
        else:
            final_status[cid] = reported.get(cid, "unknown")
            needs_human_review.append(cid)

    failed = [cid for cid, st in final_status.items() if st == "failed"]
    unknown = [cid for cid, st in final_status.items() if st == "unknown"]

    if failed or unknown:
        raise ContractError("required_criterion_not_passed", {
            "failed": failed,
            "unknown": unknown,
        })

    return {
        "status": "completed",
        "criterion_status": final_status,
        "pending_human_review": needs_human_review,
    }
