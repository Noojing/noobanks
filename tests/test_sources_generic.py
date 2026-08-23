"""Tests for CompositeAdapter URL discovery & download.

Uses aioresponses to mock HTTP calls for fast, deterministic tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientResponseError

from noobanks.config.models import BankSpec, SourceConfig
from noobanks.sources.base import FetchResult, Report
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
        assert result.report.url == "(cached)"

    @pytest.mark.asyncio
    async def test_force_re_downloads(self, tmp_data_dir: Path, mocker):
        # Use a bank with a non-existent IR URL to force discovery failure
        bank = _make_bank(ticker="BOGUS.XX", market="UK")
        adapter = CompositeAdapter(data_dir=tmp_data_dir, browser_fallback=False)
        target = adapter.target_path(tmp_data_dir, 2025, "BOGUS_XX", "annual_report", "FY")
        target.parent.mkdir(parents=True)
        target.write_bytes(b"%PDF-1.4\nold\n%%EOF")

        # Disable discovery for all adapters so fetch returns errors
        for a in adapter.adapters:
            mocker.patch.object(a, "discover_urls", return_value=[])

        # With force=True and a bogus URL, discovery will fail → errors
        result = await adapter.fetch(bank, "annual_report", 2025, force=True)
        # Should not return cached, and should have errors from failed discovery
        assert result.error is not None


class TestFetchNoUrls:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_urls_discovered(
        self, tmp_data_dir: Path, sample_bank_spec: BankSpec, mocker
    ):
        adapter = CompositeAdapter(data_dir=tmp_data_dir, browser_fallback=False)
        # The bank's IR URL doesn't exist → discovery will fail
        # We can make this deterministic by using a bank with a clearly bogus URL
        bank = _make_bank(ticker="BOGUS.TK", market="UK")
        # Disable discovery for all adapters so fetch returns errors
        for a in adapter.adapters:
            mocker.patch.object(a, "discover_urls", return_value=[])
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
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        mock_session = self._mock_session_get(
            mocker, 200,
            "<html><body><a href='/reports/'>Annual Reports</a></body></html>",
        )
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/ir")
        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_http_404_is_invalid(self, mocker):
        """HTTP 404 should be reported as invalid with a clear message."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        mock_session = self._mock_session_get(mocker, 404, "<html><body>Not Found</body></html>")
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/dead")
        assert result["valid"] is False
        assert "404" in result["error"]

    @pytest.mark.asyncio
    async def test_http_200_js_shell_is_invalid(self, mocker):
        """A page returning 200 but only a JS redirect shell (<500 bytes, no links)
        should be flagged as likely JS-rendered."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
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
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        mock_session = self._mock_session_get(mocker, 500, "Internal Server Error")
        result = await adapter._validate_ir_url(mock_session, "https://bank.example.com/broken")
        assert result["valid"] is False
        assert "500" in result["error"]

    @pytest.mark.asyncio
    async def test_network_error_is_invalid(self, mocker):
        """Network/connection errors should be caught and reported."""
        from noobanks.sources.ir_adapter import IrAdapter
        from aiohttp import ClientConnectionError

        adapter = IrAdapter()
        mock_session = mocker.MagicMock()
        mock_session.get.side_effect = ClientConnectionError("Connection refused")

        result = await adapter._validate_ir_url(mock_session, "https://nonexistent.example.com")
        assert result["valid"] is False
        assert "connection" in result["error"].lower()


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


class TestDdgsDiscoverUrls:
    """Tests for DdgsAdapter.discover_urls — ddgs-powered URL discovery."""

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
    async def test_discover_urls_returns_pdf_urls(self, mocker):
        """When DDGS returns results with PDF hrefs, they should be collected."""
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
        mocker.patch.object(
            adapter, "verify_url",
            return_value={"status": 200, "content_type": "application/pdf", "content_length": 500000},
        )

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert len(urls) >= 1
        assert any("2025-annual-report.pdf" in u for u in urls)

    @pytest.mark.asyncio
    async def test_discover_urls_returns_empty_on_no_results(self, mocker):
        """Empty search results should return an empty list."""
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

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert urls == []

    @pytest.mark.asyncio
    async def test_discover_urls_calls_fallback_when_primary_returns_empty(self, mocker):
        """When the first adapter returns no URLs, CompositeAdapter tries the next."""
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

        mocker.patch.object(adapter.adapters[0], "discover_urls", return_value=[])
        mocker.patch.object(
            adapter.adapters[1], "discover_urls",
            return_value=["https://cdn.example.com/report-2025.pdf"],
        )

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert len(urls) == 1
        assert "report-2025.pdf" in urls[0]

    @pytest.mark.asyncio
    async def test_discover_urls_skips_fallback_when_primary_succeeds(self, mocker):
        """When the first adapter finds URLs, later adapters are never called."""
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
            adapter.adapters[0], "discover_urls",
            return_value=["https://home.barclays/reports/2025-annual-report.pdf"],
        )
        fallback_spy = mocker.patch.object(adapter.adapters[1], "discover_urls", return_value=[])

        urls = await adapter.discover_urls(bank, "annual_report", 2025)
        assert len(urls) >= 1
        fallback_spy.assert_not_called()

    def test_report_type_labels_coverage(self):
        """All report types with patterns should have a search label."""
        for rt in REPORT_PATTERNS:
            assert rt in REPORT_TYPE_LABELS, f"Missing label for {rt}"


# ── Browser fallback (Playwright) ──────────────────────────────────────────


class TestBrowserFallback:
    """Tests for the headless-browser fallback — playwright-free.

    The real Playwright client is never imported: tests patch the
    `_render_page` / `_discover_via_browser` wrapper methods instead.
    """

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
    async def test_discover_urls_uses_browser_fallback_when_crawl_empty(self, mocker):
        """When the static crawl finds nothing, try the browser render."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        pdf = "https://www.icbc-ltd.com/en/page/2025-annual-report.pdf"
        adapter = IrAdapter()

        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})
        mocker.patch.object(adapter, "_find_pdf_links", return_value=[])
        browser_spy = mocker.patch.object(
            adapter, "_discover_via_browser", return_value=[(pdf, "2025 Annual Report")]
        )

        urls = await adapter.discover_urls(self._bank(ir_url), "annual_report", 2025)
        assert urls == [pdf]
        browser_spy.assert_called_once_with(ir_url, "annual_report", "2025")

    @pytest.mark.asyncio
    async def test_discover_urls_skips_browser_when_crawl_succeeds(self, mocker):
        """When the crawl finds PDFs, never touch the browser."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        crawled = [("https://home.barclays/reports/2025-annual-report.pdf", "Annual Report 2025")]
        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})
        mocker.patch.object(adapter, "_find_pdf_links", return_value=crawled)
        browser_spy = mocker.patch.object(adapter, "_discover_via_browser", return_value=[])

        urls = await adapter.discover_urls(
            self._bank("https://home.barclays/investor-relations"), "annual_report", 2025
        )
        assert urls == [url for url, _ in crawled]
        browser_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_browser_fallback_disabled_via_constructor(self, mocker):
        """browser_fallback=False bypasses the render step entirely."""
        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter(browser_fallback=False)
        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})
        mocker.patch.object(adapter, "_find_pdf_links", return_value=[])
        browser_spy = mocker.patch.object(adapter, "_discover_via_browser", return_value=[])

        urls = await adapter.discover_urls(
            self._bank("https://www.icbc-ltd.com/en/page/1220.html"), "annual_report", 2025
        )
        assert urls == []
        browser_spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_render_page_returns_none_when_playwright_missing(self, mocker):
        """Missing playwright degrades to None (no exception)."""
        import sys

        from noobanks.sources.ir_adapter import IrAdapter

        adapter = IrAdapter()
        mocker.patch.dict(
            sys.modules, {"playwright": None, "playwright.async_api": None}
        )
        assert await adapter._render_page("https://example.com") is None

    @pytest.mark.asyncio
    async def test_render_page_returns_none_on_render_failure(self, mocker):
        """Any render failure degrades to None (no exception)."""
        import sys
        import types

        from noobanks.sources.ir_adapter import IrAdapter

        async def boom():
            raise RuntimeError("no browser binary")

        stub = types.ModuleType("playwright.async_api")
        stub.async_playwright = boom
        mocker.patch.dict(
            sys.modules,
            {
                "playwright": types.ModuleType("playwright"),
                "playwright.async_api": stub,
            },
        )

        adapter = IrAdapter()
        assert await adapter._render_page("https://example.com") is None

    @pytest.mark.asyncio
    async def test_discover_via_browser_extracts_pdfs_from_rendered_landing(self, mocker):
        """PDF links in the rendered landing page are extracted directly."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        adapter = IrAdapter()
        render_spy = mocker.patch.object(
            adapter,
            "_render_page",
            return_value=_pdf_links_html(ir_url, "/en/page/2025-annual-report.pdf"),
        )

        urls = await adapter._discover_via_browser(
            ir_url, "annual_report", "2025"
        )
        assert urls == [
            ("https://www.icbc-ltd.com/en/page/2025-annual-report.pdf", "2025-annual-report.pdf")
        ]
        render_spy.assert_called_once_with(ir_url)

    @pytest.mark.asyncio
    async def test_discover_via_browser_follows_nav_links_when_no_direct_pdfs(self, mocker):
        """Without direct PDFs, render same-domain nav pages and extract there."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        nav_url = "https://www.icbc-ltd.com/en/page/financial-results/"
        adapter = IrAdapter()
        landing = (
            '<html><body><a href="/en/page/financial-results/">'
            "Financial Results</a></body></html>"
        )
        nav_page = _pdf_links_html(nav_url, "annual-report-2025.pdf")
        render_spy = mocker.patch.object(
            adapter, "_render_page", side_effect=[landing, nav_page]
        )

        urls = await adapter._discover_via_browser(
            ir_url, "annual_report", "2025"
        )
        assert urls == [(f"{nav_url}annual-report-2025.pdf", "annual-report-2025.pdf")]
        assert render_spy.call_count == 2

    @pytest.mark.asyncio
    async def test_discover_via_browser_bounds_nav_pages_and_stays_same_domain(self, mocker):
        """Nav rendering is capped and external domains are never rendered."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        adapter = IrAdapter(browser_max_pages=1)
        landing = (
            "<html><body>"
            '<a href="/en/page/annual-reports/">Annual Reports</a>'
            '<a href="https://other.example.com/annual-reports/">External</a>'
            '<a href="/en/page/financial-results/">Financial Results</a>'
            "</body></html>"
        )
        nav_page = _pdf_links_html(
            "https://www.icbc-ltd.com/en/page/annual-reports/", "annual-report-2025.pdf"
        )
        render_spy = mocker.patch.object(
            adapter, "_render_page", side_effect=[landing, nav_page]
        )

        urls = await adapter._discover_via_browser(
            ir_url, "annual_report", "2025"
        )
        assert render_spy.call_count == 2  # landing + first same-domain nav only
        for call in render_spy.call_args_list:
            assert "other.example.com" not in str(call.args[0])
        assert urls == [
            ("https://www.icbc-ltd.com/en/page/annual-reports/annual-report-2025.pdf", "annual-report-2025.pdf")
        ]

    @pytest.mark.asyncio
    async def test_discover_urls_ranks_annual_above_interim(self, mocker):
        """Rendered candidates: annual-report anchor text outranks interim."""
        from noobanks.sources.ir_adapter import IrAdapter

        ir_url = "https://www.icbc-ltd.com/en/page/1220.html"
        adapter = IrAdapter()
        interim_url = "https://www.icbc-ltd.com/en/page/2024InterimReport.pdf"
        annual_url = "https://www.icbc-ltd.com/en/page/2024AnnualReport.pdf"

        mocker.patch.object(adapter, "_validate_ir_url", return_value={"valid": True})
        mocker.patch.object(adapter, "_find_pdf_links", return_value=[])
        mocker.patch.object(
            adapter,
            "_discover_via_browser",
            return_value=[
                (interim_url, "2024 Interim Report"),
                (annual_url, "2024 Annual Report"),
            ],
        )

        urls = await adapter.discover_urls(self._bank(ir_url), "annual_report", 2024)
        assert urls[0] == annual_url
        assert urls[1] == interim_url


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
        assert adapter.adapters[0].browser_fallback is True
        assert adapter.adapters[0].browser_max_pages == 3

    def test_custom_values(self, tmp_path: Path):
        adapter = CompositeAdapter(
            data_dir=tmp_path,
            timeout=60,
            rate_limit_delay=1.0,
            max_concurrent=8,
            browser_fallback=False,
            browser_max_pages=1,
        )
        assert adapter.data_dir == tmp_path
        assert adapter.timeout == 60
        assert adapter.rate_limit_delay == 1.0
        assert adapter.adapters[0].browser_fallback is False
        assert adapter.adapters[0].browser_max_pages == 1

    def test_custom_adapters_override_default_chain(self):
        from noobanks.sources.base import SourceAdapter

        class DummyAdapter(SourceAdapter):
            async def discover_urls(self, bank, report_type, year, period="FY"):
                return []

        custom = [DummyAdapter(), DummyAdapter()]
        adapter = CompositeAdapter(adapters=custom)
        assert list(adapter.adapters) == custom
        assert len(adapter.adapters) == 2
        assert adapter.adapters[0] is custom[0]
        assert adapter.adapters[-1] is custom[-1]

    def test_empty_adapters_raises(self):
        with pytest.raises(ValueError):
            CompositeAdapter(adapters=[])