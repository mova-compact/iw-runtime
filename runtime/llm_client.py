"""
llm_client.py — provider-neutral wrapper for the two soft
(judgment) steps: resolve_intent and build_workflow.

Everything this module returns is a PROPOSAL. It carries no authority of
its own — it only becomes real once it passes through contracts.py
(freeze_intent / approve_workflow), which this module never bypasses,
replicates, or is trusted in place of.
"""

import json
import os
import random
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum

import jsonschema
from dotenv import load_dotenv
from .secrets import default_resolver, redact_text
from .observability import default_observability

load_dotenv()

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-5.6-terra",
}


class LLMErrorCode(str, Enum):
    CONFIGURATION = "configuration"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"
    SCHEMA_UNSUPPORTED = "schema_unsupported"
    INVALID_RESPONSE = "invalid_response"


class LLMClientError(RuntimeError):
    def __init__(self, code: LLMErrorCode, message: str, retryable: bool,
                 provider: str | None = None):
        super().__init__(redact_text(message))
        self.code = code
        self.retryable = retryable
        self.provider = provider


@dataclass(frozen=True)
class ProviderCapabilities:
    api_style: str
    full_json_schema: bool


def provider_capabilities(provider: str) -> ProviderCapabilities:
    if provider == "openai":
        return ProviderCapabilities("responses", True)
    if provider == "anthropic":
        return ProviderCapabilities("tool_use", True)
    if provider == "openai_compatible":
        mode = os.environ.get("LLM_STRUCTURED_OUTPUT_MODE", "json_schema").lower()
        if mode not in {"json_schema", "json_object"}:
            raise LLMClientError(
                LLMErrorCode.CONFIGURATION,
                "LLM_STRUCTURED_OUTPUT_MODE must be json_schema or json_object",
                False, provider,
            )
        return ProviderCapabilities("chat_completions", mode == "json_schema")
    raise LLMClientError(
        LLMErrorCode.CONFIGURATION, f"unsupported provider: {provider}", False, provider,
    )


def _classify_error(exc: Exception, provider: str) -> LLMClientError:
    if isinstance(exc, LLMClientError):
        return exc
    if isinstance(exc, (TimeoutError,)) or "timeout" in type(exc).__name__.lower():
        return LLMClientError(LLMErrorCode.TIMEOUT, str(exc), True, provider)
    status = getattr(exc, "status_code", None)
    message = str(exc)
    lowered = message.lower()
    if status in (401, 403):
        return LLMClientError(LLMErrorCode.AUTHENTICATION, message, False, provider)
    if status == 429:
        return LLMClientError(LLMErrorCode.RATE_LIMIT, message, True, provider)
    if status == 408 or (isinstance(status, int) and status >= 500):
        return LLMClientError(LLMErrorCode.UNAVAILABLE, message, True, provider)
    if status == 400 and any(word in lowered for word in ("schema", "response_format")):
        return LLMClientError(LLMErrorCode.SCHEMA_UNSUPPORTED, message, False, provider)
    if status is not None and 400 <= status < 500:
        return LLMClientError(LLMErrorCode.INVALID_REQUEST, message, False, provider)
    if isinstance(exc, (json.JSONDecodeError, jsonschema.ValidationError,
                        KeyError, IndexError, AttributeError)):
        return LLMClientError(LLMErrorCode.INVALID_RESPONSE, message, True, provider)
    if isinstance(exc, (ConnectionError, OSError)):
        return LLMClientError(LLMErrorCode.UNAVAILABLE, message, True, provider)
    return LLMClientError(LLMErrorCode.INVALID_REQUEST, message, False, provider)

def _provider() -> str:
    configured = os.environ.get("LLM_PROVIDER", "").strip().lower()
    aliases = {"openrouter": "openai_compatible", "compatible": "openai_compatible"}
    configured = aliases.get(configured, configured)
    if configured:
        if configured not in {"anthropic", "openai", "openai_compatible"}:
            raise RuntimeError(f"Unsupported LLM_PROVIDER: {configured}")
        return configured
    if os.environ.get("LLM_BASE_URL"):
        return "openai_compatible"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise RuntimeError(
        "Configure LLM_PROVIDER plus its API key, or set OPENAI_API_KEY / "
        "ANTHROPIC_API_KEY."
    )


def _model(provider: str) -> str:
    model = os.environ.get("LLM_MODEL") or os.environ.get("RUNTIME_MODEL")
    if model:
        return model
    if provider == "openai_compatible":
        raise RuntimeError("Set LLM_MODEL for an OpenAI-compatible provider.")
    return DEFAULT_MODELS[provider]


def _api_key(provider: str) -> str:
    if provider == "anthropic":
        names, env_names = ("anthropic_api_key", "llm_api_key"), ("ANTHROPIC_API_KEY", "LLM_API_KEY")
    elif provider == "openai":
        names, env_names = ("openai_api_key", "llm_api_key"), ("OPENAI_API_KEY", "LLM_API_KEY")
    else:
        names, env_names = ("llm_api_key",), ("LLM_API_KEY", "OPENAI_API_KEY")
    for name in names:
        value = default_resolver.get(name, env_names, required=False)
        if value:
            return value
    raise RuntimeError(f"No API key configured for LLM_PROVIDER={provider}.")


