import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from runtime import llm_client


@pytest.fixture(autouse=True)
def clean_llm_environment(monkeypatch):
    monkeypatch.setenv("IW_SECRET_SOURCE", "environment")
    for name in (
        "LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL",
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "RUNTIME_MODEL",
        "LLM_STRUCTURED_OUTPUT_MODE", "LLM_MAX_OUTPUT_TOKENS",
        "LLM_MAX_ATTEMPTS", "LLM_RETRY_BASE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_provider_auto_detects_native_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    assert llm_client._provider() == "openai"


def test_provider_aliases_openrouter_to_compatible(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    assert llm_client._provider() == "openai_compatible"


def test_compatible_provider_requires_model(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    with pytest.raises(RuntimeError, match="LLM_MODEL"):
        llm_client._model("openai_compatible")


def test_openai_compatible_uses_chat_completions(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "vendor/model")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
    )
    client = MagicMock()
    client.chat.completions.create.return_value = response
    with patch("openai.OpenAI", return_value=client) as constructor:
        result = llm_client.call_structured(
            "system", "user", max_tokens=123,
            schema={"type": "object"}, schema_name="test_result",
        )
    assert result == {"ok": True}
    constructor.assert_called_once_with(
        api_key="test-key", base_url="https://gateway.example/v1"
    )
    assert client.chat.completions.create.call_args.kwargs["model"] == "vendor/model"
    assert client.chat.completions.create.call_args.kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "test_result", "strict": True,
            "schema": {"type": "object"},
        },
    }


def test_native_openai_uses_responses_api(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gpt-test")
    client = MagicMock()
    client.responses.create.return_value = SimpleNamespace(output_text='{"ok": true}')
    with patch("openai.OpenAI", return_value=client):
        result = llm_client.call_structured(
            "system", "user", max_tokens=321,
            schema={"type": "object"}, schema_name="test_result",
        )
    assert result == {"ok": True}
    assert client.responses.create.call_args.kwargs["max_output_tokens"] == 321
    assert client.responses.create.call_args.kwargs["text"]["format"]["schema"] == {
        "type": "object"
    }


def test_strict_schema_requires_every_property_recursively():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean", "default": True}},
            },
        },
        "required": ["name"],
    }
    strict = llm_client._strict_schema(schema)
    assert strict["required"] == ["name", "nested"]
    assert strict["additionalProperties"] is False
    assert strict["properties"]["nested"]["required"] == ["enabled"]
    assert "default" not in strict["properties"]["nested"]["properties"]["enabled"]


def test_strict_schema_gives_unconstrained_nodes_explicit_json_types():
    strict = llm_client._strict_schema({"type": "object", "properties": {"value": {}}})
    assert strict["properties"]["value"]["type"] == [
        "string", "number", "boolean", "null"
    ]


def test_full_schema_fails_closed_when_compatible_provider_only_supports_json_object(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_MODEL", "vendor/model")
    monkeypatch.setenv("LLM_STRUCTURED_OUTPUT_MODE", "json_object")
    with pytest.raises(llm_client.LLMClientError) as exc:
        llm_client.call_structured("system", "user", schema={"type": "object"})
    assert exc.value.code == llm_client.LLMErrorCode.SCHEMA_UNSUPPORTED
    assert exc.value.retryable is False


def test_rate_limit_retries_with_bounded_attempts(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "0")

    class RateLimit(Exception):
        status_code = 429

    with patch(
        "runtime.llm_client._call_structured_once",
        side_effect=[RateLimit("slow down"), RateLimit("slow down"), {"ok": True}],
    ) as call:
        assert llm_client.call_structured("system", "user") == {"ok": True}
    assert call.call_count == 3


def test_authentication_error_is_normalized_and_not_retried(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "bad")

    class Authentication(Exception):
        status_code = 401

    with patch(
        "runtime.llm_client._call_structured_once", side_effect=Authentication("denied")
    ) as call, pytest.raises(llm_client.LLMClientError) as exc:
        llm_client.call_structured("system", "user")
    assert exc.value.code == llm_client.LLMErrorCode.AUTHENTICATION
    assert exc.value.retryable is False
    assert call.call_count == 1


def test_invalid_json_response_is_retried(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "0")
    invalid = json.JSONDecodeError("bad", "x", 0)
    with patch(
        "runtime.llm_client._call_structured_once",
        side_effect=[invalid, {"ok": True}],
    ) as call:
        assert llm_client.call_structured("system", "user") == {"ok": True}
    assert call.call_count == 2


def test_output_token_quota_is_enforced_before_provider_call(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("LLM_MAX_OUTPUT_TOKENS", "100")
    with patch("runtime.llm_client._call_structured_once") as call:
        with pytest.raises(llm_client.LLMClientError) as exc:
            llm_client.call_structured("system", "user", max_tokens=101)
    assert exc.value.code == llm_client.LLMErrorCode.CONFIGURATION
    call.assert_not_called()


def test_gateway_claiming_schema_support_is_verified_locally(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_MODEL", "vendor/model")
    monkeypatch.setenv("LLM_RETRY_BASE_SECONDS", "0")
    schema = {
        "type": "object", "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"], "additionalProperties": False,
    }
    with patch(
        "runtime.llm_client._call_structured_once",
        side_effect=[{"wrong": 1}, {"ok": True}],
    ) as call:
        assert llm_client.call_structured("system", "user", schema=schema) == {"ok": True}
    assert call.call_count == 2
