"""Shared fixtures for the noobanks test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from noobanks.config.models import (
    BankRegistry,
    BankSpec,
    SourceConfig,
)


@pytest.fixture
def sample_source_config() -> SourceConfig:
    return SourceConfig(
        investor_relations="https://home.barclays/investor-relations",
    )


@pytest.fixture
def sample_bank_spec(sample_source_config: SourceConfig) -> BankSpec:
    return BankSpec(
        name="Barclays PLC",
        ticker="BARC.L",
        exchange="LSE",
        market="UK",
        sources=sample_source_config,
        filings=["annual_report", "interim_report", "quarterly_report"],
    )


@pytest.fixture
def sample_us_bank_spec() -> BankSpec:
    return BankSpec(
        name="JPMorgan Chase & Co.",
        ticker="JPM",
        exchange="NYSE",
        market="US",
        cik="0000019617",
        sources=SourceConfig(investor_relations="https://www.jpmorganchase.com/ir"),
        filings=["10-K", "10-Q", "8-K"],
    )


@pytest.fixture
def sample_cn_bank_spec() -> BankSpec:
    return BankSpec(
        name="ICBC",
        ticker="601398.SH",
        exchange="SSE",
        market="CN",
        sources=SourceConfig(
            investor_relations="https://www.icbc-ltd.com/en/page/1220435982957096960.html",
        ),
        filings=["annual_report", "interim_report", "quarterly_report"],
    )


@pytest.fixture
def sample_bank_registry(
    sample_bank_spec: BankSpec,
    sample_us_bank_spec: BankSpec,
    sample_cn_bank_spec: BankSpec,
) -> BankRegistry:
    return BankRegistry(
        banks=[sample_bank_spec, sample_us_bank_spec, sample_cn_bank_spec]
    )


@pytest.fixture
def tmp_yaml_config(tmp_path: Path) -> Path:
    """Write a minimal banks.yaml to a temp directory and return its path."""
    data = {
        "banks": [
            {
                "name": "Test Bank",
                "ticker": "TEST.L",
                "exchange": "LSE",
                "market": "UK",
                "sources": {
                    "investor_relations": "https://test.bank/ir",
                },
                "filings": ["annual_report"],
            }
        ]
    }
    config_path = tmp_path / "banks.yaml"
    config_path.write_text(yaml.dump(data), encoding="utf-8")
    return config_path


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory with raw/processed/output structure."""
    data_dir = tmp_path / "data"
    for sub in ["raw", "processed", "output"]:
        (data_dir / sub).mkdir(parents=True)
    return data_dir


@pytest.fixture
def tmp_pdf(tmp_path: Path) -> Path:
    """Create a minimal valid PDF file in a temp directory."""
    pdf = tmp_path / "test_report.pdf"
    # Minimal valid PDF: header + catalog + trailer
    content = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]>>endobj\n"
        b"xref\n0 4\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n0000000115 00000 n \n"
        b"trailer<</Size 4/Root 1 0 R>>\n"
        b"startxref\n190\n%%EOF"
    )
    pdf.write_bytes(content)
    return pdf