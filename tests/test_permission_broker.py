from runtime.permission_broker import derive_policy


def test_network_denied_by_default():
    intent = {
        "side_effects": {"requested": [], "forbidden": []},
        "authoritative_inputs": [],
    }
    policy = derive_policy(intent, "/tmp/ws")
    assert policy.deny_all_network is True
    assert policy.allowed_network_hosts == []


def test_network_denied_even_if_requested_without_named_hosts():
    # Requesting "network_access" without naming specific hosts must NOT
    # open blanket internet access — this is the direct fix for "any
    # path to the goal, including the open internet".
    intent = {
        "side_effects": {"requested": ["network_access"], "forbidden": []},
        "authoritative_inputs": [],
    }
    policy = derive_policy(intent, "/tmp/ws")
    assert policy.deny_all_network is True


def test_network_opened_only_for_named_hosts_with_explicit_request():
    intent = {
        "side_effects": {"requested": ["network_access"], "forbidden": []},
        "authoritative_inputs": [
            {"source": "https://api.example.com/v1/data", "role": "reference data"}
        ],
    }
    policy = derive_policy(intent, "/tmp/ws")
    assert policy.deny_all_network is False
    assert "api.example.com" in policy.allowed_network_hosts


def test_forbidden_side_effects_logged_for_audit():
    intent = {
        "side_effects": {"requested": [], "forbidden": ["delete_files", "send_email"]},
        "authoritative_inputs": [],
    }
    policy = derive_policy(intent, "/tmp/ws")
    assert "delete_files" in policy.forbidden_actions_log
    assert "send_email" in policy.forbidden_actions_log


def test_workspace_is_only_write_path():
    intent = {"side_effects": {"requested": [], "forbidden": []}, "authoritative_inputs": []}
    policy = derive_policy(intent, "/tmp/my-workspace")
    assert policy.allowed_write_paths == ["/tmp/my-workspace"]
