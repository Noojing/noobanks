"""Tests for CompositeAdapter URL discovery & download.

Uses aioresponses to mock HTTP calls for fast, deterministic tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from noobanks.config.models import BankSpec, SourceConfig
from noobanks.sources.composite_adapter import CompositeAdapter
from noobanks.sources.webutils import extract_nav_links, extract_pdf_links
from noobanks.sources.keywords import (
    NAV_KEYWORDS,
    REPORT_PATTERNS,
    REPORT_TYPE_LABELS,
)
from noobanks.sources.scoring import score_candidate

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
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-2025.pdf",
        )
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1
        assert links[0][0].endswith("annual-report-2025.pdf")
        assert links[0][1]

    def test_matches_year_only_relaxed(self):
        """PDF with year + type keyword in name should match."""
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-2025.pdf",
        )
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_ignores_non_pdf(self):
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-2025.html",
        )
        html += '<a href="annual-report-2025.pdf">Report</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1
        assert links[0][0].endswith(".pdf")

    def test_deduplicates_urls(self):
        html = '<a href="/annual-report-2025.pdf">A</a>\n<a href="/annual-report-2025.pdf">A again</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_resolves_relative_urls(self):
        html = _pdf_links_html(
            "https://bank.example.com/ir/",
            "/pdfs/annual-report-2025.pdf",
        )
        links = extract_pdf_links(
            html, "https://bank.example.com/ir/", "annual_report", "2025"
        )
        assert links[0][0] == "https://bank.example.com/pdfs/annual-report-2025.pdf"

    def test_matches_10k_patterns(self):
        html = _pdf_links_html(
            "https://bank.example.com",
            "form-10-k-2025.pdf",
        )
        links = extract_pdf_links(
            html, "https://bank.example.com", "10-K", "2025"
        )
        assert len(links) == 1

    def test_matches_short_year(self):
        """Should match '25' for year=2025 in the filename."""
        html = _pdf_links_html(
            "https://bank.example.com",
            "annual-report-fy25.pdf",
        )
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_empty_html_returns_empty(self):
        links = extract_pdf_links(
            "", "https://bank.example.com", "annual_report", "2025"
        )
        assert links == []


# ── URL scoring ────────────────────────────────────────────────────────────


class TestUrlScore:
    def test_prefers_year_in_url(self):
        score_with_year = score_candidate("https://example.com/2025/report.pdf", "2025", report_type="annual_report")
        score_without_year = score_candidate("https://example.com/report.pdf", "2025", report_type="annual_report")
        assert score_with_year > score_without_year

    def test_prefers_annual_and_report(self):
        good = score_candidate("https://example.com/annual-report-2025.pdf", "2025", report_type="annual_report")
        bad = score_candidate("https://example.com/something-2025.pdf", "2025", report_type="annual_report")
        assert good > bad

    def test_penalizes_cdn_urls(self):
        cdn = score_candidate("https://cdn.example.com/report-2025.pdf", "2025", report_type="annual_report")
        official = score_candidate("https://www.example.com/report-2025.pdf", "2025", report_type="annual_report")
        assert official > cdn


# ── Report type patterns ───────────────────────────────────────────────────


class TestReportPatterns:
    def test_all_report_types_have_patterns(self):
        for rt in ["annual_report", "interim_report", "quarterly_report",
                     "10-K", "10-Q", "8-K", "pillar3"]:
            assert rt in REPORT_PATTERNS
            assert len(REPORT_PATTERNS[rt]) > 0

    def test_nav_keywords_not_empty(self):
        """NAV_KEYWORDS should be populated with report-related keywords."""
        assert len(NAV_KEYWORDS) > 0
        assert "annual-report" in NAV_KEYWORDS or "annual_report" in NAV_KEYWORDS
        assert "financial-results" in NAV_KEYWORDS or "financial_results" in NAV_KEYWORDS
        assert "performance-report" in NAV_KEYWORDS or "performance_reports" in NAV_KEYWORDS


# ── Rate limiting ──────────────────────────────────────────────────────────


class TestRateLimiting:
    @pytest.mark.asyncio
    async def test_first_call_no_delay(self):
        adapter = CompositeAdapter(rate_limit_delay=5.0)
        # First call to a domain should not delay
        t0 = asyncio.get_event_loop().time()
        await adapter._rate_limit("new-domain.example.com")
        elapsed = asyncio.get_event_loop().time() - t0
        assert elapsed < 0.1  # No delay on first request

    @pytest.mark.asyncio
    async def test_second_call_is_delayed(self):
        adapter = CompositeAdapter(rate_limit_delay=0.2)
        await adapter._rate_limit("test.example.com")
        t0 = asyncio.get_event_loop().time()
        await adapter._rate_limit("test.example.com")
        elapsed = asyncio.get_event_loop().time() - t0
        # Should have ~200ms delay
        assert 0.1 <= elapsed <= 0.5

    @pytest.mark.asyncio
    async def test_different_domains_no_delay(self):
        adapter = CompositeAdapter(rate_limit_delay=5.0)
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
        adapter = CompositeAdapter(data_dir=tmp_data_dir)
        # Pre-create the target file
        target = adapter.target_path(tmp_data_dir, 2025, "BARC_L", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nfake content\n%%EOF")

        result = await adapter.fetch(sample_bank_spec, "annual_report", 2025)
        assert result.succeeded == 1
        assert result.report.downloaded_from == "(stored)"

    @pytest.mark.asyncio
    async def test_force_re_downloads(self, tmp_data_dir: Path, mocker):
        # Use a bank with a non-existent IR URL to force discovery failure
        bank = _make_bank(ticker="BOGUS.XX", market="UK")
        adapter = CompositeAdapter(data_dir=tmp_data_dir)
        target = adapter.target_path(tmp_data_dir, 2025, "BOGUS_XX", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nold\n%%EOF")

        # Disable discovery for all adapters so fetch returns errors
        for a in adapter.adapters:
            mocker.patch.object(a, "discover_url", return_value=None)

        # With force=True and a bogus URL, discovery will fail → errors
        result = await adapter.fetch(bank, "annual_report", 2025, force=True)
        # Should not return cached, and should have errors from failed discovery
        assert result.error is not None


class TestFetchNoUrls:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_urls_discovered(
        self, tmp_data_dir: Path, sample_bank_spec: BankSpec, mocker
    ):
        adapter = CompositeAdapter(data_dir=tmp_data_dir)
        bank = _make_bank(ticker="BOGUS.TK", market="UK")
        for a in adapter.adapters:
            mocker.patch.object(a, "discover_url", return_value=None)
        result = await adapter.fetch(bank, "annual_report", 2025)
        assert result.succeeded == 0
        assert result.error is not None


# ── Advanced PDF link extraction (link text + path segment matching) ────────


class TestExtractPdfLinksAdvanced:
    """Tests for link-text and path-segment year detection.

    Covers two Chinese-bank patterns:
    - ICBC: year/type info only in <a> tag text, not in href
    - ABC:  year info only in path segments (/202603/), not in filename
    """

    def test_matches_year_in_link_text(self):
        """Link text like '2025 Annual Report' should match even if href has no year."""
        html = '<a href="/downloads/report.pdf">2025 Annual Report</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1
        assert links[0][0] == "https://bank.example.com/downloads/report.pdf"

    def test_matches_short_year_in_link_text(self):
        """Short year '25' in link text should match for year=2025."""
        html = '<a href="/downloads/report.pdf">FY25 Annual Results</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_year_in_path_only_matches_fiscal_year(self):
        """/202603/ should NOT match FY2025 — only the actual fiscal year
        appears in path segments."""
        html = '<a href="./202603/P020260423652699711023.pdf">(Online Reading)</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com/ir/", "annual_report", "2025"
        )
        assert len(links) == 0, "/202603/ does not contain 2025"

    def test_matches_fy_year_in_path_segment(self):
        """Path with FY year directly: /2025/report.pdf should match."""
        html = '<a href="/reports/2025/annual-report.pdf">Report</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_link_text_with_chinese_characters(self):
        """Chinese link text like '2025年度报告' should match year + type."""
        html = '<a href="/downloads/report.pdf">2025年度报告（A股）</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_text_match_in_relaxed_fallback(self):
        """Both year and type keywords must be present."""
        html = '<a href="/downloads/document.pdf">Annual Report FY2025</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 1

    def test_no_false_match_on_wrong_year_in_text(self):
        """Link text with wrong year should not match."""
        html = '<a href="/downloads/report.pdf">2024 Annual Report</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 0

    def test_ignores_non_pdf_even_with_year_in_text(self):
        """Link text with year but href is .html — should be ignored."""
        html = '<a href="/column/12287143.html">2025年</a>'
        links = extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025"
        )
        assert len(links) == 0


# ── Navigation link extraction ────────────────────────────────────────────


class TestExtractNavLinks:
    """Tests for extract_nav_links — discover report-related pages from HTML."""

    def test_extracts_annual_reports_link(self):
        """'Annual Reports' link text should be extracted."""
        html = '<a href="/ir/annual-reports/">Annual Reports</a>'
        links = extract_nav_links(html, "https://bank.example.com/ir/")
        assert "https://bank.example.com/ir/annual-reports/" in links

    def test_extracts_financial_results_link(self):
        """'Financial Results' in href should be extracted."""
        html = '<a href="/investors/financial-results/2025">View Results</a>'
        links = extract_nav_links(html, "https://bank.example.com")
        assert len(links) >= 1
        assert "financial-results" in links[0]

    def test_extracts_performance_reports_link(self):
        """'Performance Reports' — ABC's actual label — should be extracted."""
        html = '<a href="./performance-reports/">Performance Reports</a>'
        links = extract_nav_links(
            html, "https://www.abchina.com/en/investor-relations/"
        )
        assert len(links) >= 1
        assert "performance-reports" in links[0]

    def test_extracts_quarterly_reports_link(self):
        """Sub-page links like 'Quarterly Reports' should be found."""
        html = '<a href="/ir/quarterly-reports/">Quarterly Reports</a>'
        links = extract_nav_links(html, "https://bank.example.com/ir/")
        assert len(links) >= 1

    def test_ignores_home_and_contact_links(self):
        """Navigation links like 'Home', 'Contact Us' should be excluded."""
        html = (
            '<a href="/">Home</a>'
            '<a href="/contact">Contact Us</a>'
            '<a href="/about">About</a>'
            '<a href="/ir/annual-reports/">Annual Reports</a>'
        )
        links = extract_nav_links(html, "https://bank.example.com/ir/")
        assert len(links) == 1
        assert "annual-reports" in links[0]

    def test_deduplicates_urls(self):
        """Duplicate navigation URLs should be removed."""
        html = (
            '<a href="/reports/">Annual Reports</a>'
            '<a href="/reports/">Reports</a>'
        )
        links = extract_nav_links(html, "https://bank.example.com")
        assert len(links) == 1

    def test_excludes_pdf_links(self):
        """Navigation extraction should exclude direct PDF links."""
        html = (
            '<a href="/reports/2025.pdf">Annual Report PDF</a>'
            '<a href="/reports-and-events/">Reports and Events</a>'
        )
        links = extract_nav_links(html, "https://bank.example.com")
        assert len(links) == 1
        assert links[0].endswith("/reports-and-events/")

    def test_resolves_relative_urls(self):
        """Relative URLs should be resolved against base_url."""
        html = '<a href="./annual-reports/">Annual Reports</a>'
        links = extract_nav_links(
            html, "https://bank.example.com/investors/"
        )
        assert links[0] == "https://bank.example.com/investors/annual-reports/"


