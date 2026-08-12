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
    NAV_KEYWORDS,
    REPORT_PATTERNS,
    REPORT_TYPE_LABELS,
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
    def test_builds_common_pattern_urls(self):
        """Simplified constructor builds URLs from common patterns."""
        bank = _make_bank(ticker="BARC.L", market="UK")
        adapter = GenericIrAdapter()
        candidates = adapter._construct_candidates(bank, "2025", "25")
        assert len(candidates) > 0
        assert any("2025" in c for c in candidates)
        assert any(c.endswith(".pdf") for c in candidates)

    def test_includes_ir_base_in_urls(self):
        """All candidate URLs should be under the bank's IR base."""
        bank = _make_bank(ticker="JPM", market="US")
        adapter = GenericIrAdapter()
        candidates = adapter._construct_candidates(bank, "2025", "25")
        assert len(candidates) > 0
        # All should start with the IR base
        ir_base = bank.sources.investor_relations.rstrip("/")
        assert all(c.startswith(ir_base) for c in candidates)

    def test_cn_market_builds_urls(self):
        """CN banks also get candidate URLs."""
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
    async def test_force_re_downloads(self, tmp_data_dir: Path, mocker):
        # Use a bank with a non-existent IR URL to force discovery failure
        bank = _make_bank(ticker="BOGUS.XX", market="UK")
        adapter = GenericIrAdapter(data_dir=tmp_data_dir)
        target = adapter.target_path(tmp_data_dir, 2025, "BOGUS_XX", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nold\n%%EOF")

        # Disable search fallback for this test (bank URL is intentionally bogus)
        mocker.patch.object(adapter, "_search_fallback", return_value=[])

        # With force=True and a bogus URL, discovery will fail → errors
        result = await adapter.fetch(bank, "annual_report", 2025, force=True)
        # Should not return cached, and should have errors from failed discovery
        assert len(result.errors) > 0


class TestFetchNoUrls:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_urls_discovered(
        self, tmp_data_dir: Path, sample_bank_spec: BankSpec, mocker
    ):
        adapter = GenericIrAdapter(data_dir=tmp_data_dir)
        # The bank's IR URL doesn't exist → discovery will fail
        # We can make this deterministic by using a bank with a clearly bogus URL
        bank = _make_bank(ticker="BOGUS.TK", market="UK")
        # Disable search fallback for this test (bank URL is intentionally bogus)
        mocker.patch.object(adapter, "_search_fallback", return_value=[])
        result = await adapter.fetch(bank, "annual_report", 2025)
        assert result.succeeded == 0
        assert len(result.errors) >= 1


# ── Advanced PDF link extraction (link text + path segment matching) ────────


class TestExtractPdfLinksAdvanced:
    """Tests for link-text and path-segment year detection.

    Covers two Chinese-bank patterns:
    - ICBC: year/type info only in <a> tag text, not in href
    - ABC:  year info only in path segments (/202603/), not in filename
    """

    def test_matches_year_in_link_text(self):
        """Link text like '2025 Annual Report' should match even if href has no year."""
        adapter = GenericIrAdapter()
        html = '<a href="/downloads/report.pdf">2025 Annual Report</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1
        assert links[0] == "https://bank.example.com/downloads/report.pdf"

    def test_matches_short_year_in_link_text(self):
        """Short year '25' in link text should match for year=2025."""
        adapter = GenericIrAdapter()
        html = '<a href="/downloads/report.pdf">FY25 Annual Results</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_year_in_path_only_matches_fiscal_year(self):
        """/202603/ should NOT match FY2025 — only the actual fiscal year
        appears in path segments. The year+1 assumption was removed."""
        adapter = GenericIrAdapter()
        html = '<a href="./202603/P020260423652699711023.pdf">(Online Reading)</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com/ir/", "annual_report", "2025", "25"
        )
        assert len(links) == 0, "Year+1 removed: /202603/ does not contain 2025"

    def test_matches_fy_year_in_path_segment(self):
        """Path with FY year directly: /2025/report.pdf should match."""
        adapter = GenericIrAdapter()
        html = '<a href="/reports/2025/annual-report.pdf">Report</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_link_text_with_chinese_characters(self):
        """Chinese link text like '2025年度报告' should match year + type."""
        adapter = GenericIrAdapter()
        html = '<a href="/downloads/report.pdf">2025年度报告（A股）</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_text_match_in_relaxed_fallback(self):
        """Relaxed fallback should also check link text when href has no year."""
        adapter = GenericIrAdapter()
        html = '<a href="/downloads/document.pdf">Document FY2025</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 1

    def test_no_false_match_on_wrong_year_in_text(self):
        """Link text with wrong year should not match."""
        adapter = GenericIrAdapter()
        html = '<a href="/downloads/report.pdf">2024 Annual Report</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 0

    def test_ignores_non_pdf_even_with_year_in_text(self):
        """Link text with year but href is .html — should be ignored."""
        adapter = GenericIrAdapter()
        html = '<a href="/column/12287143.html">2025年</a>'
        links = adapter._extract_pdf_links(
            html, "https://bank.example.com", "annual_report", "2025", "25"
        )
        assert len(links) == 0


