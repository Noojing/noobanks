"""Tests for noobanks.sources.base — Report, FetchResult, SourceAdapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from noobanks.config.models import BankSpec, SourceConfig
from noobanks.sources.base import FetchResult, Report, SourceAdapter


class TestReport:
    def test_full_construction(self):
        r = Report(
            bank_ticker="BARC.L",
            report_type="annual_report",
            year=2025,
            period="FY",
            local_path=Path("/tmp/BARC_L_annual_report_FY.pdf"),
            downloaded_from="https://example.com/report.pdf",
            file_size=10_485_760,
            content_hash="abc123",
        )
        assert r.bank_ticker == "BARC.L"
        assert r.report_type == "annual_report"
        assert r.year == 2025
        assert r.period == "FY"
        assert r.file_size == 10_485_760
        assert r.content_hash == "abc123"

    def test_default_values(self):
        r = Report(
            bank_ticker="JPM",
            report_type="10-K",
            year=2025,
            period="FY",
            local_path=Path("/tmp/JPM_10-K_FY.pdf"),
            downloaded_from="https://example.com/report.pdf",
        )
        assert r.file_size == 0
        assert r.content_hash == ""
        assert r.downloaded_at is not None

    def test_filename_property(self):
        r = Report(
            bank_ticker="BAC",
            report_type="10-K",
            year=2025,
            period="FY",
            local_path=Path("/data/raw/2025/BAC_10-K_FY.pdf"),
            downloaded_from="https://example.com/report.pdf",
        )
        assert r.filename == "BAC_10-K_FY.pdf"

    def test_size_mb_property(self):
        r = Report(
            bank_ticker="BAC",
            report_type="10-K",
            year=2025,
            period="FY",
            local_path=Path("/tmp/test.pdf"),
            downloaded_from="https://example.com/report.pdf",
            file_size=5_242_880,
        )
        assert r.size_mb == 5.0

    def test_size_mb_zero(self):
        r = Report(
            bank_ticker="BAC",
            report_type="10-K",
            year=2025,
            period="FY",
            local_path=Path("/tmp/test.pdf"),
            downloaded_from="https://example.com/report.pdf",
            file_size=0,
        )
        assert r.size_mb == 0.0


class TestFetchResult:
    def test_empty_result(self, sample_bank_spec: BankSpec):
        fr = FetchResult(bank=sample_bank_spec)
        assert fr.bank.ticker == "BARC.L"
        assert fr.succeeded == 0
        assert fr.failed == 0
        assert fr.ok is False

    def test_with_reports(self, sample_bank_spec: BankSpec):
        r = Report(
            bank_ticker="BARC.L",
            report_type="annual_report",
            year=2025,
            period="FY",
            local_path=Path("/tmp/test.pdf"),
            downloaded_from="https://example.com/report.pdf",
        )
        fr = FetchResult(bank=sample_bank_spec, report=r)
        assert fr.succeeded == 1
        assert fr.failed == 0
        assert fr.ok is True

    def test_with_errors(self, sample_bank_spec: BankSpec):
        fr = FetchResult(bank=sample_bank_spec, error="404 Not Found")
        assert fr.succeeded == 0
        assert fr.failed == 1
        assert fr.ok is False  # no reports, has errors

    def test_with_both_report_and_error_not_ok(self, sample_bank_spec: BankSpec):
        """ok is True only when there are reports AND zero errors."""
        r = Report(
            bank_ticker="BARC.L",
            report_type="annual_report",
            year=2025,
            period="FY",
            local_path=Path("/tmp/test.pdf"),
            downloaded_from="https://example.com/report.pdf",
        )
        fr = FetchResult(
            bank=sample_bank_spec,
            report=r,
            error="Minor warning",
        )
        assert fr.succeeded == 1
        assert fr.failed == 1
        assert fr.ok is False


class TestSourceAdapter:
    def test_target_path_convention(self):
        path = SourceAdapter.target_path(
            base_dir=Path("src/data"),
            year=2025,
            ticker_safe="BARC_L",
            report_type="annual_report",
            period="FY",
        )
        assert path == Path("src/data/raw/2025/BARC_L_annual_report_FY.pdf")

    def test_target_path_quarterly(self):
        path = SourceAdapter.target_path(
            base_dir=Path("data"),
            year=2026,
            ticker_safe="JPM",
            report_type="10-Q",
            period="Q3",
        )
        assert path == Path("data/raw/2026/JPM_10-Q_Q3.pdf")

    def test_compute_hash(self, tmp_pdf: Path):
        class ConcreteSourceAdapter(SourceAdapter):
            async def fetch(self, *args, **kwargs): ...
            async def discover_url(self, *args, **kwargs): ...

        adapter = ConcreteSourceAdapter()
        h = adapter._compute_hash(tmp_pdf)
        assert len(h) == 64  # SHA-256 hex digest
        assert h == adapter._compute_hash(tmp_pdf)  # deterministic

    def test_compute_hash_different_files(self, tmp_pdf: Path, tmp_path: Path):
        pdf2 = tmp_path / "other.pdf"
        pdf2.write_bytes(b"%PDF-1.4\ndifferent content\n%%EOF")

        class ConcreteSourceAdapter(SourceAdapter):
            async def fetch(self, *args, **kwargs): ...
            async def discover_url(self, *args, **kwargs): ...

        adapter = ConcreteSourceAdapter()
        assert adapter._compute_hash(tmp_pdf) != adapter._compute_hash(pdf2)

    def test_abc_cannot_instantiate_without_abstract_methods(self):
        """SourceAdapter requires subclasses to implement abstract methods."""
        with pytest.raises(TypeError):
            SourceAdapter()  # type: ignore[abstract]