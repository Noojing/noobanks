"""LlmClient — provider-agnostic LLM abstraction.

Backends:
- ClaudeLlmClient — Anthropic API via the official SDK.
- DeepSeekLlmClient — OpenAI-compatible /chat/completions endpoint
  (works for DeepSeek or any OpenAI-compatible server).

Use create_llm_client(config) to build whichever backend
config/pipeline.yaml selects.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from noobanks.config.models import LlmConfig

logger = logging.getLogger(__name__)


@dataclass
class LlmUsage:
    """Token consumption reported by the LLM backend for one run."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


def _validate_json_result(
    text: str,
    json_schema: dict[str, Any],
    *,
    context: str = "",
) -> dict[str, Any]:
    """Parse model output as JSON and enforce the schema's required fields.

    Raises:
        ValueError: If the output is empty, not JSON, not an object, or
            missing required schema fields (e.g. truncated generation).
    """
    if not text.strip():
        raise ValueError(f"Model produced no JSON output{context}")

    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Model output was not valid JSON{context}: {text[:200]!r}"
        ) from exc

    if not isinstance(result, dict):
        raise ValueError(f"Model output was not a JSON object{context}: {text[:200]!r}")

    missing = set(json_schema.get("required", [])) - set(result.keys())
    if missing:
        raise ValueError(
            f"Model output missing required fields {sorted(missing)}{context}: {text[:200]!r}"
        )

    # value/error exclusivity: exactly one of the two keys may be present.
    has_value = "value" in result
    has_error = "error" in result
    if has_value == has_error:
        raise ValueError(
            f"Model output must contain exactly one of value/error "
            f"(value present: {has_value}, error present: {has_error}){context}: {text[:200]!r}"
        )

    # value must always be paired with a unit (and vice versa).
    has_unit = "unit" in result
    if has_value != has_unit:
        raise ValueError(
            f"Model output must pair value with unit "
            f"(value present: {has_value}, unit present: {has_unit}){context}: {text[:200]!r}"
        )

    # Fields are never null — unavailable fields must be omitted instead.
    null_keys = [k for k, v in result.items() if v is None]
    if null_keys:
        raise ValueError(
            f"Model output must not use null (offending fields: {sorted(null_keys)})"
            f"{context}: {text[:200]!r}"
        )

    return result


def _merge_schema_into_system(system: str, json_schema: dict[str, Any]) -> str:
    """Append the schema instruction block to the system prompt."""


    schema = f"""Respond exactly with a JSON schema as below:\n{json.dumps(json_schema, indent=2, ensure_ascii=False)}\n"""
    return f"{system}\n\n{schema}"


class LlmClient(ABC):
    """Abstract LLM client for structured metric extraction."""

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one structured extraction request.

        Args:
            system: System prompt (extraction instructions).
            user: User content (the relevant document pages).
            json_schema: JSON schema the response must conform to.

        Returns:
            Parsed dict validated against the schema's required fields.

        Raises:
            ValueError: If the model refuses or produces unusable output.
        """
        ...


class ClaudeLlmClient(LlmClient):
    """Claude backend via the official Anthropic Python SDK.

    Credentials resolve from the environment (ANTHROPIC_API_KEY /
    ANTHROPIC_AUTH_TOKEN) or an `ant auth login` profile — the zero-arg
    constructor handles all of it.
    """

    def __init__(self, model: str = "claude-opus-5", max_tokens: int = 2048):
        from anthropic import AsyncAnthropic

        self.model = model
        self.max_tokens = max_tokens
        self.total_usage = LlmUsage()
        self._client = AsyncAnthropic()

    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            # Structured extraction is a simple task — disable thinking so
            # it cannot consume the max_tokens budget before the JSON starts.
            thinking={"type": "disabled"},
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": json_schema,
                }
            },
            messages=[{"role": "user", "content": user}],
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.total_usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
            self.total_usage.output_tokens += getattr(usage, "output_tokens", 0) or 0

        if response.stop_reason == "refusal":
            raise ValueError(
                f"Model refused the request (category={getattr(response.stop_details, 'category', None)})"
            )

        text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
        return _validate_json_result(
            text, json_schema, context=f" (stop_reason={response.stop_reason})"
        )


class DeepSeekLlmClient(LlmClient):
    """OpenAI-compatible backend (DeepSeek or any compatible server).

    Calls POST {base_url}/chat/completions with:
    - Authorization: Bearer <key from api_key_env>
    - response_format: {"type": "json_object"} to force JSON output

    The API key is read from the environment variable named by
    api_key_env (default DEEPSEEK_API_KEY) — never hardcoded.
    """

    def __init__(
        self,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        api_key_env: str = "DEEPSEEK_API_KEY",
        max_tokens: int = 2048,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.max_tokens = max_tokens
        self._timeout = timeout
        self.total_usage = LlmUsage()

    async def complete(
        self,
        system: str,
        user: str,
        json_schema: dict[str, Any],
    ) -> dict[str, Any]:
        import httpx

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ValueError(
                f"Missing API key: set environment variable {self.api_key_env}"
            )

        payload = {
            "model": self.model,
            "messages": [
                # DeepSeek has no standalone json_schema parameter — merge
                # the output contract into the system instruction.
                {"role": "system", "content": _merge_schema_into_system(system, json_schema)},
                {"role": "user", "content": user},
            ],
            # Force JSON object output (OpenAI-compatible json mode).
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"DeepSeek API error {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(f"DeepSeek API request failed: {exc}") from exc

        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"Unexpected DeepSeek response shape: {str(data)[:300]}") from exc

        usage = data.get("usage") or {}
        self.total_usage.input_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.total_usage.output_tokens += int(usage.get("completion_tokens", 0) or 0)

        finish = data["choices"][0].get("finish_reason", "unknown")
        return _validate_json_result(
            text, json_schema, context=f" (finish_reason={finish})"
        )


def create_llm_client(config: LlmConfig) -> LlmClient:
    """Build the LlmClient selected by pipeline.yaml configuration.

    Args:
        config: LlmConfig from config/pipeline.yaml.

    Returns:
        A ClaudeLlmClient or DeepSeekLlmClient (or compatible backend).

    Raises:
        ValueError: For an unknown provider.
    """
    provider = config.provider.lower()
    if provider == "claude":
        return ClaudeLlmClient(model=config.model, max_tokens=config.max_tokens)
    if provider in ("deepseek", "openai", "openai-compatible"):
        return DeepSeekLlmClient(
            model=config.model,
            base_url=config.base_url or "https://api.deepseek.com",
            api_key_env=config.api_key_env or "DEEPSEEK_API_KEY",
            max_tokens=config.max_tokens,
        )
    raise ValueError(f"Unknown LLM provider: {config.provider}")
