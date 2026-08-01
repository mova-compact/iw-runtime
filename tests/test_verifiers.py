from unittest.mock import patch

from runtime import verifiers


def _intent():
    return {
        "scope": {"included": [], "excluded": []},
        "constraints": [], "authoritative_inputs": [],
        "side_effects": {"requested": [], "forbidden": []},
        "acceptance_criteria": [{
            "id": "AC-1", "required": True, "automation": "mechanical",
        }],
    }


def _workflow(status="completed", unexpected=False):
    diff = {"added": {"result.json": "hash"}, "modified": {}, "removed": {}}
    if unexpected:
        diff["added"]["extra.txt"] = "hash2"
    return {"steps": [{
        "id": "S-1", "status": status,
        "expected_outputs": ["result.json"], "_workspace_diff": diff,
    }]}


def test_missing_verifier_spec_fails_closed(tmp_path):
    result = verifiers.run_mechanical_verifiers(
        _intent(), _workflow(), str(tmp_path), {},
    )
    assert result["results"] == {"AC-1": "unknown"}
    assert result["evidence"]["AC-1"]["reason"] == "missing_verifier_spec"


def test_json_equals_returns_independent_evidence(tmp_path):
    (tmp_path / "result.json").write_text('{"value": 42}')
    result = verifiers.run_mechanical_verifiers(
        _intent(), _workflow(), str(tmp_path),
        {"AC-1": {"type": "json_equals", "config": {
            "path": "result.json", "expected": {"value": 42},
        }}},
    )
    assert result["results"] == {"AC-1": "passed"}
    assert result["evidence"]["AC-1"]["actual"] == {"value": 42}


def test_unexpected_output_overrides_passing_verifier(tmp_path):
    (tmp_path / "result.json").write_text("ok")
    (tmp_path / "extra.txt").write_text("unexpected")
    result = verifiers.run_mechanical_verifiers(
        _intent(), _workflow(unexpected=True), str(tmp_path),
        {"AC-1": {"type": "exact_files", "config": {
            "paths": ["extra.txt", "result.json"],
        }}},
    )
    assert result["results"] == {"AC-1": "failed"}
    assert result["structural"]["unexpected_outputs"] == {"S-1": ["extra.txt"]}


def test_nonterminal_step_overrides_passing_verifier(tmp_path):
    (tmp_path / "result.json").write_text("ok")
    result = verifiers.run_mechanical_verifiers(
        _intent(), _workflow(status="failed"), str(tmp_path),
        {"AC-1": {"type": "exact_files", "config": {"paths": ["result.json"]}}},
    )
    assert result["results"] == {"AC-1": "failed"}


def test_python_unittest_uses_read_only_docker_verifier(tmp_path):
    with patch("runtime.sandbox.run_verifier", return_value={
        "exit_code": 0, "stdout": "OK", "stderr": "",
    }) as runner:
        result = verifiers.run_mechanical_verifiers(
            _intent(), _workflow(), str(tmp_path),
            {"AC-1": {"type": "python_unittest", "config": {
                "path": "test_result.py", "timeout": 10,
            }}},
        )
    assert result["results"] == {"AC-1": "passed"}
    assert runner.call_args.args[0] == [
        "python", "-m", "unittest", "-q", "test_result.py"
    ]