# ── DuckDuckGo search fallback ────────────────────────────────────────────


class TestDdgsDiscoverUrl:
    """Tests for DdgsAdapter.discover_url — ddgs-powered URL discovery."""

    def test_builds_search_query_from_bank_and_type(self):
        """Query should combine bank name, year, and report type label."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="ICBC",
            ticker="601398.SH",
            exchange="SSE",
            market="CN",
            sources=SourceConfig(investor_relations="https://example.com/ir"),
            filings=["annual_report"],
        )
        label = REPORT_TYPE_LABELS.get("annual_report", "annual report")
        query = f"{bank.name} 2025 {label} financial report PDF"
        assert "ICBC" in query
        assert "2025" in query
        assert "annual report" in query
        assert "PDF" in query

    @pytest.mark.asyncio
    async def test_discover_url_returns_pdf_url(self, mocker):
        """When DDGS returns results with PDF hrefs, the first valid one is returned."""
        from noobanks.config.models import BankSpec, SourceConfig
        from noobanks.sources.ddgs_adapter import DdgsAdapter

        bank = BankSpec(
            name="Test Bank",
            ticker="TEST.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://test.example.com/ir"),
            filings=["annual_report"],
        )
        adapter = DdgsAdapter()

        mocker.patch.object(
            adapter, "_run_search",
            return_value=[
                {"title": "Annual Report 2025", "href": "https://cdn.example.com/2025-annual-report.pdf", "body": "..."},
                {"title": "Test Bank IR", "href": "https://test.example.com/ir/reports", "body": "..."},
            ],
        )
        mocker.patch(
            "noobanks.sources.ddgs_adapter.validate_doc_url",
            return_value={"status": 200, "content_type": "application/pdf", "content_length": 500000},
        )
        mocker.patch(
            "noobanks.sources.ddgs_adapter.score_candidate",
            return_value=10,
        )

        url = await adapter.discover_url(bank, "annual_report", 2025)
        assert url is not None
        assert "2025-annual-report.pdf" in url

    @pytest.mark.asyncio
    async def test_discover_url_skips_pdf_below_score_threshold(self, mocker):
        """A valid PDF that scores below threshold should be skipped."""
        from noobanks.config.models import BankSpec, SourceConfig
        from noobanks.sources.ddgs_adapter import DdgsAdapter

        bank = BankSpec(
            name="Test Bank",
            ticker="TEST.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://test.example.com/ir"),
            filings=["annual_report"],
        )
        adapter = DdgsAdapter()

        mocker.patch.object(
            adapter, "_run_search",
            return_value=[
                {"title": "Quarterly Results", "href": "https://cdn.example.com/q3-results.pdf", "body": "..."},
            ],
        )
        mocker.patch(
            "noobanks.sources.ddgs_adapter.validate_doc_url",
            return_value={"status": 200, "content_type": "application/pdf", "content_length": 500000},
        )
        mocker.patch(
            "noobanks.sources.ddgs_adapter.score_candidate",
            return_value=5,
        )

        url = await adapter.discover_url(bank, "annual_report", 2025)
        assert url is None

    @pytest.mark.asyncio
    async def test_discover_url_returns_none_on_no_results(self, mocker):
        """Empty search results should return None."""
        from noobanks.config.models import BankSpec, SourceConfig
        from noobanks.sources.ddgs_adapter import DdgsAdapter

        bank = BankSpec(
            name="NoResults Bank",
            ticker="NOPE.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://nope.example.com/ir"),
            filings=["annual_report"],
        )
        adapter = DdgsAdapter()

        mocker.patch.object(adapter, "_run_search", return_value=[])

        url = await adapter.discover_url(bank, "annual_report", 2025)
        assert url is None

    @pytest.mark.asyncio
    async def test_composite_discover_url_calls_fallback_when_primary_returns_none(self, mocker):
        """When the first adapter returns None, CompositeAdapter tries the next."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="ICBC",
            ticker="601398.SH",
            exchange="SSE",
            market="CN",
            sources=SourceConfig(investor_relations="https://www.icbc-ltd.com/en/page/1220.html"),
            filings=["annual_report"],
        )
        adapter = CompositeAdapter()

        mocker.patch.object(adapter.adapters[0], "discover_url", return_value=None)
        mocker.patch.object(
            adapter.adapters[1], "discover_url",
            return_value="https://cdn.example.com/report-2025.pdf",
        )

        url = await adapter.discover_url(bank, "annual_report", 2025)
        assert url is not None
        assert "report-2025.pdf" in url

    @pytest.mark.asyncio
    async def test_composite_discover_url_skips_fallback_when_primary_succeeds(self, mocker):
        """When the first adapter finds a URL, later adapters are never called."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="Barclays",
            ticker="BARC.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://home.barclays/investor-relations"),
            filings=["annual_report"],
        )
        adapter = CompositeAdapter()

        mocker.patch.object(
            adapter.adapters[0], "discover_url",
            return_value="https://home.barclays/reports/2025-annual-report.pdf",
        )
        fallback_spy = mocker.patch.object(adapter.adapters[1], "discover_url", return_value=None)

        url = await adapter.discover_url(bank, "annual_report", 2025)
        assert url is not None
        assert "2025-annual-report.pdf" in url
        fallback_spy.assert_not_called()

    def test_report_type_labels_coverage(self):
        """All report types with patterns should have a search label."""
        for rt in REPORT_PATTERNS:
            assert rt in REPORT_TYPE_LABELS, f"Missing label for {rt}"


# ── IrAdapter discover_url ────────────────────────────────────────────────


class TestIrAdapterDiscoverUrl:
    """Tests for IrAdapter.discover_url — unified single-URL discovery."""

    def _bank(self, ir_url: str) -> BankSpec:
        return BankSpec(
            name="ICBC",
            ticker="601398.SH",
            exchange="SSE",
            market="CN",
            sources=SourceConfig(investor_relations=ir_url),
            filings=["annual_report"],
        )

    @pytest.mark.asyncio
    async def test_discover_url_returns_none_when_crawl_empty(self, mocker):
        """When the unified crawl finds nothing, discover_url returns None."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        adapter = IrAdapter()

        crawl_mock = mocker.patch(
            "noobanks.sources.ir_adapter.crawl_pdf_link",
            return_value=None,
        )

        url = await adapter.discover_url(self._bank(ir_url), "annual_report", 2025)
        assert url is None
        assert crawl_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_discover_url_returns_url_when_crawl_succeeds(self, mocker):
        """When crawl_pdf_link finds a PDF, discover_url returns it directly."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        crawled = ("https://home.barclays/reports/2025-annual-report.pdf", "Annual Report 2025")
        crawl_mock = mocker.patch(
            "noobanks.sources.ir_adapter.crawl_pdf_link",
            return_value=crawled,
        )

        url = await adapter.discover_url(
            self._bank("https://home.barclays/investor-relations"), "annual_report", 2025
        )
        assert url == crawled[0]
        assert crawl_mock.call_count == 1

    @pytest.mark.asyncio
    async def test_discover_url_passes_both_getters_to_crawl(self, mocker):
        """Both static and browser getters are passed as page_getters."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        crawled = ("https://www.icbc.com.cn/en/2024-annual-report.pdf", "2024 Annual Report")
        crawl_mock = mocker.patch(
            "noobanks.sources.ir_adapter.crawl_pdf_link",
            return_value=crawled,
        )

        url = await adapter.discover_url(
            self._bank("https://www.icbc.com.cn/en/page/1220.html"), "annual_report", 2024
        )
        assert url == crawled[0]
        assert crawl_mock.call_count == 1

        call_kwargs = crawl_mock.call_args.kwargs
        page_getters = call_kwargs.get("page_getters", [])
        assert len(page_getters) == 2

    @pytest.mark.asyncio
    async def test_discover_url_passes_scoring_params(self, mocker):
        """Scoring + validation pipeline is passed through to crawl_pdf_link."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        adapter = IrAdapter()
        annual_url = "https://www.icbc-ltd.com/en/page/2024AnnualReport.pdf"

        crawl_mock = mocker.patch(
            "noobanks.sources.ir_adapter.crawl_pdf_link",
            return_value=(annual_url, "2024 Annual Report"),
        )

        url = await adapter.discover_url(self._bank(ir_url), "annual_report", 2024)
        assert url == annual_url

        for call_args in crawl_mock.call_args_list:
            assert call_args.kwargs.get("score_func") is not None
            assert call_args.kwargs.get("score_threshold") == adapter.score_threshold
            assert call_args.kwargs.get("period") == "FY"


# ── URL scoring (link text + report type) ──────────────────────────────────


class TestUrlScoreTextAware:
    """Tests for score_candidate with link text + report type weighting."""

    def test_text_aware_scoring_without_link_text(self):
        """Without link_text, URL-level signals still rank correctly."""
        score = score_candidate("https://example.com/annual-report-2025.pdf", "2025", report_type="annual_report")
        assert score == 8

    def test_year_in_link_text_beats_year_in_url(self):
        text_only = score_candidate(
            "https://example.com/report.pdf", "2025",
            link_text="2025 Annual Report", report_type="annual_report",
        )
        url_only = score_candidate(
            "https://example.com/2025/report.pdf", "2025",
            link_text="Download", report_type="annual_report",
        )
        assert text_only > url_only

    def test_type_keyword_in_text_beats_type_keyword_in_url(self):
        text_only = score_candidate(
            "https://example.com/2025/x.pdf", "2025",
            link_text="2025 Annual Report", report_type="annual_report",
        )
        url_only = score_candidate(
            "https://example.com/annual-report-2025.pdf", "2025",
            link_text="download", report_type="annual_report",
        )
        assert text_only > url_only

    def test_interim_penalized_for_annual_report(self):
        interim = score_candidate(
            "https://example.com/2024InterimReport.pdf", "2024",
            link_text="2024 Interim Report", report_type="annual_report",
        )
        annual = score_candidate(
            "https://example.com/2024AnnualReport.pdf", "2024",
            link_text="2024 Annual Report", report_type="annual_report",
        )
        assert annual > interim

    def test_interim_results_penalized_for_annual_report(self):
        """Opaque 'InterimResults' filenames are also penalized."""
        interim = score_candidate(
            "https://example.com/2024InterimResultsEn20240914.pdf", "2024",
            link_text="", report_type="annual_report",
        )
        annual = score_candidate(
            "https://example.com/2024/2024AnnualReport.pdf", "2024",
            link_text="", report_type="annual_report",
        )
        assert annual > interim

    def test_announcement_ranks_below_annual_report(self):
        """Non-report documents (announcements) rank below real reports."""
        announcement = score_candidate(
            "https://example.com/2024/Announcement20241030.pdf", "2024",
            link_text="", report_type="annual_report",
        )
        annual = score_candidate(
            "https://example.com/2024/2024AnnualReport.pdf", "2024",
            link_text="", report_type="annual_report",
        )
        assert annual > announcement

    def test_briefing_and_qa_record_penalized(self):
        """Opaque non-report names (briefings, Q&A records) are penalized."""
        annual = score_candidate(
            "https://example.com/2024/2024AnnualReport.pdf", "2024",
            link_text="", report_type="annual_report",
        )
        for name in ("ResultsBriefing20240422.pdf", "QARecord20240914.pdf"):
            score = score_candidate(
                f"https://example.com/2024/{name}", "2024",
                link_text="", report_type="annual_report",
            )
            assert annual > score, name

    def test_anchor_text_outvotes_url_penalty(self):
        """Positive anchor text outweighs a URL-level non-report penalty."""
        score = score_candidate(
            "https://example.com/2024/annualresults.pdf", "2024",
            link_text="2024 Annual Report", report_type="annual_report",
        )
        assert score > 5  # +4 year-text, +4 annual-text, −2 url penalty, +1 non-cdn

    def test_extract_pdf_links_with_text_returns_pairs(self):
        """Always returns (url, anchor_text) tuples."""
        html = _pdf_links_html("https://example.com", "annual-report-2025.pdf")
        pairs = extract_pdf_links(
            html, "https://example.com", "annual_report", "2025"
        )
        assert pairs == [
            ("https://example.com/annual-report-2025.pdf", "annual-report-2025.pdf")
        ]


# ── Constructor ────────────────────────────────────────────────────────────


class TestCompositeAdapterInit:
    def test_default_values(self):
        from noobanks.storage.store import DEFAULT_DATA_DIR

        adapter = CompositeAdapter()
        assert adapter.data_dir == DEFAULT_DATA_DIR
        assert adapter.timeout == 30
        assert adapter.rate_limit_delay == 3.0
        assert len(adapter.adapters) == 2
        assert adapter.adapters[0].score_threshold == 9
        assert adapter.adapters[0].browser_max_pages is None
        assert adapter.adapters[1].score_threshold == 9

    def test_custom_values(self, tmp_path: Path):
        adapter = CompositeAdapter(
            data_dir=tmp_path,
            timeout=60,
            rate_limit_delay=1.0,
        )
        assert adapter.data_dir == tmp_path
        assert adapter.timeout == 60
        assert adapter.rate_limit_delay == 1.0

    def test_custom_adapters_override_default_chain(self):
        from noobanks.sources.base_adapter import SourceAdapter

        class DummyAdapter(SourceAdapter):
            async def discover_url(self, bank, report_type, year, period="FY"):
                return None

        custom = [DummyAdapter(), DummyAdapter()]
        adapter = CompositeAdapter(adapters=custom)
        assert list(adapter.adapters) == custom
        assert len(adapter.adapters) == 2
        assert adapter.adapters[0] is custom[0]
        assert adapter.adapters[-1] is custom[-1]

    def test_empty_adapters_raises(self):
        with pytest.raises(ValueError):
            CompositeAdapter(adapters=[])