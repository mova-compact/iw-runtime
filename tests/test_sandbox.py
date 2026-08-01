import sys
from unittest.mock import patch

import pytest

from runtime import sandbox
from runtime.permission_broker import SandboxPolicy


# --- build_egress_ruleset (finding #1: host allowlist wasn't enforced) ----

def test_egress_ruleset_empty_when_network_denied():
    policy = SandboxPolicy(deny_all_network=True, allowed_network_hosts=["example.com"])
    rules = sandbox.build_egress_ruleset(policy, chain="TEST_CHAIN")
    assert rules == []


def test_egress_ruleset_allows_only_resolved_ips_then_drops():
    policy = SandboxPolicy(deny_all_network=False, allowed_network_hosts=["api.example.com"])
    fake_resolver = lambda host: ["203.0.113.10", "203.0.113.11"]

    rules = sandbox.build_egress_ruleset(policy, chain="TEST_CHAIN", resolver=fake_resolver)

    accept_targets = [r[4] for r in rules if r[-1] == "ACCEPT"]
    assert "203.0.113.10" in accept_targets
    assert "203.0.113.11" in accept_targets
    # Must end in an unconditional DROP — default-deny, not default-allow.
    assert rules[-1] == ["iptables", "-A", "TEST_CHAIN", "-j", "DROP"]


def test_egress_ruleset_fails_closed_when_host_does_not_resolve():
    policy = SandboxPolicy(deny_all_network=False, allowed_network_hosts=["nonexistent.invalid"])
    fake_resolver = lambda host: []  # simulates a resolution failure

    rules = sandbox.build_egress_ruleset(policy, chain="TEST_CHAIN", resolver=fake_resolver)

    # No ACCEPT rules were added for a host that failed to resolve —
    # only the trailing DROP remains, so nothing gets through.
    assert all(r[-1] != "ACCEPT" for r in rules)
    assert rules == [["iptables", "-A", "TEST_CHAIN", "-j", "DROP"]]


def test_partial_network_policy_fails_closed_without_host_enforcement():
    policy = SandboxPolicy(deny_all_network=False, allowed_network_hosts=["example.com"])
    with patch("runtime.sandbox.os.name", "nt"):
        with pytest.raises(sandbox.NetworkEnforcementError):
            sandbox._NetworkContext(policy).__enter__()


def test_bounded_process_stops_excessive_combined_output():
    result = sandbox._run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 50000)"],
        timeout=10, max_output_bytes=1024,
    )
    assert result["exit_code"] == 137
    assert result["output_truncated"] is True
    assert len(result["stdout"].encode()) <= 1024
    assert "output limit exceeded" in result["stderr"]


def test_bounded_process_stops_on_timeout():
    result = sandbox._run_bounded(
        [sys.executable, "-c", "import time; time.sleep(10)"], timeout=1,
    )
    assert result["exit_code"] == 124
    assert result["timed_out"] is True
    assert "timed out" in result["stderr"]


# --- run_step_local_unsafe requiring explicit allow_unsafe (finding #6) ---

def test_run_step_local_unsafe_refuses_without_explicit_flag():
    policy = SandboxPolicy()
    with pytest.raises(sandbox.NetworkEnforcementError):
        sandbox.run_step_local_unsafe(["echo", "hi"], policy, "/tmp/iw-test-ws")


def test_run_step_local_unsafe_runs_with_explicit_flag(tmp_path):
    policy = SandboxPolicy()
    result = sandbox.run_step_local_unsafe(
        ["python3", "-c", "print('hi')"], policy, str(tmp_path), allow_unsafe=True
    )
    assert result["exit_code"] == 0
    assert "hi" in result["stdout"]


# --- workspace snapshot/diff (finding #3: exit_code alone isn't proof) ----

def test_workspace_diff_detects_added_modified_removed(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    before = sandbox.snapshot_workspace(str(tmp_path))

    (tmp_path / "a.txt").write_text("changed")
    (tmp_path / "b.txt").write_text("new file")
    after = sandbox.snapshot_workspace(str(tmp_path))

    diff = sandbox.diff_workspace(before, after)
    assert "b.txt" in diff["added"]
    assert "a.txt" in diff["modified"]
    assert diff["removed"] == {}


def test_workspace_diff_empty_when_nothing_changes(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    before = sandbox.snapshot_workspace(str(tmp_path))
    after = sandbox.snapshot_workspace(str(tmp_path))
    diff = sandbox.diff_workspace(before, after)
    assert diff == {"added": {}, "removed": {}, "modified": {}}