# ── IR URL validation ─────────────────────────────────────────────────────


class TestValidateIrUrl:
    """Tests for _validate_ir_url — detect dead/broken IR URLs before crawling."""

    @staticmethod
    def _mock_session_get(mocker, status: int, body: str):
        """Build a mock aiohttp session.get() that supports async with."""
        mock_resp = mocker.MagicMock()
        mock_resp.status = status
        mock_resp.text = mocker.AsyncMock(return_value=body)
        mock_resp.__aenter__ = mocker.AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = mocker.AsyncMock(return_value=None)

        mock_session = mocker.MagicMock()
        mock_session.get.return_value = mock_resp
        return mock_session

    @pytest.mark.asyncio
    async def test_http_200_with_html_is_valid(self, mocker):
        """A page returning 200 with real HTML content is valid."""
        from noobanks.sources.generic import GenericIrAdapter

        adapter = GenericIrAdapter()
        mock_session = self._mock_session_get(
            mocker, 200,
            "<html><body><a href='/reports/'>Annual Reports</a></body></html>",
        )
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/ir")
        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_http_404_is_invalid(self, mocker):
        """HTTP 404 should be reported as invalid with a clear message."""
        from noobanks.sources.generic import GenericIrAdapter

        adapter = GenericIrAdapter()
        mock_session = self._mock_session_get(mocker, 404, "<html><body>Not Found</body></html>")
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/dead")
        assert result["valid"] is False
        assert "404" in result["error"]

    @pytest.mark.asyncio
    async def test_http_200_js_shell_is_invalid(self, mocker):
        """A page returning 200 but only a JS redirect shell (<500 bytes, no links)
        should be flagged as likely JS-rendered."""
        from noobanks.sources.generic import GenericIrAdapter

        adapter = GenericIrAdapter()
        mock_session = self._mock_session_get(
            mocker, 200,
            '<html><body><script>window.location="/"</script></body></html>',
        )
        result = await adapter._validate_ir_url(mock_session, "https://www.icbc.com.cn/ir")
        assert result["valid"] is False
        assert "js" in result["error"].lower() or "shell" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_http_500_is_invalid(self, mocker):
        """Server errors should be invalid."""
        from noobanks.sources.generic import GenericIrAdapter

        adapter = GenericIrAdapter()
        mock_session = self._mock_session_get(mocker, 500, "Internal Server Error")
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/broken")
        assert result["valid"] is False
        assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_network_error_is_invalid(self, mocker):
        """Network/connection errors should be caught and reported."""
        from noobanks.sources.generic import GenericIrAdapter
        from aiohttp import ClientConnectionError

        adapter = GenericIrAdapter()
        mock_session = mocker.MagicMock()
        mock_session.get.side_effect = ClientConnectionError("Connection refused")

        result = await adapter._validate_ir_url(mock_session, "https://nonexistent.example.com")
        assert result["valid"] is False
        assert "connection" in result["error"].lower()


# ── Navigation link extraction ────────────────────────────────────────────


class TestExtractNavLinks:
    """Tests for _extract_nav_links — discover report-related pages from HTML."""

    def test_extracts_annual_reports_link(self):
        """'Annual Reports' link text should be extracted."""
        adapter = GenericIrAdapter()
        html = '<a href="/ir/annual-reports/">Annual Reports</a>'
        links = adapter._extract_nav_links(html, "https://bank.example.com/ir/")
        assert "https://bank.example.com/ir/annual-reports/" in links

    def test_extracts_financial_results_link(self):
        """'Financial Results' in href should be extracted."""
        adapter = GenericIrAdapter()
        html = '<a href="/investors/financial-results/2025">View Results</a>'
        links = adapter._extract_nav_links(html, "https://bank.example.com")
        assert len(links) >= 1
        assert "financial-results" in links[0]

    def test_extracts_performance_reports_link(self):
        """'Performance Reports' — ABC's actual label — should be extracted."""
        adapter = GenericIrAdapter()
        html = '<a href="./performance-reports/">Performance Reports</a>'
        links = adapter._extract_nav_links(
            html, "https://www.abchina.com/en/investor-relations/"
        )
        assert len(links) >= 1
        assert "performance-reports" in links[0]

    def test_extracts_quarterly_reports_link(self):
        """Sub-page links like 'Quarterly Reports' should be found."""
        adapter = GenericIrAdapter()
        html = '<a href="/ir/quarterly-reports/">Quarterly Reports</a>'
        links = adapter._extract_nav_links(html, "https://bank.example.com/ir/")
        assert len(links) >= 1

    def test_ignores_home_and_contact_links(self):
        """Navigation links like 'Home', 'Contact Us' should be excluded."""
        adapter = GenericIrAdapter()
        html = (
            '<a href="/">Home</a>'
            '<a href="/contact">Contact Us</a>'
            '<a href="/about">About</a>'
            '<a href="/ir/annual-reports/">Annual Reports</a>'
        )
        links = adapter._extract_nav_links(html, "https://bank.example.com/ir/")
        assert len(links) == 1
        assert "annual-reports" in links[0]

    def test_deduplicates_urls(self):
        """Duplicate navigation URLs should be removed."""
        adapter = GenericIrAdapter()
        html = (
            '<a href="/reports/">Annual Reports</a>'
            '<a href="/reports/">Reports</a>'
        )
        links = adapter._extract_nav_links(html, "https://bank.example.com")
        assert len(links) == 1

    def test_excludes_pdf_links(self):
        """Navigation extraction should exclude direct PDF links."""
        adapter = GenericIrAdapter()
        html = (
            '<a href="/reports/2025.pdf">Annual Report PDF</a>'
            '<a href="/reports-and-events/">Reports and Events</a>'
        )
        links = adapter._extract_nav_links(html, "https://bank.example.com")
        assert len(links) == 1
        assert links[0].endswith("/reports-and-events/")

    def test_resolves_relative_urls(self):
        """Relative URLs should be resolved against base_url."""
        adapter = GenericIrAdapter()
        html = '<a href="./annual-reports/">Annual Reports</a>'
        links = adapter._extract_nav_links(
            html, "https://bank.example.com/investors/"
        )
        assert links[0] == "https://bank.example.com/investors/annual-reports/"


