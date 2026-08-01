from unittest.mock import patch
from pathlib import Path

import pytest

from runtime import pipeline, contracts
from runtime.run_store import RunStore


def _valid_intent(owner_id="alice"):
    return {
        "version": 1, "status": "draft", "owner_id": owner_id,
        "raw_intent": "write a hello world script",
        "objective": "produce a hello world script", "subject": "hello.py",
        "deliverables": [{"id": "D-1", "description": "hello.py", "format": "python"}],
        "scope": {"included": [], "excluded": []},
        "constraints": [], "authoritative_inputs": [],
        "side_effects": {"requested": [], "forbidden": []},
        "acceptance_criteria": [
            {"id": "AC-1", "description": "script runs and prints hello",
             "verification_method": "python hello.py", "required": True, "automation": "mechanical"},
        ],
        "resolved_from_context": [], "assumptions": [],
        "uncertainties": {"blocking": [], "non_blocking": [], "residual": []},
    }


def _blocking_intent():
    intent = _valid_intent()
    intent["uncertainties"] = {
        "blocking": [{"field": "language", "description": "which language?"}],
        "non_blocking": [], "residual": [],
    }
    return intent


def test_resolve_intent_bounded_happy_path():
    with patch("runtime.llm_client.call_structured", return_value=_valid_intent()):
        frozen = pipeline.resolve_intent_bounded("write a hello world script", owner_id="alice")
    assert frozen["status"] == "frozen"


def test_resolve_intent_bounded_stops_after_max_clarification_rounds():
    with patch("runtime.llm_client.call_structured", return_value=_blocking_intent()):
        with pytest.raises(pipeline.PipelineStop) as e:
            pipeline.resolve_intent_bounded(
                "ambiguous request", owner_id="alice", ask_user=lambda q: "still unclear"
            )
    assert e.value.reason == "blocking_uncertainty_unresolved_after_max_clarification_rounds"


def test_build_workflow_bounded_happy_path():
    intent = contracts.freeze_intent(_valid_intent())
    digest = contracts.compute_intent_digest(intent)

    workflow_proposal = {
        "version": 1, "revision": 1, "status": "draft", "intent_digest": digest,
        "steps": [{
            "id": "S-1", "action": "write hello.py", "rationale": "single deliverable",
            "inputs": [], "expected_outputs": ["hello.py"], "dependencies": [],
            "tools": [], "acceptance_criteria": ["AC-1"],
            "completion_check": "run script", "failure_handling": "retry",
            "status": "pending", "command": ["python", "hello.py"],
        }],
        "revision_history": [],
    }

    with patch("runtime.llm_client.call_structured", return_value=workflow_proposal):
        approved = pipeline.build_workflow_bounded(intent)

    assert approved["status"] == "approved"


def test_build_workflow_bounded_stops_on_digest_mismatch():
    intent = contracts.freeze_intent(_valid_intent())

    bad_workflow = {
        "version": 1, "revision": 1, "status": "draft", "intent_digest": "sha256:wrong",
        "steps": [{
            "id": "S-1", "action": "x", "rationale": "x", "inputs": [],
            "expected_outputs": ["out"], "dependencies": [], "tools": [],
            "acceptance_criteria": ["AC-1"], "completion_check": "c",
            "failure_handling": "r", "status": "pending", "command": None,
        }],
        "revision_history": [],
    }

    with patch("runtime.llm_client.call_structured", return_value=bad_workflow):
        with pytest.raises(pipeline.PipelineStop) as e:
            pipeline.build_workflow_bounded(intent)
    assert e.value.reason == "frozen_intent_requires_revision"


def test_build_workflow_bounded_stops_on_actor_mismatch():
    intent = contracts.freeze_intent(_valid_intent(owner_id="alice"))
    digest = contracts.compute_intent_digest(intent)

    workflow_proposal = {
        "version": 1, "revision": 1, "status": "draft", "intent_digest": digest,
        "steps": [{
            "id": "S-1", "action": "write hello.py", "rationale": "x",
            "inputs": [], "expected_outputs": ["hello.py"], "dependencies": [],
            "tools": [], "acceptance_criteria": ["AC-1"], "completion_check": "c",
            "failure_handling": "r", "status": "pending", "command": None,
        }],
        "revision_history": [],
    }

    with patch("runtime.llm_client.call_structured", return_value=workflow_proposal):
        with pytest.raises(pipeline.PipelineStop) as e:
            pipeline.build_workflow_bounded(intent, actor_id="mallory")
    assert e.value.reason == "actor_mismatch"


