"""Tests for noobanks.extraction.aggregator — ResultAggregator."""

import json

import pytest

from noobanks.config.models import MetricSpec
from noobanks.extraction.aggregator import ResultAggregator
from noobanks.extraction.extractor import MetricExtractor
from noobanks.processing.parser import PageText

SPECS = [
    MetricSpec(
        name="net_profit_margin",
        label="Net Profit Margin",
        keywords=["margin"],
        description="d",
    ),
    MetricSpec(
        name="equity_multiplier",
        label="Equity Multiplier",
        keywords=["equity"],
        description="d",
    ),
]

PAGES = [
    PageText(page_no=1, text="margin of 10%"),
    PageText(page_no=2, text="equity multiplier of 12x"),
]

VALUE_RESULT = {"value": 1.0, "unit": "%", "source_page": 1, "confidence": "high"}
ERROR_RESULT = {"error": "no relevant content", "confidence": "low"}


class FakeLlm:
    def __init__(self, result: dict):
        self._result = result

    async def complete(self, system, user, json_schema):
        return self._result


class TestResultAggregator:
    @pytest.mark.asyncio
    async def test_extract_all_keys_by_metric_name(self):
        llm = FakeLlm(VALUE_RESULT)
        agg = ResultAggregator(extractor=MetricExtractor(llm=llm))
        result = await agg.extract_all(SPECS, PAGES, year=2025)
        assert set(result.keys()) == {"net_profit_margin", "equity_multiplier"}

    @pytest.mark.asyncio
    async def test_failed_metric_records_error(self):
        async def fail(system, user, json_schema):
            raise ValueError("Model refused")

        llm = FakeLlm({})
        llm.complete = fail
        agg = ResultAggregator(extractor=MetricExtractor(llm=llm))
        result = await agg.extract_all(SPECS, PAGES, year=2025)
        assert result["net_profit_margin"]["error"]
        assert result["equity_multiplier"]["error"]

    def test_save_writes_json(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        path = tmp_path / "metrics.json"
        agg.save({"net_profit_margin": VALUE_RESULT}, path)
        loaded = json.loads(path.read_text())
        assert loaded["net_profit_margin"]["value"] == 1.0


class TestJsonlOutput:
    def test_records_for_bank_single_record_per_bank(self):
        """One record per bank, metrics nested under a `metrics` dict."""
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        results = {
            "net_profit_margin": VALUE_RESULT,
            "roe": {"value": 11.3, "unit": "%", "source_page": 3, "confidence": "high"},
        }
        records = agg.records_for_bank("BARC_L", "Barclays PLC", 2025, results)
        assert len(records) == 1
        record = records[0]
        assert record["bank"] == "BARC_L"
        assert record["bank_name"] == "Barclays PLC"
        assert record["year"] == 2025
        assert set(record["metrics"].keys()) == {"net_profit_margin", "roe"}
        assert record["metrics"]["roe"]["value"] == 11.3

    def test_append_jsonl_replaces_existing_bank_year(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        path = tmp_path / "metrics-2025.jsonl"

        # First write: BARC_L + JPM (one record each)
        agg.append_jsonl(
            [{"bank": "BARC_L", "year": 2025, "metrics": {"roe": {"value": 11.3}}},
             {"bank": "JPM", "year": 2025, "metrics": {"roe": {"value": 15.0}}}],
            path,
        )

        # Re-extract BARC_L with updated value — must replace, not duplicate
        agg.append_jsonl(
            [{"bank": "BARC_L", "year": 2025, "metrics": {"roe": {"value": 11.4}}}],
            path,
            replace_bank="BARC_L",
            replace_year=2025,
        )

        lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2
        barc = [l for l in lines if l["bank"] == "BARC_L"]
        jpm = [l for l in lines if l["bank"] == "JPM"]
        assert len(barc) == 1 and barc[0]["metrics"]["roe"]["value"] == 11.4
        assert len(jpm) == 1 and jpm[0]["metrics"]["roe"]["value"] == 15.0

    def test_append_jsonl_creates_file_if_missing(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        path = tmp_path / "nested" / "metrics-2025.jsonl"
        agg.append_jsonl([{"bank": "JPM", "year": 2025, "metrics": {}}], path)
        assert path.exists()
        assert len(path.read_text().splitlines()) == 1


class TestExtractAllResultShape:
    @pytest.mark.asyncio
    async def test_success_result_stored_unchanged(self):
        llm = FakeLlm(VALUE_RESULT)
        agg = ResultAggregator(extractor=MetricExtractor(llm=llm))
        result = await agg.extract_all(SPECS, PAGES, year=2025)
        for stored in result.values():
            assert stored == VALUE_RESULT

    @pytest.mark.asyncio
    async def test_failure_records_error_only(self):
        """Failed metrics carry ONLY an error field — no value."""
        async def fail(system, user, json_schema):
            raise ValueError("Model refused")

        llm = FakeLlm({})
        llm.complete = fail
        agg = ResultAggregator(extractor=MetricExtractor(llm=llm))
        result = await agg.extract_all(SPECS, PAGES, year=2025)
        for stored in result.values():
            assert set(stored.keys()) == {"error"}
            assert "value" not in stored

    @pytest.mark.asyncio
    async def test_in_band_llm_error_preserved(self):
        """An error returned by the LLM (not raised) is stored as-is."""
        llm = FakeLlm(ERROR_RESULT)
        agg = ResultAggregator(extractor=MetricExtractor(llm=llm))
        result = await agg.extract_all(SPECS, PAGES, year=2025)
        for stored in result.values():
            assert stored == ERROR_RESULT


class TestHasBankRecord:
    def test_true_when_record_exists(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        path = tmp_path / "metrics-2025.jsonl"
        agg.append_jsonl([{"bank": "JPM", "year": 2025, "metrics": {}}], path)
        assert agg.has_bank_record(path, "JPM", 2025) is True

    def test_false_when_absent_or_other_year(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        path = tmp_path / "metrics-2025.jsonl"
        agg.append_jsonl([{"bank": "JPM", "year": 2025, "metrics": {}}], path)
        assert agg.has_bank_record(path, "BARC_L", 2025) is False
        assert agg.has_bank_record(path, "JPM", 2024) is False

    def test_false_when_file_missing(self, tmp_path):
        agg = ResultAggregator(extractor=MetricExtractor(llm=FakeLlm({})))
        assert agg.has_bank_record(tmp_path / "nope.jsonl", "JPM", 2025) is False