def _extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def _strict_schema(schema: dict) -> dict:
    """Convert regular JSON Schema to the strict Structured Outputs subset.

    OpenAI-compatible strict mode requires every declared object property
    to be listed in `required` and disallows undeclared properties. Fields
    that may be absent in the runtime contract are already nullable or have
    defaults in our schemas, so requiring their explicit representation does
    not weaken validation.
    """
    result = deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            if not node:
                node["type"] = [
                    "string", "number", "boolean", "null",
                ]
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            node.pop("default", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(result)
    return result


def _call_structured_once(
    provider: str, system_prompt: str, user_content: str, max_tokens: int,
    schema: dict | None = None, schema_name: str = "structured_response",
) -> dict:
    model = _model(provider)
    wire_schema = _strict_schema(schema) if schema else None
    instruction = system_prompt + "\n\nReturn ONLY valid JSON. No prose, no markdown fences."

    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic(api_key=_api_key(provider))
        request = {
            "model": model,
            "max_tokens": max_tokens,
            "system": instruction,
            "messages": [{"role": "user", "content": user_content}],
        }
        if wire_schema:
            request["tools"] = [{
                "name": schema_name,
                "description": "Return the validated structured response.",
                "input_schema": wire_schema,
            }]
            request["tool_choice"] = {"type": "tool", "name": schema_name}
        response = client.messages.create(**request)
        if wire_schema:
            blocks = [block for block in response.content if block.type == "tool_use"]
            if not blocks:
                raise RuntimeError("Anthropic returned no structured tool result.")
            return blocks[0].input
        text = "".join(block.text for block in response.content if block.type == "text")
    else:
        from openai import OpenAI

        base_url = os.environ.get("LLM_BASE_URL")
        client = OpenAI(api_key=_api_key(provider), base_url=base_url or None)
        if provider == "openai":
            request = dict(
                model=model,
                instructions=instruction,
                input=user_content,
                max_output_tokens=max_tokens,
            )
            if wire_schema:
                request["text"] = {"format": {
                    "type": "json_schema", "name": schema_name,
                    "strict": True, "schema": wire_schema,
                }}
            response = client.responses.create(**request)
            text = response.output_text
        else:
            response_format = {"type": "json_object"}
            if wire_schema:
                response_format = {"type": "json_schema", "json_schema": {
                    "name": schema_name, "strict": True, "schema": wire_schema,
                }}
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                response_format=response_format,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": user_content},
                ],
            )
            text = response.choices[0].message.content or ""

    return _extract_json(text)


def call_structured(
    system_prompt: str, user_content: str, max_tokens: int = 4000,
    schema: dict | None = None, schema_name: str = "structured_response",
) -> dict:
    """Call an LLM with bounded retries and provider capability enforcement."""
    try:
        provider = _provider()
        capabilities = provider_capabilities(provider)
        token_limit = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "16000"))
        attempts = int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))
        base_delay = float(os.environ.get("LLM_RETRY_BASE_SECONDS", "0.5"))
    except (RuntimeError, ValueError) as exc:
        raise LLMClientError(
            LLMErrorCode.CONFIGURATION, str(exc), False,
            locals().get("provider"),
        ) from exc
    if max_tokens <= 0 or max_tokens > token_limit:
        raise LLMClientError(
            LLMErrorCode.CONFIGURATION,
            f"max_tokens must be between 1 and configured limit {token_limit}",
            False, provider,
        )
    if attempts < 1 or attempts > 10 or base_delay < 0:
        raise LLMClientError(
            LLMErrorCode.CONFIGURATION,
            "LLM_MAX_ATTEMPTS must be 1..10 and retry delay must be non-negative",
            False, provider,
        )
    if schema is not None and not capabilities.full_json_schema:
        raise LLMClientError(
            LLMErrorCode.SCHEMA_UNSUPPORTED,
            f"provider {provider} is not configured for full JSON Schema outputs",
            False, provider,
        )

    last_error = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        default_observability.event(
            "llm_attempt_started", provider=provider, attempt=attempt,
            schema_name=schema_name,
        )
        try:
            result = _call_structured_once(
                provider, system_prompt, user_content, max_tokens,
                schema=schema, schema_name=schema_name,
            )
            if schema is not None:
                jsonschema.validate(result, schema)
            default_observability.metrics.observe(
                "iw_llm_request_duration_seconds", time.monotonic() - started,
                provider=provider, outcome="success",
            )
            default_observability.event(
                "llm_attempt_succeeded", provider=provider, attempt=attempt,
            )
            return result
        except Exception as exc:
            error = _classify_error(exc, provider)
            last_error = error
            default_observability.metrics.observe(
                "iw_llm_request_duration_seconds", time.monotonic() - started,
                provider=provider, outcome=error.code.value,
            )
            if not error.retryable or attempt == attempts:
                event = "llm_retry_exhausted" if error.retryable else "llm_request_rejected"
                default_observability.event(
                    event, level="error", provider=provider, attempt=attempt,
                    error_code=error.code.value,
                )
                raise error from exc
            delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
            default_observability.event(
                "llm_retry_scheduled", level="warning", provider=provider,
                attempt=attempt, error_code=error.code.value,
                delay_seconds=delay,
            )
            time.sleep(delay * random.uniform(0.8, 1.2))
    raise last_error  # pragma: no cover