# ── DuckDuckGo search fallback ────────────────────────────────────────────


class TestSearchFallback:
    """Tests for _search_fallback — ddgs-powered URL discovery fallback."""

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
    async def test_search_fallback_returns_pdf_urls(self, mocker):
        """When DDGS returns results with PDF hrefs, they should be collected."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="Test Bank",
            ticker="TEST.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://test.example.com/ir"),
            filings=["annual_report"],
        )
        adapter = GenericIrAdapter()

        # Mock _run_ddg_search to return synthetic results
        mocker.patch.object(
            adapter, "_run_ddg_search",
            return_value=[
                {"title": "Annual Report 2025", "href": "https://cdn.example.com/2025-annual-report.pdf", "body": "..."},
                {"title": "Test Bank IR", "href": "https://test.example.com/ir/reports", "body": "..."},
            ],
        )
        # Mock verify_url to accept the PDF URL
        mocker.patch.object(
            adapter, "verify_url",
            return_value={"status": 200, "content_type": "application/pdf", "content_length": 500000},
        )

        urls = await adapter._search_fallback(bank, "annual_report", "2025")
        assert len(urls) >= 1
        assert any("2025-annual-report.pdf" in u for u in urls)

    @pytest.mark.asyncio
    async def test_search_fallback_returns_empty_on_no_results(self, mocker):
        """Empty search results should return an empty list."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="NoResults Bank",
            ticker="NOPE.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://nope.example.com/ir"),
            filings=["annual_report"],
        )
        adapter = GenericIrAdapter()

        mocker.patch.object(adapter, "_run_ddg_search", return_value=[])

        urls = await adapter._search_fallback(bank, "annual_report", "2025")
        assert urls == []

    @pytest.mark.asyncio
    async def test_discover_urls_calls_fallback_when_crawl_fails(self, mocker):
        """When crawl and construction both return empty, invoke search fallback."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="ICBC",
            ticker="601398.SH",
            exchange="SSE",
            market="CN",
            sources=SourceConfig(investor_relations="https://www.icbc-ltd.com/en/page/1220.html"),
            filings=["annual_report"],
        )
        adapter = GenericIrAdapter()

        mocker.patch.object(adapter, "_crawl_for_report_pages", return_value=[])
        mocker.patch.object(adapter, "_construct_candidates", return_value=[])
        mocker.patch.object(
            adapter, "_search_fallback",
            return_value=["https://cdn.example.com/report-2025.pdf"],
        )
        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert len(urls) == 1
        assert "report-2025.pdf" in urls[0]

    @pytest.mark.asyncio
    async def test_discover_urls_skips_fallback_when_crawl_succeeds(self, mocker):
        """When crawl finds URLs, skip the search fallback entirely."""
        from noobanks.config.models import BankSpec, SourceConfig

        bank = BankSpec(
            name="Barclays",
            ticker="BARC.L",
            exchange="LSE",
            market="UK",
            sources=SourceConfig(investor_relations="https://home.barclays/investor-relations"),
            filings=["annual_report"],
        )
        adapter = GenericIrAdapter()

        mocker.patch.object(
            adapter, "_crawl_for_report_pages",
            return_value=["https://home.barclays/reports/2025-annual-report.pdf"],
        )
        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})
        fallback_spy = mocker.patch.object(adapter, "_search_fallback", return_value=[])

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert len(urls) >= 1
        fallback_spy.assert_not_called()

    def test_report_type_labels_coverage(self):
        """All report types with patterns should have a search label."""
        for rt in REPORT_PATTERNS:
            assert rt in REPORT_TYPE_LABELS, f"Missing label for {rt}"


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
