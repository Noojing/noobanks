"""Tests for noobanks.extraction.llm — provider-agnostic LLM client."""

import pytest

from noobanks.extraction.llm import ClaudeLlmClient, LlmClient

SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "error": {"type": "string"},
        "source_page": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["confidence"],
    "additionalProperties": False,
}


class TestLlmClientInterface:
    def test_claude_client_is_an_llm_client(self):
        client = ClaudeLlmClient()
        assert isinstance(client, LlmClient)

    def test_default_model_is_claude_opus_5(self):
        client = ClaudeLlmClient()
        assert client.model == "claude-opus-5"


class TestClaudeLlmClient:
    @pytest.mark.asyncio
    async def test_complete_parses_json_from_response(self, mocker):
        client = ClaudeLlmClient()

        mock_text_block = mocker.MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = '{"value": 32.5, "unit": "%", "confidence": "high"}'

        mock_response = mocker.MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [mock_text_block]

        mock_client = mocker.AsyncMock()
        mock_client.messages.create.return_value = mock_response
        client._client = mock_client

        result = await client.complete(
            system="You extract metrics.",
            user="Pages text...",
            json_schema=SCHEMA,
        )
        assert result == {"value": 32.5, "unit": "%", "confidence": "high"}

        # Verify structured outputs config was passed
        call_kwargs = mock_client.messages.create.call_args.kwargs
        assert call_kwargs["model"] == "claude-opus-5"
        assert call_kwargs["output_config"]["format"]["type"] == "json_schema"

    @pytest.mark.asyncio
    async def test_complete_raises_on_refusal(self, mocker):
        client = ClaudeLlmClient()

        mock_response = mocker.MagicMock()
        mock_response.stop_reason = "refusal"
        mock_response.content = []

        mock_client = mocker.AsyncMock()
        mock_client.messages.create.return_value = mock_response
        client._client = mock_client

        with pytest.raises(ValueError, match="refus"):
            await client.complete("s", "u", SCHEMA)

    @pytest.mark.asyncio
    async def test_complete_raises_on_invalid_json(self, mocker):
        client = ClaudeLlmClient()

        mock_text_block = mocker.MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = "not json at all"

        mock_response = mocker.MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [mock_text_block]

        mock_client = mocker.AsyncMock()
        mock_client.messages.create.return_value = mock_response
        client._client = mock_client

        with pytest.raises(ValueError, match="JSON"):
            await client.complete("s", "u", SCHEMA)

    @pytest.mark.asyncio
    async def test_complete_raises_on_missing_required_fields(self, mocker):
        """Truncated-but-parseable JSON must be rejected."""
        client = ClaudeLlmClient()

        mock_text_block = mocker.MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = '{"value": 32.5}'  # no confidence

        mock_response = mocker.MagicMock()
        mock_response.stop_reason = "max_tokens"
        mock_response.content = [mock_text_block]

        mock_client = mocker.AsyncMock()
        mock_client.messages.create.return_value = mock_response
        client._client = mock_client

        with pytest.raises(ValueError, match="missing required"):
            await client.complete("s", "u", SCHEMA)


from noobanks.config.models import LlmConfig
from noobanks.extraction.llm import (
    DeepSeekLlmClient,
    create_llm_client,
    _validate_json_result,
)


class TestDeepSeekLlmClient:
    @pytest.mark.asyncio
    async def test_complete_parses_json(self, mocker):
        client = DeepSeekLlmClient()
        mocker.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})

        mock_response = mocker.MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"value": 11.3, "unit": "%", "confidence": "high"}'
                    },
                    "finish_reason": "stop",
                }
            ]
        }

        mock_http = mocker.AsyncMock()
        mock_http.__aenter__.return_value.post.return_value = mock_response
        mock_http.__aexit__ = mocker.AsyncMock(return_value=None)

        mocker.patch("httpx.AsyncClient", return_value=mock_http)

        result = await client.complete("s", "u", SCHEMA)
        assert result == {"value": 11.3, "unit": "%", "confidence": "high"}

        # Verify request shape: json mode + bearer auth, and the schema
        # merged into the system message (DeepSeek has no json_schema field).
        call = mock_http.__aenter__.return_value.post.call_args
        assert call.kwargs["json"]["response_format"] == {"type": "json_object"}
        assert call.kwargs["headers"]["Authorization"] == "Bearer test-key"
        system_content = call.kwargs["json"]["messages"][0]["content"]
        assert "Respond exactly with a JSON schema as below" in system_content
        assert '"value"' in system_content

    @pytest.mark.asyncio
    async def test_missing_api_key_raises(self, mocker):
        client = DeepSeekLlmClient()
        mocker.patch.dict("os.environ", {}, clear=True)
        with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
            await client.complete("s", "u", SCHEMA)

    @pytest.mark.asyncio
    async def test_api_error_surfaces_status(self, mocker):
        client = DeepSeekLlmClient()
        mocker.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})

        import httpx

        exc_response = mocker.MagicMock()
        exc_response.status_code = 401
        exc_response.text = '{"error": "bad key"}'

        mock_response = mocker.MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=mocker.MagicMock(), response=exc_response
        )

        mock_http = mocker.AsyncMock()
        mock_http.__aenter__.return_value.post.return_value = mock_response
        mock_http.__aexit__ = mocker.AsyncMock(return_value=None)

        mocker.patch("httpx.AsyncClient", return_value=mock_http)

        with pytest.raises(ValueError, match="401"):
            await client.complete("s", "u", SCHEMA)


