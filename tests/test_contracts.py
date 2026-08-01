import pytest

from runtime import contracts


def make_intent(**overrides):
    intent = {
        "version": 1,
        "status": "draft",
        "owner_id": "user-123",
        "raw_intent": "test",
        "objective": "test objective",
        "subject": "test subject",
        "deliverables": [{"id": "D-1", "description": "thing", "format": None}],
        "scope": {"included": [], "excluded": []},
        "constraints": [],
        "authoritative_inputs": [],
        "side_effects": {"requested": [], "forbidden": []},
        "acceptance_criteria": [
            {"id": "AC-1", "description": "must pass", "required": True,
             "verification_method": "manual", "automation": "manual",
             "manual_justification": "requires subjective human review of tone"},
            {"id": "AC-2", "description": "optional", "required": False,
             "verification_method": "manual", "automation": "manual",
             "manual_justification": None},
        ],
        "resolved_from_context": [],
        "assumptions": [],
        "uncertainties": {"blocking": [], "non_blocking": [], "residual": []},
    }
    intent.update(overrides)
    return intent


def make_step(sid, ac_ids, deps=None, kind="action"):
    return {
        "id": sid, "action": "do thing", "rationale": "x", "inputs": [],
        "expected_outputs": ["out"], "dependencies": deps or [],
        "tools": [], "acceptance_criteria": ac_ids,
        "completion_check": "check", "failure_handling": "retry",
        "status": "pending", "command": None, "kind": kind,
    }


def make_workflow(digest, steps):
    return {
        "version": 1, "revision": 1, "status": "draft",
        "intent_digest": digest, "steps": steps, "revision_history": [],
    }


# --- freeze ---------------------------------------------------------------

def test_freeze_happy_path():
    frozen = contracts.freeze_intent(make_intent())
    assert frozen["status"] == "frozen"


def test_freeze_rejects_blocking_uncertainty():
    intent = make_intent(uncertainties={
        "blocking": [{"field": "x", "description": "unclear"}],
        "non_blocking": [], "residual": [],
    })
    with pytest.raises(contracts.ContractError) as e:
        contracts.freeze_intent(intent)
    assert e.value.reason == "blocking_uncertainty_present"


def test_freeze_rejects_unconfirmed_material_assumption():
    intent = make_intent(assumptions=[
        {"description": "x", "materiality": "material", "confirmed": False}
    ])
    with pytest.raises(contracts.ContractError) as e:
        contracts.freeze_intent(intent)
    assert e.value.reason == "unconfirmed_material_assumption"


# --- approve ---------------------------------------------------------------

def test_approve_rejects_digest_mismatch():
    intent = contracts.freeze_intent(make_intent())
    workflow = make_workflow("sha256:deadbeef", [make_step("S-1", ["AC-1"])])
    with pytest.raises(contracts.ContractError) as e:
        contracts.approve_workflow(intent, workflow)
    assert e.value.reason == "intent_digest_mismatch"


def test_approve_rejects_uncovered_required_criterion():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-2"])])  # only covers optional AC
    with pytest.raises(contracts.ContractError) as e:
        contracts.approve_workflow(intent, workflow)
    assert e.value.reason == "uncovered_required_acceptance_criteria"
    assert "AC-1" in e.value.details["uncovered"]


def test_approve_rejects_dependency_cycle():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    steps = [
        make_step("S-1", ["AC-1"], deps=["S-2"]),
        make_step("S-2", ["AC-1"], deps=["S-1"]),
    ]
    workflow = make_workflow(digest, steps)
    with pytest.raises(contracts.ContractError) as e:
        contracts.approve_workflow(intent, workflow)
    assert e.value.reason == "dependency_cycle"


def test_approve_accepts_valid_workflow():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    approved = contracts.approve_workflow(intent, workflow)
    assert approved["status"] == "approved"


def test_digest_ignores_non_frozen_fields():
    intent_a = make_intent(status="draft", version=1)
    intent_b = make_intent(status="frozen", version=7)
    assert contracts.compute_intent_digest(intent_a) == contracts.compute_intent_digest(intent_b)