def _workflow(intent, revision=1, supersedes=None, command=None):
    digest = contracts.compute_intent_digest(intent)
    return {
        "version": 1, "revision": revision, "supersedes": supersedes,
        "status": "approved", "intent_digest": digest,
        "steps": [{
            "id": "S-1", "action": "write hello.py", "rationale": "x",
            "inputs": [], "expected_outputs": ["hello.py"], "dependencies": [],
            "tools": [], "acceptance_criteria": ["AC-1"],
            "completion_check": "run", "failure_handling": "repair",
            "status": "pending", "kind": "action", "command": command,
        }],
        "revision_history": [] if revision == 1 else [{
            "revision": revision, "reason": "mechanical test failed",
            "material_changes": ["fixed test import"],
        }],
    }


def test_repair_workflow_bounded_approves_next_revision():
    intent = contracts.freeze_intent(_valid_intent())
    previous = _workflow(intent)
    repaired = _workflow(
        intent, revision=2, supersedes=1,
        command=["python", "-c", "open('hello.py', 'w').write('print(1)')"],
    )
    with patch("runtime.llm_client.call_structured", return_value=repaired):
        approved = pipeline.repair_workflow_bounded(
            intent, previous, {"failed_criteria": {"AC-1": "failed"}},
            actor_id="alice",
        )
    assert approved["status"] == "approved"
    assert approved["revision"] == 2
    assert approved["supersedes"] == 1


def test_repair_workflow_bounded_repairs_bad_revision_metadata():
    intent = contracts.freeze_intent(_valid_intent())
    previous = _workflow(intent)
    stale = _workflow(intent, revision=1, supersedes=None)
    repaired = _workflow(intent, revision=2, supersedes=1)
    with patch(
        "runtime.llm_client.call_structured", side_effect=[stale, repaired]
    ) as call:
        approved = pipeline.repair_workflow_bounded(
            intent, previous, {"failed_criteria": {"AC-1": "failed"}}
        )
    assert approved["revision"] == 2
    assert call.call_count == 2


def test_execute_with_mechanical_repair_retries_and_returns_pass():
    intent = contracts.freeze_intent(_valid_intent())
    first = _workflow(intent)
    failed = _workflow(intent)
    failed["steps"][0]["status"] = "completed"
    repaired = _workflow(intent, revision=2, supersedes=1)
    passed = _workflow(intent, revision=2, supersedes=1)
    passed["steps"][0]["status"] = "completed"
    checks = [
        {"results": {"AC-1": "failed"}, "evidence": {"tests": "NameError"}},
        {"results": {"AC-1": "passed"}, "evidence": {"tests": "ok"}},
    ]
    with patch("runtime.pipeline.execute_workflow", side_effect=[failed, passed]), patch(
        "runtime.pipeline.repair_workflow_bounded", return_value=repaired
    ) as repair:
        result = pipeline.execute_with_mechanical_repair(
            intent, first, "workspace",
            mechanical_check=lambda *_: checks.pop(0), actor_id="alice",
            transactional=False,
        )
    assert result["mechanical_results"] == {"AC-1": "passed"}
    assert len(result["repair_history"]) == 2
    repair.assert_called_once()


def test_execute_with_mechanical_repair_stops_when_budget_exhausted():
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    executed = _workflow(intent)
    executed["steps"][0]["status"] = "completed"
    with patch("runtime.pipeline.execute_workflow", return_value=executed):
        with pytest.raises(pipeline.PipelineStop) as exc:
            pipeline.execute_with_mechanical_repair(
                intent, workflow, "workspace",
                mechanical_check=lambda *_: {"results": {"AC-1": "failed"}},
                max_repair_rounds=0, transactional=False,
            )
    assert exc.value.reason == "mechanical_repair_exhausted"