class TestCreateLlmClient:
    def test_claude_config_builds_claude_client(self):
        cfg = LlmConfig(provider="claude", model="claude-opus-5")
        client = create_llm_client(cfg)
        assert isinstance(client, ClaudeLlmClient)
        assert client.model == "claude-opus-5"

    def test_deepseek_config_builds_deepseek_client(self):
        cfg = LlmConfig(
            provider="deepseek",
            model="deepseek-chat",
            base_url="https://api.deepseek.com",
            api_key_env="DEEPSEEK_API_KEY",
        )
        client = create_llm_client(cfg)
        assert isinstance(client, DeepSeekLlmClient)
        assert client.model == "deepseek-chat"

    def test_unknown_provider_raises(self):
        cfg = LlmConfig(provider="mystery")
        with pytest.raises(ValueError, match="Unknown"):
            create_llm_client(cfg)


class TestValidateJsonResult:
    def test_required_fields_checked(self):
        with pytest.raises(ValueError, match="missing required"):
            _validate_json_result('{"value": 32.5, "unit": "%"}', SCHEMA)

    def test_value_with_unit_passes(self):
        result = _validate_json_result(
            '{"value": 32.5, "unit": "%", "confidence": "high"}', SCHEMA
        )
        assert result["value"] == 32.5

    def test_error_only_passes(self):
        result = _validate_json_result(
            '{"error": "no relevant content", "confidence": "low"}', SCHEMA
        )
        assert result == {"error": "no relevant content", "confidence": "low"}

    def test_both_value_and_error_raises(self):
        with pytest.raises(ValueError, match="exactly one of value/error"):
            _validate_json_result(
                '{"value": 11.3, "unit": "%", "error": "boom", "confidence": "low"}',
                SCHEMA,
            )

    def test_neither_value_nor_error_raises(self):
        with pytest.raises(ValueError, match="exactly one of value/error"):
            _validate_json_result('{"confidence": "low"}', SCHEMA)

    def test_value_without_unit_raises(self):
        with pytest.raises(ValueError, match="pair value with unit"):
            _validate_json_result(
                '{"value": 11.3, "confidence": "high"}', SCHEMA
            )

    def test_unit_without_value_raises(self):
        # unit alone means neither branch is present — the XOR check fires first
        with pytest.raises(ValueError, match="exactly one of value/error"):
            _validate_json_result(
                '{"unit": "%", "confidence": "high"}', SCHEMA
            )

    def test_null_value_raises(self):
        with pytest.raises(ValueError, match="must not use null"):
            _validate_json_result(
                '{"value": null, "unit": "%", "confidence": "low"}', SCHEMA
            )


from noobanks.extraction.llm import _merge_schema_into_system

ONE_OF_SCHEMA = {
    "type": "object",
    "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "error": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["confidence"],
    "oneOf": [
        {"required": ["value", "unit"], "not": {"required": ["error"]}},
        {"required": ["error"], "not": {"required": ["value", "unit"]}},
    ],
    "additionalProperties": False,
}


class TestSchemaInstruction:
    def test_embeds_serialized_schema(self):
        text = _merge_schema_into_system("Base", ONE_OF_SCHEMA)
        assert '"value"' in text
        assert '"unit"' in text
        assert '"error"' in text
        assert '"oneOf"' in text

    def test_merges_after_system_text(self):
        merged = _merge_schema_into_system("Base instructions", ONE_OF_SCHEMA)
        assert merged.startswith("Base instructions\n\n")
        assert "Respond exactly with a JSON schema as below" in merged


from noobanks.extraction.llm import LlmUsage


class TestUsageTracking:
    @pytest.mark.asyncio
    async def test_claude_accumulates_usage(self, mocker):
        client = ClaudeLlmClient()

        mock_text_block = mocker.MagicMock()
        mock_text_block.type = "text"
        mock_text_block.text = '{"value": 32.5, "unit": "%", "confidence": "high"}'

        usage = mocker.MagicMock()
        usage.input_tokens = 1234
        usage.output_tokens = 56

        mock_response = mocker.MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.content = [mock_text_block]
        mock_response.usage = usage

        mock_client = mocker.AsyncMock()
        mock_client.messages.create.return_value = mock_response
        client._client = mock_client

        await client.complete("s", "u", SCHEMA)
        assert client.total_usage == LlmUsage(input_tokens=1234, output_tokens=56)

    @pytest.mark.asyncio
    async def test_deepseek_accumulates_usage(self, mocker):
        client = DeepSeekLlmClient()
        mocker.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})

        mock_response = mocker.MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"value": 11.3, "unit": "%", "confidence": "high"}'
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 999, "completion_tokens": 42},
        }

        mock_http = mocker.AsyncMock()
        mock_http.__aenter__.return_value.post.return_value = mock_response
        mock_http.__aexit__ = mocker.AsyncMock(return_value=None)

        mocker.patch("httpx.AsyncClient", return_value=mock_http)

        await client.complete("s", "u", SCHEMA)
        assert client.total_usage == LlmUsage(input_tokens=999, output_tokens=42)

    @pytest.mark.asyncio
    async def test_deepseek_missing_usage_is_zero(self, mocker):
        client = DeepSeekLlmClient()
        mocker.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"})

        mock_response = mocker.MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": '{"value": 11.3, "unit": "%", "confidence": "high"}'
                    },
                    "finish_reason": "stop",
                }
            ]
        }

        mock_http = mocker.AsyncMock()
        mock_http.__aenter__.return_value.post.return_value = mock_response
        mock_http.__aexit__ = mocker.AsyncMock(return_value=None)

        mocker.patch("httpx.AsyncClient", return_value=mock_http)

        await client.complete("s", "u", SCHEMA)
        assert client.total_usage == LlmUsage()