def test_digest_ignores_owner_id():
    # owner_id is an identity/access-control concern, not part of the
    # commitment itself — two intents with identical content but
    # different owners (e.g. after a legitimate handoff) should still
    # digest identically, so a workflow built before a handoff remains
    # approvable after it (ownership is checked separately via actor_id).
    intent_a = make_intent(owner_id="alice")
    intent_b = make_intent(owner_id="bob")
    assert contracts.compute_intent_digest(intent_a) == contracts.compute_intent_digest(intent_b)


def test_digest_changes_when_acceptance_criteria_change():
    intent_a = make_intent()
    intent_b = make_intent(acceptance_criteria=[
        {"id": "AC-1", "description": "different", "required": True,
         "verification_method": "manual", "automation": "manual"}
    ])
    assert contracts.compute_intent_digest(intent_a) != contracts.compute_intent_digest(intent_b)


# --- check_completion --------------------------------------------------

def test_completion_rejects_unchecked_mechanical_criterion():
    intent = make_intent(acceptance_criteria=[
        {"id": "AC-1", "description": "tests pass", "required": True,
         "verification_method": "pytest", "automation": "mechanical"},
    ])
    intent = contracts.freeze_intent(intent)
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    workflow["steps"][0]["status"] = "completed"
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "trust me"}]}

    with pytest.raises(contracts.ContractError) as e:
        contracts.check_completion(intent, workflow, report, mechanical_results={})
    assert e.value.reason == "mechanical_criterion_not_independently_checked"


def test_completion_uses_mechanical_result_not_model_report():
    intent = make_intent(acceptance_criteria=[
        {"id": "AC-1", "description": "tests pass", "required": True,
         "verification_method": "pytest", "automation": "mechanical"},
    ])
    intent = contracts.freeze_intent(intent)
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    workflow["steps"][0]["status"] = "completed"
    # Model claims "passed" but the runtime's own check says "failed" —
    # the runtime's result must win.
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "looks fine"}]}

    with pytest.raises(contracts.ContractError) as e:
        contracts.check_completion(intent, workflow, report, mechanical_results={"AC-1": "failed"})
    assert e.value.reason == "required_criterion_not_passed"
    assert "AC-1" in e.value.details["failed"]


def test_completion_flags_manual_criteria_for_review():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    workflow["steps"][0]["status"] = "completed"
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "manual check"}]}

    result = contracts.check_completion(intent, workflow, report, mechanical_results={})
    assert result["status"] == "completed"
    assert "AC-1" in result["pending_human_review"]


# --- ownership / actor binding (MOVA AUTHN-003 class fix) -----------------

def test_freeze_requires_owner_id():
    intent = make_intent(owner_id="")
    with pytest.raises(contracts.ContractError) as e:
        contracts.freeze_intent(intent)
    assert e.value.reason == "intent_missing_owner"


def test_approve_rejects_actor_mismatch():
    intent = contracts.freeze_intent(make_intent(owner_id="alice"))
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    with pytest.raises(contracts.ContractError) as e:
        contracts.approve_workflow(intent, workflow, actor_id="mallory")
    assert e.value.reason == "actor_mismatch"


def test_approve_accepts_matching_actor():
    intent = contracts.freeze_intent(make_intent(owner_id="alice"))
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    approved = contracts.approve_workflow(intent, workflow, actor_id="alice")
    assert approved["status"] == "approved"


def test_check_completion_rejects_actor_mismatch_before_criteria_are_evaluated():
    """
    Order-of-operations check, mirroring MOVA's BF-001/BF-002: the actor
    check must fire even when the criterion data would otherwise look
    perfectly valid — an attacker who forged a plausible-looking report
    must not slip through because the report itself 'looks fine'.
    """
    intent = contracts.freeze_intent(make_intent(owner_id="alice"))
    digest = contracts.compute_intent_digest(intent)
    workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
    workflow["steps"][0]["status"] = "completed"
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "e"}]}

    with pytest.raises(contracts.ContractError) as e:
        contracts.check_completion(intent, workflow, report, mechanical_results={}, actor_id="mallory")
    assert e.value.reason == "actor_mismatch"


# --- manual_justification cross-field check (MOVA confidence_hint style) --

def test_freeze_rejects_manual_required_criterion_without_justification():
    intent = make_intent(acceptance_criteria=[
        {"id": "AC-1", "description": "x", "required": True,
         "verification_method": "manual", "automation": "manual",
         "manual_justification": ""},
    ])
    with pytest.raises(contracts.ContractError) as e:
        contracts.freeze_intent(intent)
    assert e.value.reason == "manual_criterion_missing_justification"
    assert "AC-1" in e.value.details["criteria"]