def test_execution_persists_run_checkpoint_and_completion(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    executed = _workflow(intent)
    executed["steps"][0]["status"] = "completed"
    store = RunStore(str(tmp_path / "runs"))
    with patch("runtime.pipeline.execute_workflow", return_value=executed):
        result = pipeline.execute_with_mechanical_repair(
            intent, workflow, str(tmp_path / "workspace"),
            mechanical_check=lambda *_: {"results": {"AC-1": "passed"}},
            transactional=False, run_store=store,
        )
    state = store.get(result["run_id"])
    assert state["status"] == "completed"
    assert state["checkpoint"]["phase"] == "mechanical_check"
    assert store.recover_incomplete() == []


def test_execution_persists_terminal_failure(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    executed = _workflow(intent)
    executed["steps"][0]["status"] = "completed"
    store = RunStore(str(tmp_path / "runs"))
    with patch("runtime.pipeline.execute_workflow", return_value=executed):
        with pytest.raises(pipeline.PipelineStop):
            pipeline.execute_with_mechanical_repair(
                intent, workflow, str(tmp_path / "workspace"),
                mechanical_check=lambda *_: {"results": {"AC-1": "failed"}},
                max_repair_rounds=0, transactional=False, run_store=store,
            )
    states = [store.get(path.stem) for path in (tmp_path / "runs").glob("*.json")]
    assert len(states) == 1
    assert states[0]["status"] == "failed"
    assert states[0]["result"]["reason"] == "mechanical_repair_exhausted"


def test_structured_file_writes_execute_without_model_generated_writer(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    workflow["steps"][0]["command"] = None
    workflow["steps"][0]["file_writes"] = [{
        "path": "hello.py", "content": "print('hello')\n",
    }]
    pipeline._validate_literal_commands(workflow)
    executed = pipeline.execute_workflow(
        intent, workflow, str(tmp_path), use_docker=False,
        allow_unsafe_fallback=True,
    )
    assert executed["steps"][0]["status"] == "completed"
    assert (tmp_path / "hello.py").read_text() == "print('hello')\n"


def test_structured_file_writes_reject_path_outside_expected_outputs():
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    workflow["steps"][0]["command"] = None
    workflow["steps"][0]["file_writes"] = [{
        "path": "../escape.py", "content": "bad",
    }]
    with pytest.raises(contracts.ContractError) as exc:
        pipeline._validate_literal_commands(workflow)
    assert exc.value.reason == "invalid_file_write_path"


def test_transactional_repair_does_not_publish_failed_attempt(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    first = _workflow(intent)
    repaired = _workflow(intent, revision=2, supersedes=1)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / "input.txt").write_text("baseline")
    calls = 0

    def execute(_intent, current, workspace, **_kwargs):
        nonlocal calls
        calls += 1
        Path(workspace, "result.txt").write_text("bad" if calls == 1 else "good")
        result = _workflow(intent, revision=current["revision"], supersedes=current.get("supersedes"))
        result["steps"][0]["status"] = "completed"
        return result

    def check(_intent, _workflow_result, workspace):
        value = Path(workspace, "result.txt").read_text()
        if value == "bad":
            assert not (target / "result.txt").exists()
        return {"results": {"AC-1": "passed" if value == "good" else "failed"}}

    with patch("runtime.pipeline.execute_workflow", side_effect=execute), patch(
        "runtime.pipeline.repair_workflow_bounded", return_value=repaired
    ):
        result = pipeline.execute_with_mechanical_repair(
            intent, first, str(target), mechanical_check=check, actor_id="alice",
        )
    assert result["workflow"]["revision"] == 2
    assert (target / "input.txt").read_text() == "baseline"
    assert (target / "result.txt").read_text() == "good"
    assert not list(tmp_path.glob(".iw-stage-*"))
    assert not list(tmp_path.glob(".iw-backup-*"))


def test_transactional_exhaustion_preserves_original_workspace(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    workflow = _workflow(intent)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / "original.txt").write_text("keep")

    def execute(_intent, current, workspace, **_kwargs):
        Path(workspace, "failed.txt").write_text("discard")
        result = _workflow(intent, revision=current["revision"], supersedes=current.get("supersedes"))
        result["steps"][0]["status"] = "completed"
        return result

    with patch("runtime.pipeline.execute_workflow", side_effect=execute):
        with pytest.raises(pipeline.PipelineStop):
            pipeline.execute_with_mechanical_repair(
                intent, workflow, str(target),
                mechanical_check=lambda *_: {"results": {"AC-1": "failed"}},
                max_repair_rounds=0,
            )
    assert (target / "original.txt").read_text() == "keep"
    assert not (target / "failed.txt").exists()


def test_affected_steps_include_failed_step_and_downstream_only():
    workflow = {"steps": [
        {"id": "S-1", "dependencies": [], "acceptance_criteria": []},
        {"id": "S-2", "dependencies": ["S-1"], "acceptance_criteria": ["AC-1"]},
        {"id": "S-3", "dependencies": ["S-2"], "acceptance_criteria": []},
        {"id": "S-4", "dependencies": ["S-1"], "acceptance_criteria": []},
    ]}
    failure = {"failed_steps": [], "failed_criteria": {"AC-1": "failed"}}
    assert pipeline._affected_step_ids(workflow, failure) == {"S-2", "S-3"}


def test_transactional_repair_reexecutes_only_affected_subgraph(tmp_path):
    intent = contracts.freeze_intent(_valid_intent())
    first = _workflow(intent)
    template = first["steps"][0]
    first["steps"] = [
        {**template, "id": "S-1", "expected_outputs": ["one.txt"],
         "acceptance_criteria": [], "dependencies": []},
        {**template, "id": "S-2", "expected_outputs": ["two.txt"],
         "acceptance_criteria": ["AC-1"], "dependencies": ["S-1"]},
        {**template, "id": "S-3", "expected_outputs": ["three.txt"],
         "acceptance_criteria": [], "dependencies": ["S-2"]},
    ]
    repaired = {**first, "revision": 2, "supersedes": 1,
                "revision_history": [{"revision": 2, "reason": "repair",
                                      "material_changes": ["fixed S-2"]}]}
    target = tmp_path / "workspace"
    target.mkdir()
    selected = []

    def execute(_intent, current, workspace, execute_step_ids=None, **_kwargs):
        selected.append(execute_step_ids)
        ids = execute_step_ids or {"S-1", "S-2", "S-3"}
        for step_id, filename in (("S-1", "one.txt"), ("S-2", "two.txt"),
                                  ("S-3", "three.txt")):
            if step_id in ids:
                Path(workspace, filename).write_text(
                    f"{step_id}-attempt-{len(selected)}"
                )
        result = {**current, "steps": [dict(step) for step in current["steps"]]}
        for step in result["steps"]:
            step["status"] = "failed" if len(selected) == 1 and step["id"] == "S-2" else "completed"
        return result

    checks = [
        {"results": {"AC-1": "failed"}},
        {"results": {"AC-1": "passed"}},
    ]
    with patch("runtime.pipeline.execute_workflow", side_effect=execute), patch(
        "runtime.pipeline.repair_workflow_bounded", return_value=repaired
    ):
        pipeline.execute_with_mechanical_repair(
            intent, first, str(target), mechanical_check=lambda *_: checks.pop(0)
        )

    assert selected == [None, {"S-2", "S-3"}]
    assert (target / "one.txt").read_text() == "S-1-attempt-1"
    assert (target / "two.txt").read_text() == "S-2-attempt-2"
    assert (target / "three.txt").read_text() == "S-3-attempt-2"


def test_clear_step_outputs_rejects_parent_escape(tmp_path):
    workflow = {"steps": [{
        "id": "S-1", "expected_outputs": ["../outside.txt"],
    }]}
    with pytest.raises(pipeline.PipelineStop) as exc:
        pipeline._clear_step_outputs(tmp_path, workflow, {"S-1"})
    assert exc.value.reason == "unsafe_expected_output"


def test_execute_workflow_follows_dependency_order_not_list_order():
    """
    Steps are declared in the workflow's `steps` list OUT of dependency
    order (S-2 listed first but depends on S-1). Each step appends its id
    to a shared sequence file. If execution followed raw list order,
    S-2's marker would be written before S-1's — this test fails loudly
    if that regression reappears.
    """
    intent = contracts.freeze_intent(_valid_intent())
    digest = contracts.compute_intent_digest(intent)

    import tempfile
    with tempfile.TemporaryDirectory() as ws:
        seq_file = f"{ws}/sequence.txt"

        workflow = {
            "version": 1, "revision": 1, "status": "approved", "intent_digest": digest,
            "steps": [
                {
                    "id": "S-2", "action": "second", "rationale": "x", "inputs": [],
                    "expected_outputs": ["out"], "dependencies": ["S-1"], "tools": [],
                    "acceptance_criteria": ["AC-1"], "completion_check": "c",
                    "failure_handling": "r", "status": "pending", "kind": "action",
                    "command": ["python3", "-c", f"open({seq_file!r},'a').write('S-2\\n')"],
                },
                {
                    "id": "S-1", "action": "first", "rationale": "x", "inputs": [],
                    "expected_outputs": ["out"], "dependencies": [], "tools": [],
                    "acceptance_criteria": ["AC-1"], "completion_check": "c",
                    "failure_handling": "r", "status": "pending", "kind": "action",
                    "command": ["python3", "-c", f"open({seq_file!r},'a').write('S-1\\n')"],
                },
            ],
            "revision_history": [],
        }

        pipeline.execute_workflow(intent, workflow, ws, use_docker=False, allow_unsafe_fallback=True)

        with open(seq_file) as f:
            written_order = [line.strip() for line in f if line.strip()]
        assert written_order == ["S-1", "S-2"]
    intent = contracts.freeze_intent(_valid_intent())
    digest = contracts.compute_intent_digest(intent)
    workflow = {
        "version": 1, "revision": 1, "status": "approved", "intent_digest": digest,
        "steps": [
            {
                "id": "S-1", "action": "human review before shipping", "rationale": "x",
                "inputs": [], "expected_outputs": ["approval"], "dependencies": [],
                "tools": [], "acceptance_criteria": ["AC-1"], "completion_check": "c",
                "failure_handling": "r", "status": "pending", "command": None,
                "kind": "human_gate",
            },
        ],
        "revision_history": [],
    }

    import tempfile
    with tempfile.TemporaryDirectory() as ws:
        executed = pipeline.execute_workflow(intent, workflow, ws, use_docker=False)
    assert executed["steps"][0]["status"] == "blocked"

    approved = pipeline.approve_human_gate(executed, "S-1", approver_id="dana")
    assert approved["steps"][0]["status"] == "completed"
    assert approved["steps"][0]["_approved_by"] == "dana"
