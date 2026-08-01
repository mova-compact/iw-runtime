"""
example_run.py — end-to-end demo of the full pipeline against the configured
LLM provider. Configuration is loaded from `.env` by runtime.llm_client.

Usage:
    # Configure LLM_PROVIDER, LLM_API_KEY/ provider key, and LLM_MODEL in .env
    python examples/example_run.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from runtime import pipeline, llm_client
from runtime.audit import AuditLedger


REQUEST = (
    "Write a Python script called hello.py that prints 'Hello, world!' "
    "and nothing else. No network access needed, no other files should "
    "be modified."
)
OWNER_ID = "demo-user"


def main():
    # ephemeral_ok=True is correct HERE because this demo writes and
    # verifies in the same process. In a real deployment, generate
    # AUDIT_SIGNING_KEY outside the executor (CI secret / verifier
    # service) — see runtime/audit.py's docstring for why.
    ledger = AuditLedger("./audit.jsonl", ephemeral_ok=True)

    print("=== 1. Resolving intent (bounded loop: resolve -> validate -> "
          "clarify if needed, max 5 rounds; owner_id bound at this step) ===")
    intent = pipeline.resolve_intent_bounded(REQUEST, owner_id=OWNER_ID, ledger=ledger)
    print(f"Frozen intent objective: {intent['objective']}")
    print(f"Required acceptance criteria: "
          f"{[ac['id'] for ac in intent['acceptance_criteria'] if ac['required']]}")

    print("\n=== 2. Building + approving workflow (digest / cycle / "
          "coverage / actor-ownership checks happen here, not as separate "
          "graph nodes) ===")
    workflow = pipeline.build_workflow_bounded(intent, actor_id=OWNER_ID, ledger=ledger)
    print(f"Approved workflow with {len(workflow['steps'])} step(s).")

    print("\n=== 3. Executing inside sandbox (policy derived from frozen "
          "intent's side_effects/scope, network closed by default) ===")
    with tempfile.TemporaryDirectory() as workspace:
        executed = pipeline.execute_workflow(intent, workflow, workspace, use_docker=True)

        for step in executed["steps"]:
            print(f"  {step['id']}: {step['status']}")
            if "_result" in step:
                print(f"    stdout: {step['_result']['stdout'].strip()}")
            if "_workspace_diff" in step:
                print(f"    workspace diff: {step['_workspace_diff']}")

        print("\n=== 4. Verifying completion (mechanical criteria checked "
              "independently, never trusted from the model's report) ===")
        prompt = (Path(__file__).parent.parent / "prompts" / "verify_completion.md").read_text()
        import json as _json
        context = {
            "frozen_intent": intent,
            "approved_workflow": executed,
            "execution_result": [s.get("_result") for s in executed["steps"]],
        }
        completion_report = llm_client.call_structured(prompt, _json.dumps(context, ensure_ascii=False))

        def _compute_mechanical_result(step: dict) -> str:
            """
            A real mechanical check, not just exit_code==0 (that alone
            proves the process didn't crash — nothing about what it
            actually did to the filesystem). This checks exit_code AND
            that the step touched only files it declared it would touch
            (`expected_outputs`) — using the actual before/after
            workspace diff captured by execute_workflow, not the model's
            claim about what it changed.
            """
            result = step.get("_result", {})
            if result.get("exit_code") != 0:
                return "failed"
            diff = step.get("_workspace_diff", {"added": {}, "removed": {}, "modified": {}})
            touched = set(diff["added"]) | set(diff["modified"]) | set(diff["removed"])
            unexpected = touched - set(step.get("expected_outputs", []))
            return "failed" if unexpected else "passed"

        # Independently compute the mechanical result ourselves — this is
        # the part that must NOT come from the model's report.
        mechanical_results = {}
        for step in executed["steps"]:
            for ac_id in step["acceptance_criteria"]:
                ac = next(a for a in intent["acceptance_criteria"] if a["id"] == ac_id)
                if ac.get("automation") == "mechanical":
                    mechanical_results[ac_id] = _compute_mechanical_result(step)

        try:
            summary = pipeline.verify(
                intent, executed, completion_report, mechanical_results,
                actor_id=OWNER_ID, ledger=ledger,
            )
            print(f"Completion status: {summary['status']}")
            print(f"Pending human review: {summary['pending_human_review']}")
        except Exception as e:
            print(f"Completion gate FAILED: {e}")

    print("\n=== 5. Verifying the audit ledger's hash chain wasn't tampered with ===")
    check = ledger.verify_chain()
    print(f"Audit chain: {check}")


if __name__ == "__main__":
    main()