def test_freeze_accepts_manual_required_criterion_with_justification():
    intent = make_intent(acceptance_criteria=[
        {"id": "AC-1", "description": "x", "required": True,
         "verification_method": "manual", "automation": "manual",
         "manual_justification": "no automated test exists for tone/style review"},
    ])
    frozen = contracts.freeze_intent(intent)
    assert frozen["status"] == "frozen"


# --- human_gate (MOVA HUMAN_GATE / auto-escalation-forbidden pattern) -----

def test_completion_blocked_by_unapproved_human_gate():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    steps = [
        make_step("S-1", ["AC-1"], kind="human_gate"),
    ]
    workflow = make_workflow(digest, steps)
    workflow["steps"][0]["status"] = "blocked"  # never auto-completed
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "e"}]}

    with pytest.raises(contracts.ContractError) as e:
        contracts.check_completion(intent, workflow, report, mechanical_results={})
    assert e.value.reason in ("required_steps_not_terminal", "human_gate_not_approved")


def test_completion_succeeds_after_human_gate_explicitly_approved():
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    steps = [make_step("S-1", ["AC-1"], kind="human_gate")]
    workflow = make_workflow(digest, steps)
    workflow["steps"][0]["status"] = "completed"  # only reachable via approve_human_gate in pipeline.py
    report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "e"}]}

    result = contracts.check_completion(intent, workflow, report, mechanical_results={})
    assert result["status"] == "completed"


def test_completion_rejects_non_terminal_step_status_variants():
    """
    Guards against the specific BF-001/BF-002 shape: a step in some
    in-between state ('running', 'blocked') must not be silently treated
    as equivalent to 'completed' just because it isn't 'failed'.
    """
    intent = contracts.freeze_intent(make_intent())
    digest = contracts.compute_intent_digest(intent)
    for bad_status in ("running", "blocked", "pending"):
        workflow = make_workflow(digest, [make_step("S-1", ["AC-1"])])
        workflow["steps"][0]["status"] = bad_status
        report = {"criterion_results": [{"criterion_id": "AC-1", "status": "passed", "evidence": "e"}]}
        with pytest.raises(contracts.ContractError) as e:
            contracts.check_completion(intent, workflow, report, mechanical_results={})
        assert e.value.reason == "required_steps_not_terminal"


# --- topological_order (finding #4: execution must follow the DAG, not
# the LLM's arbitrary list order) ------------------------------------------

def test_topological_order_respects_dependencies():
    # S-3 depends on S-2 depends on S-1, but listed out of order.
    steps = [
        make_step("S-3", ["AC-1"], deps=["S-2"]),
        make_step("S-1", ["AC-1"], deps=[]),
        make_step("S-2", ["AC-1"], deps=["S-1"]),
    ]
    ordered = contracts.topological_order(steps)
    positions = {s["id"]: i for i, s in enumerate(ordered)}
    assert positions["S-1"] < positions["S-2"] < positions["S-3"]


def test_topological_order_is_deterministic_for_independent_branches():
    # S-1 and S-2 are independent (no deps between them); S-3 depends on
    # both. Original list order among independent nodes should be
    # preserved, not shuffled.
    steps = [
        make_step("S-2", ["AC-1"], deps=[]),
        make_step("S-1", ["AC-1"], deps=[]),
        make_step("S-3", ["AC-1"], deps=["S-1", "S-2"]),
    ]
    ordered = contracts.topological_order(steps)
    assert [s["id"] for s in ordered] == ["S-2", "S-1", "S-3"]


def test_topological_order_raises_on_unknown_dependency():
    steps = [make_step("S-1", ["AC-1"], deps=["S-does-not-exist"])]
    with pytest.raises(contracts.ContractError) as e:
        contracts.topological_order(steps)
    assert e.value.reason == "unknown_dependency_reference"


def test_topological_order_raises_on_cycle():
    steps = [
        make_step("S-1", ["AC-1"], deps=["S-2"]),
        make_step("S-2", ["AC-1"], deps=["S-1"]),
    ]
    with pytest.raises(contracts.ContractError) as e:
        contracts.topological_order(steps)
    assert e.value.reason == "dependency_cycle_at_execution_time"
