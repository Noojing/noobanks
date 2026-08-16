"""Tests for noobanks.extraction.extractor — MetricExtractor."""

import pytest

from noobanks.config.models import MetricSpec
from noobanks.extraction.extractor import MetricExtractor
from noobanks.processing.parser import PageText

SPEC = MetricSpec(
    name="net_profit_margin",
    label="Net Profit Margin",
    keywords=["attributable profit"],
    description="Attributable profit divided by total income, in percent.",
)


def _pages() -> list[PageText]:
    return [
        PageText(page_no=1, text="Welcome and overview."),
        PageText(page_no=2, text="Attributable profit was £6,175m."),
        PageText(page_no=3, text="Board of directors."),
    ]


class FakeLlm:
    """Minimal LlmClient double."""

    def __init__(self, result: dict):
        self._result = result
        self.last_system = ""
        self.last_user = ""
        self.last_schema = {}

    async def complete(self, system, user, json_schema):
        self.last_system = system
        self.last_user = user
        self.last_schema = json_schema
        return self._result


VALUE_RESULT = {"value": 21.2, "unit": "%", "source_page": 2, "confidence": "high"}
ERROR_RESULT = {"error": "no relevant content", "confidence": "low"}


class TestMetricExtractor:
    @pytest.mark.asyncio
    async def test_extract_returns_metric_dict(self):
        llm = FakeLlm(VALUE_RESULT)
        extractor = MetricExtractor(llm=llm)
        result = await extractor.extract(SPEC, _pages(), year=2025)
        assert result["value"] == 21.2
        assert result["unit"] == "%"
        assert result["source_page"] == 2

    @pytest.mark.asyncio
    async def test_error_result_flows_through(self):
        """An in-band LLM error is returned unchanged (no exception)."""
        llm = FakeLlm(ERROR_RESULT)
        extractor = MetricExtractor(llm=llm)
        result = await extractor.extract(SPEC, _pages(), year=2025)
        assert result["error"] == "no relevant content"
        assert "value" not in result

    @pytest.mark.asyncio
    async def test_prompt_contains_top_pages_only(self):
        llm = FakeLlm(VALUE_RESULT)
        extractor = MetricExtractor(llm=llm)
        await extractor.extract(SPEC, _pages(), year=2025, k=1)
        assert "Attributable profit" in llm.last_user
        assert "Board of directors" not in llm.last_user  # page 3 excluded

    @pytest.mark.asyncio
    async def test_prompt_allows_computation(self):
        """The system prompt must instruct computation from cited components."""
        llm = FakeLlm(VALUE_RESULT)
        extractor = MetricExtractor(llm=llm)
        await extractor.extract(SPEC, _pages(), year=2025)
        assert "compute" in llm.last_system.lower()
        assert "cite" in llm.last_system.lower()

    @pytest.mark.asyncio
    async def test_prompt_requires_unit_and_omission(self):
        """The prompt must demand value+unit pairing and forbid nulls."""
        llm = FakeLlm(VALUE_RESULT)
        extractor = MetricExtractor(llm=llm)
        await extractor.extract(SPEC, _pages(), year=2025)
        assert "together with its unit" in llm.last_system.lower()
        assert "never use null" in llm.last_system.lower()
        assert "omit the field entirely" in llm.last_system.lower()

    @pytest.mark.asyncio
    async def test_no_matching_pages_raises(self):
        llm = FakeLlm({})
        extractor = MetricExtractor(llm=llm)
        pages = [PageText(page_no=1, text="Nothing about margins here.")]
        with pytest.raises(ValueError, match="No pages"):
            await extractor.extract(SPEC, pages, year=2025)

    @pytest.mark.asyncio
    async def test_schema_is_fixed_shape(self):
        llm = FakeLlm(VALUE_RESULT)
        extractor = MetricExtractor(llm=llm)
        await extractor.extract(SPEC, _pages(), year=2025)
        assert llm.last_schema["type"] == "object"
        assert set(llm.last_schema["required"]) == {"confidence"}
        assert "value" in llm.last_schema["properties"]
        assert "unit" in llm.last_schema["properties"]
        assert "error" in llm.last_schema["properties"]
        # value+unit branch and error branch
        one_of = llm.last_schema["oneOf"]
        assert len(one_of) == 2
        assert set(one_of[0]["required"]) == {"value", "unit"}
        assert set(one_of[1]["required"]) == {"error"}

    def test_cap_truncates_oversized_pages(self):
        """Oversized pages are truncated to bound LLM token usage."""
        extractor = MetricExtractor(llm=FakeLlm({}))
        big = "line " * 5000  # ~25K chars
        capped = extractor._cap(big, 10_000)
        assert len(capped) <= 10_100
        assert "[truncated]" in capped

    def test_cap_leaves_short_text_untouched(self):
        extractor = MetricExtractor(llm=FakeLlm({}))
        assert extractor._cap("short text", 10_000) == "short text"

    def test_total_usage_none_for_fake_llm(self):
        """Fakes without usage tracking report None."""
        extractor = MetricExtractor(llm=FakeLlm(VALUE_RESULT))
        assert extractor.total_usage is None

    def test_total_usage_from_client(self):
        from noobanks.extraction.llm import LlmUsage

        class UsageLlm(FakeLlm):
            def __init__(self):
                super().__init__(VALUE_RESULT)
                self.total_usage = LlmUsage(input_tokens=10, output_tokens=2)

        extractor = MetricExtractor(llm=UsageLlm())
        usage = extractor.total_usage
        assert usage.input_tokens == 10
        assert usage.output_tokens == 2
