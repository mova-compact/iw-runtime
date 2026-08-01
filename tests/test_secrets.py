import json

from runtime.secrets import REDACTED, SecretResolver, redact, register_secret


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[(service, name)] = value


def test_keyring_round_trip_registers_value_for_redaction(monkeypatch):
    monkeypatch.setenv("IW_SECRET_SOURCE", "keyring")
    backend = MemoryKeyring()
    resolver = SecretResolver(keyring_backend=backend)
    resolver.set("llm_api_key", "super-secret-value")
    assert resolver.get("llm_api_key") == "super-secret-value"
    assert redact("error contained super-secret-value") == f"error contained {REDACTED}"


def test_ci_auto_mode_prefers_environment(monkeypatch):
    backend = MemoryKeyring()
    backend.set_password("intent-workflow-runtime-v3", "llm_api_key", "keyring-value")
    monkeypatch.setenv("IW_SECRET_SOURCE", "auto")
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("LLM_API_KEY", "ci-environment-value")
    value = SecretResolver(keyring_backend=backend).get("llm_api_key", ("LLM_API_KEY",))
    assert value == "ci-environment-value"


def test_recursive_redaction_handles_sensitive_keys_and_token_patterns():
    register_secret("known-value-123")
    value = {
        "nested": [{"api_key": "anything"}, "known-value-123"],
        "message": "Authorization: Bearer abc.def-123",
    }
    result = redact(value)
    assert result["nested"] == [{"api_key": REDACTED}, REDACTED]
    assert "abc.def-123" not in json.dumps(result)


def test_redaction_does_not_mutate_input():
    original = {"token": "secret", "safe": ["value"]}
    result = redact(original)
    assert original["token"] == "secret"
    assert result["token"] == REDACTED
