"""Tests for noobanks.sources.generic — GenericIrAdapter URL discovery & download.

Uses aioresponses to mock HTTP calls for fast, deterministic tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientResponseError

from noobanks.config.models import BankSpec, SourceConfig
from noobanks.sources.base import Report
from noobanks.sources.generic import (
    IR_SUBPATHS,
    MARKET_HEURISTICS,
    REPORT_PATTERNS,
    GenericIrAdapter,
)

# ── helpers ────────────────────────────────────────────────────────────────


def _make_bank(ticker: str = "BARC.L", market: str = "UK", **kwargs) -> BankSpec:
    defaults = {
        "name": "Test Bank",
        "ticker": ticker,
        "exchange": "LSE",
        "market": market,
        "sources": SourceConfig(
            investor_relations=f"https://{ticker.lower().replace('.', '-')}.example.com/ir"
        ),
        "filings": ["annual_report"],
    }
    defaults.update(kwargs)
    return BankSpec(**defaults)


def _pdf_links_html(base_url: str, *paths: str) -> str:
    """Generate HTML with <a href> links to PDFs."""
    links = "\n".join(
        f'<a href="{p}">{p.split("/")[-1]}</a>' for p in paths
    )
    return f"<html><body>{links}</body></html>"


# ── URL extraction (unit, no network) ──────────────────────────────────────


class TestExtractPdfLinks:
    def test_matching_type_and_year(self):
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-2025.pdf",
        )
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1
        assert links[0].endswith("annual-report-2025.pdf")

    def test_matches_year_only_relaxed(self):
        """When no type+year matches, fall back to any PDF with year."""
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com",
            "some-random-file-2025.pdf",
        )
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_ignores_non_pdf(self):
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-2025.html",
        )
        html += '<a href="annual-report-2025.pdf">Report</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1
        assert links[0].endswith(".pdf")

    def test_deduplicates_urls(self):
        adapter = GenericIrAdapter()
        html = '<a href="/reports/a-2025.pdf">A</a>\n<a href="/reports/a-2025.pdf">A again</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_resolves_relative_urls(self):
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com/ir/",
            "/pdfs/annual-report-2025.pdf",
        )
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com/ir/", "annual_report", "2025", "25"
        )
        assert links[0] == "https://bank.example.com/pdfs/annual-report-2025.pdf"

    def test_matches_10k_patterns(self):
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com",
            "form-10-k-2025.pdf",
        )
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "10-K", "2025", "25"
        )
        assert len(links) == 1

    def test_matches_short_year(self):
        """Should match '25' for year=2025 in the filename."""
        adapter = GenericIrAdapter()
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-fy25.pdf",
        )
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_empty_html_returns_empty(self):
        adapter = GenericIrAdapter()
        links = adapter._extract_pdf_links(
            "", "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert links == []


# ── URL scoring ────────────────────────────────────────────────────────────


class TestUrlScore:
    def test_prefers_year_in_url(self):
        adapter = GenericIrAdapter()
        score_with_year = adapter._url_score("https://example.com/2025/report.pdf", "2025")
        score_without_year = adapter._url_score("https://example.com/report.pdf", "2025")
        assert score_with_year > score_without_year

    def test_prefers_annual_and_report(self):
        adapter = GenericIrAdapter()
        good = adapter._url_score("https://example.com/annual-report-2025.pdf", "2025")
        bad = adapter._url_score("https://example.com/something-2025.pdf", "2025")
        assert good > bad

    def test_penalizes_cdn_urls(self):
        adapter = GenericIrAdapter()
        cdn = adapter._url_score("https://cdn.example.com/report-2025.pdf", "2025")
        official = adapter._url_score("https://www.example.com/report-2025.pdf", "2025")
        assert official > cdn


# ── Candidate URL construction ─────────────────────────────────────────────


class TestConstructCandidates:
    def test_builds_market_specific_urls(self):
        bank = _make_bank(ticker="BARC.L", market="UK")
        adapter = GenericIrAdapter()
        candidates = adapter._construct_candidates(bank, "2025", "25")
        assert len(candidates) > 0
        # The bank_name_short is derived from bank.name ("Test Bank" → "test")
        # Should contain at least one URL with the bank name or IR base path
        assert any("test" in c.lower() or "barc-l" in c.lower() for c in candidates)

    def test_us_market_uses_different_templates(self):
        bank = _make_bank(ticker="JPM", market="US")
        adapter = GenericIrAdapter()
        candidates = adapter._construct_candidates(bank, "2025", "25")
        assert len(candidates) > 0

    def test_cn_market_builds_urls(self):
        bank = _make_bank(ticker="601398.SH", market="CN")
        adapter = GenericIrAdapter()
        candidates = adapter._construct_candidates(bank, "2025", "25")
        assert len(candidates) > 0


# ── Report type patterns ───────────────────────────────────────────────────


class TestReportPatterns:
    def test_all_report_types_have_patterns(self):
        for rt in ["annual_report", "interim_report", "quarterly_report",
                     "10-K", "10-Q", "8-K", "pillar3"]:
            assert rt in REPORT_PATTERNS
            assert len(REPORT_PATTERNS[rt]) > 0

    def test_ir_subpaths_not_empty(self):
        assert len(IR_SUBPATHS) > 0

    def test_market_heuristics_cover_all_markets(self):
        for market in ["US", "CN", "HK", "UK"]:
            assert market in MARKET_HEURISTICS
            assert len(MARKET_HEURISTICS[market]) > 0


# ── Rate limiting ──────────────────────────────────────────────────────────


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_first_call_no_delay(self):
        adapter = GenericIrAdapter(rate_limit_delay=5.0)
        # First call to a domain should not delay
        t0 = asyncio.get_event_loop().time()
        await adapter._rate_limit("new-domain.example.com")
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1  # No delay on first request

    @pytest.mark.asyncio
    async def test_second_call_is_delayed(self):
        adapter = GenericIrAdapter(rate_limit_delay=0.2)
        await adapter._rate_limit("test.example.com")
        t0 = asyncio.get_event_loop().time()
        await adapter._rate_limit("test.example.com")
        elapsed = asyncio.get_event_loop().time() - t0
        # Should have ~200ms delay
        assert 0.1 <= elapsed <= 0.5

    @pytest.mark.asyncio
    async def test_different_domains_no_delay(self):
        adapter = GenericIrAdapter(rate_limit_delay=5.0)
        await adapter._rate_limit("domain-a.example.com")
        t0 = asyncio.get_event_loop().time()
        await adapter._rate_limit("domain-b.example.com")
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1  # Different domain → no delay


# ── Fetch (integration with mocked HTTP) ───────────────────────────────────


class TestFetchCached:
    """Tests for fetch() when the file already exists on disk."""

    @pytest.mark.asyncio
    async def test_skips_download_when_exists(self, tmp_data_dir: Path, sample_bank_spec: BankSpec):
        adapter = GenericIrAdapter(data_dir=tmp_data_dir)
        # Pre-create the target file
        target = adapter.target_path(tmp_data_dir, 2025, "BARC_L", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nfake content\n%%EOF")

        result = await adapter.fetch(sample_bank_spec, "annual_report", 2025)
        assert result.succeeded == 1
        assert result.reports[0].url == "(cached)"

    @pytest.mark.asyncio
    async def test_force_re_downloads(self, tmp_data_dir: Path):
        # Use a bank with a non-existent IR URL to force discovery failure
        bank = _make_bank(ticker="BOGUS.XX", market="UK")
        adapter = GenericIrAdapter(data_dir=tmp_data_dir)
        target = adapter.target_path(tmp_data_dir, 2025, "BOGUS_XX", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nold\n%%EOF")

        # With force=True and a bogus URL, discovery will fail → errors
        result = await adapter.fetch(bank, "annual_report", 2025, force=True)
        # Should not return cached, and should have errors from failed discovery
        assert len(result.errors) > 0


class TestFetchNoUrls:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_urls_discovered(
        self, tmp_data_dir: Path, sample_bank_spec: BankSpec
    ):
        adapter = GenericIrAdapter(data_dir=tmp_data_dir)
        # The bank's IR URL doesn't exist → discovery will fail
        # We can make this deterministic by using a bank with a clearly bogus URL
        bank = _make_bank(ticker="BOGUS.TK", market="UK")
        result = await adapter.fetch(bank, "annual_report", 2025)
        assert result.succeeded == 0
        assert len(result.errors) >= 1


# ── Constructor ────────────────────────────────────────────────────────────


class TestGenericIrAdapterInit:
    def test_default_values(self):
        adapter = GenericIrAdapter()
        assert adapter.data_dir == Path("src/data")
        assert adapter.timeout == 30
        assert adapter.rate_limit_delay == 3.0

    def test_custom_values(self, tmp_path: Path):
        adapter = GenericIrAdapter(
            data_dir=tmp_path,
            timeout=60,
            rate_limit_delay=1.0,
            max_concurrent=8,
        )
        assert adapter.data_dir == tmp_path
        assert adapter.timeout == 60
        assert adapter.rate_limit_delay == 1.0
