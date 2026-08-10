"""Tests for noobanks.config.models — BankSpec, BankRegistry, SourceConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from noobanks.config.models import BankRegistry, BankSpec, SourceConfig


class TestSourceConfig:
    def test_minimal_source_config(self):
        sc = SourceConfig(investor_relations="https://example.com/ir")
        assert sc.edgar is False
        assert sc.investor_relations == "https://example.com/ir"

    def test_with_edgar(self):
        sc = SourceConfig(edgar=True, investor_relations="https://example.com/ir")
        assert sc.edgar is True

    def test_missing_investor_relations_raises(self):
        with pytest.raises(ValidationError):
            SourceConfig()


class TestBankSpec:
    def test_full_construction(self, sample_bank_spec: BankSpec):
        assert sample_bank_spec.name == "Barclays PLC"
        assert sample_bank_spec.ticker == "BARC.L"
        assert sample_bank_spec.exchange == "LSE"
        assert sample_bank_spec.market == "UK"
        assert sample_bank_spec.cik is None
        assert len(sample_bank_spec.filings) == 3

    def test_ticker_safe_replaces_dots(self, sample_bank_spec: BankSpec):
        assert sample_bank_spec.ticker_safe == "BARC_L"

    def test_ticker_safe_no_dots(self, sample_us_bank_spec: BankSpec):
        assert sample_us_bank_spec.ticker_safe == "JPM"

    def test_ticker_safe_multiple_dots(self):
        bank = BankSpec(
            name="Test",
            ticker="00005.HK",
            exchange="HKEX",
            market="HK",
            sources=SourceConfig(investor_relations="https://example.com"),
        )
        assert bank.ticker_safe == "00005_HK"

    def test_domain_extraction(self, sample_bank_spec: BankSpec):
        assert sample_bank_spec.domain == "home.barclays"

    def test_domain_from_cn_bank(self, sample_cn_bank_spec: BankSpec):
        assert sample_cn_bank_spec.domain == "www.icbc.com.cn"

    def test_default_filings_empty(self):
        bank = BankSpec(
            name="Minimal",
            ticker="MIN",
            exchange="N/A",
            market="XX",
            sources=SourceConfig(investor_relations="https://example.com"),
        )
        assert bank.filings == []

    def test_optional_cik(self, sample_us_bank_spec: BankSpec):
        assert sample_us_bank_spec.cik == "0000019617"

    def test_missing_name_raises(self):
        with pytest.raises(ValidationError):
            BankSpec(
                ticker="T",
                exchange="X",
                market="XX",
                sources=SourceConfig(investor_relations="https://example.com"),
            )


class TestBankRegistry:
    def test_length(self, sample_bank_registry: BankRegistry):
        assert len(sample_bank_registry) == 3

    def test_iteration(self, sample_bank_registry: BankRegistry):
        tickers = [b.ticker for b in sample_bank_registry]
        assert "BARC.L" in tickers
        assert "JPM" in tickers

    def test_find_by_ticker_exact(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_ticker("JPM")
        assert bank is not None
        assert bank.name == "JPMorgan Chase & Co."

    def test_find_by_ticker_case_insensitive(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_ticker("jpm")
        assert bank is not None
        assert bank.ticker == "JPM"

    def test_find_by_safe_ticker(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_ticker("BARC_L")
        assert bank is not None
        assert bank.ticker == "BARC.L"

    def test_find_by_ticker_with_dots(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_ticker("601398.SH")
        assert bank is not None
        assert bank.name == "ICBC"

    def test_find_by_ticker_not_found(self, sample_bank_registry: BankRegistry):
        assert sample_bank_registry.find_by_ticker("NONEXISTENT") is None

    def test_find_by_name_substring(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_name("Barclays")
        assert bank is not None
        assert bank.ticker == "BARC.L"

    def test_find_by_name_case_insensitive(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find_by_name("icbc")
        assert bank is not None
        assert bank.ticker == "601398.SH"

    def test_find_by_name_not_found(self, sample_bank_registry: BankRegistry):
        assert sample_bank_registry.find_by_name("BitcoinBank") is None

    def test_find_ticker_first(self, sample_bank_registry: BankRegistry):
        # JPM matches both ticker and name substring; ticker should win
        bank = sample_bank_registry.find("JPM")
        assert bank is not None
        assert bank.ticker == "JPM"

    def test_find_fallback_to_name(self, sample_bank_registry: BankRegistry):
        bank = sample_bank_registry.find("ICBC")
        assert bank is not None
        assert bank.name == "ICBC"

    def test_by_market(self, sample_bank_registry: BankRegistry):
        us_banks = sample_bank_registry.by_market("US")
        assert len(us_banks) == 1
        assert us_banks[0].ticker == "JPM"

    def test_by_market_case_insensitive(self, sample_bank_registry: BankRegistry):
        uk_banks = sample_bank_registry.by_market("uk")
        assert len(uk_banks) == 1
        assert uk_banks[0].ticker == "BARC.L"

    def test_by_market_empty(self, sample_bank_registry: BankRegistry):
        assert sample_bank_registry.by_market("JP") == []

    def test_markets_property(self, sample_bank_registry: BankRegistry):
        assert sample_bank_registry.markets == ["CN", "UK", "US"]

    def test_empty_registry(self):
        reg = BankRegistry(banks=[])
        assert len(reg) == 0
        assert reg.find_by_ticker("ANY") is None
        assert reg.markets == []

    def test_invalid_yaml_structure_raises(self):
        with pytest.raises(ValidationError):
            BankRegistry.model_validate({"banks": [{"name": "Missing ticker"}]})
