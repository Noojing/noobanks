"""Generic adapter for scraping bank investor-relations pages for PDF reports."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from noobanks.config.models import BankSpec
from noobanks.sources.base import FetchResult, Report, SourceAdapter

logger = logging.getLogger(__name__)

# Common report-type keywords for matching PDF links
REPORT_PATTERNS: dict[str, list[str]] = {
    "annual_report": [
        "annual.report", "annual_report", "annualreport",
        "annual-report", "full-year", "full_year", "fy-report",
    ],
    "interim_report": [
        "interim.report", "interim_report", "interim-report",
        "half-year", "half_year", "h1", "h2", "halfyear",
    ],
    "quarterly_report": [
        "quarterly.report", "quarterly_report", "quarterly-report",
        "q1", "q2", "q3", "q4", "interim",
    ],
    "10-K": ["10-k", "10k", "form-10-k", "form 10-k"],
    "10-Q": ["10-q", "10q", "form-10-q", "form 10-q"],
    "8-K": ["8-k", "8k", "form-8-k", "form 8-k"],
    "pillar3": ["pillar.3", "pillar-3", "pillar3", "pillar_3"],
}

# Sub-paths commonly found on IR landing pages
IR_SUBPATHS = [
    "reports-and-events",
    "annual-reports",
    "annual-report",
    "quarterly-results",
    "financial-results",
    "financial-reports",
    "results-and-reports",
    "results-centre",
    "results-center",
    "investor-relations/reports",
    "investors/reports",
    "about-us/investor-relations",
    "filings",
    "financial-information",
]

# Per-market URL construction patterns (fallback when scraping fails)
MARKET_HEURISTICS = {
    "UK": [
        "/content/dam/{domain_brand}/documents/investor-relations/{year}/{bank_name_short}-annual-report-{year}.pdf",
        "/content/dam/{domain_brand}/documents/investors/{year}/{bank_name_short}-annual-report-{year}.pdf",
        "/investors/results-and-reports/annual-report/{year}/download",
    ],
    "CN": [
        "/en/investor-relations/reports/{year}/annual-report.pdf",
        "/en/investor/reports/{year}/annual-report.pdf",
        "/investor-relations/reports/{year}/annual-report.pdf",
    ],
    "HK": [
        "/investor-relations/reports/{year}/annual-report.pdf",
        "/en/investor-relations/reports/{year}/annual-report.pdf",
        "/en/ir/reports/{year}/annual-report.pdf",
    ],
    "US": [
        "/investor-relations/annual-reports/{year}/annual-report.pdf",
        "/about-us/investor-relations/annual-reports/{year}/annual-report.pdf",
    ],
}


class GenericIrAdapter(SourceAdapter):
    """Scrapes bank investor-relations websites to discover and download PDF reports.

    Ports the URL discovery heuristics from the report-fetcher agent into Python:
    1. Scrape the IR landing page → extract all PDF links matching report type + year
    2. Follow common IR sub-paths and scrape those too
    3. Construct candidate URLs from per-market patterns as fallback
    4. HEAD-verify candidates before download
    """

    def __init__(
        self,
        data_dir: str | Path = "src/data",
        *,
        timeout: int = 30,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._domain_timers: dict[str, float] = {}

    # ── public API ────────────────────────────────────────────────────────

    async def fetch(
        self,
        bank: BankSpec,
        report_type: str,
        year: int,
        period: str = "FY",
        *,
        force: bool = False,
    ) -> FetchResult:
        """Fetch a report for one bank. Returns FetchResult."""
        result = FetchResult(bank=bank)
        target = self.target_path(
            self.data_dir, year, bank.ticker_safe, report_type, period
        )

        if not force and target.exists():
            logger.info("Already downloaded, skipping: %s", target.name)
            result.reports.append(
                Report(
                    bank_ticker=bank.ticker,
                    report_type=report_type,
                    year=year,
                    period=period,
                    local_path=target,
                    url="(cached)",
                    file_size=target.stat().st_size,
                )
            )
            return result

        urls = await self.discover_urls(bank, report_type, year)
        if not urls:
            result.errors.append(
                f"No PDF URLs found for {bank.ticker} {report_type} {year}"
            )
            return result

        for url in urls:
            verified = await self.verify_url(url)
            if verified is None:
                continue

            try:
                report = await self._download(url, target, bank, report_type, year, period)
                result.reports.append(report)
                return result  # Success — stop after first successful download
            except Exception as exc:
                logger.warning("Download failed for %s: %s", url, exc)
                result.errors.append(f"{url}: {exc}")

        if not result.reports:
            result.errors.append(
                f"All {len(urls)} candidate URLs failed for {bank.ticker}"
            )
        return result

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int
    ) -> list[str]:
        """Discover PDF URLs using layered heuristics.

        Strategy (zero-token where possible):
        1. Scrape IR landing page for matching PDF links
        2. Follow common IR sub-paths and scrape those
        3. Fall back to per-market URL construction patterns
        """
        ir_base = bank.sources.investor_relations.rstrip("/")
        candidates: set[str] = set()
        year_short = str(year % 100)
        year_str = str(year)

        async with self._get_session() as session:
            # 1. Scrape the IR landing page
            page_links = await self._scrape_page_for_pdfs(
                session, ir_base, report_type, year_str, year_short
            )
            candidates.update(page_links)

            # 2. Follow common sub-paths
            subpath_links = await self._scrape_subpaths(
                session, ir_base, report_type, year_str, year_short
            )
            candidates.update(subpath_links)

        # 3. Deduplicate and sort (prefer PDFs with year in name)
        candidates_list = sorted(candidates, key=lambda u: self._url_score(u, year_str), reverse=True)

        if not candidates_list:
            # 4. Fallback: try constructed URLs from per-market patterns
            candidates_list = self._construct_candidates(bank, year_str, year_short)

        logger.info(
            "Discovered %d candidate URLs for %s %s %s",
            len(candidates_list), bank.ticker, report_type, year,
        )
        return candidates_list

    async def verify_url(self, url: str) -> Optional[dict]:
        """HEAD-check a URL to verify it's a valid PDF.

        Returns dict with status, content_type, content_length if valid.
        Returns None for non-PDF or error responses.
        """
        async with self._get_session() as session:
            try:
                async with session.head(
                    url,
                    timeout=aiohttp.ClientTimeout(total=15),
                    allow_redirects=True,
                    max_redirects=5,
                ) as resp:
                    content_type = resp.headers.get("Content-Type", "")
                    content_length = resp.headers.get("Content-Length")
                    size = int(content_length) if content_length else 0

                    if resp.status != 200:
                        logger.debug("HEAD %s → HTTP %d", url, resp.status)
                        return None

                    if "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
                        # Some servers don't set proper Content-Type; check extension
                        if not url.lower().endswith(".pdf"):
                            logger.debug("HEAD %s → not PDF (%s)", url, content_type)
                            return None

                    if size > 0 and size < 50_000:
                        logger.debug("HEAD %s → too small (%d bytes)", url, size)
                        return None

                    return {
                        "status": resp.status,
                        "content_type": content_type,
                        "content_length": size,
                    }
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug("HEAD %s → error: %s", url, exc)
                return None

    async def _validate_ir_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict[str, Any]:
        """Check whether an IR URL is reachable and contains real content.

        Catches three failure modes:
        1. HTTP errors (404, 500, etc.)
        2. JS-only shells (tiny HTML with a redirect script, no real links)
        3. Network/connection errors

        Returns:
            {"valid": True} if the URL looks usable for scraping.
            {"valid": False, "error": "<reason>"} otherwise.
        """
        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=3
            ) as resp:
                if resp.status != 200:
                    return {
                        "valid": False,
                        "error": f"IR URL returned HTTP {resp.status}: {url}",
                    }

                html = await resp.text()
                html_size = len(html)

                soup = BeautifulSoup(html, "lxml")
                link_count = len(soup.find_all("a", href=True))

                if html_size < 1000 and link_count == 0:
                    return {
                        "valid": False,
                        "error": (
                            f"IR URL appears to be a JS-rendered shell "
                            f"({html_size} bytes, {link_count} links): {url}"
                        ),
                    }

                logger.debug(
                    "IR URL validated: %s (%d bytes, %d links)",
                    url, html_size, link_count,
                )
                return {"valid": True}

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {
                "valid": False,
                "error": f"IR URL connection failed: {url} — {exc}",
            }

    # ── private helpers ────────────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        """Create a new session with our headers. Caller must close it."""
        return aiohttp.ClientSession(
            headers={"User-Agent": self.user_agent},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    async def _rate_limit(self, domain: str) -> None:
        """Enforce per-domain rate limiting."""
        now = time.monotonic()
        last = self._domain_timers.get(domain, 0)
        wait = self.rate_limit_delay - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._domain_timers[domain] = time.monotonic()

    async def _scrape_page_for_pdfs(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        report_type: str,
        year_str: str,
        year_short: str,
    ) -> list[str]:
        """Scrape a single page for PDF links matching report type + year."""
        domain = urlparse(base_url).netloc
        await self._rate_limit(domain)

        try:
            async with session.get(base_url, allow_redirects=True, max_redirects=3) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("Failed to fetch %s: %s", base_url, exc)
            return []

        return self._extract_pdf_links(html, base_url, report_type, year_str, year_short)

    async def _scrape_subpaths(
        self,
        session: aiohttp.ClientSession,
        ir_base: str,
        report_type: str,
        year_str: str,
        year_short: str,
    ) -> list[str]:
        """Try each common IR sub-path and scrape for PDFs."""
        all_links: list[str] = []
        domain = urlparse(ir_base).netloc

        for subpath in IR_SUBPATHS:
            candidate_url = f"{ir_base}/{subpath}"
            await self._rate_limit(domain)
            try:
                async with session.get(
                    candidate_url, allow_redirects=True, max_redirects=3
                ) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue

            links = self._extract_pdf_links(
                html, candidate_url, report_type, year_str, year_short
            )
            all_links.extend(links)

        return all_links

    def _extract_pdf_links(
        self,
        html: str,
        base_url: str,
        report_type: str,
        year_str: str,
        year_short: str,
    ) -> list[str]:
        """Parse HTML and extract PDF hrefs matching the report type + year.

        Matching is done against three signals (any one is sufficient):
        1. href contains year + report-type keywords
        2. <a> tag text (link text) contains year + report-type keywords
        3. URL path segment contains the target year or publication year (year+1)
           — needed for ABC where filenames are opaque (P02026042…pdf) but
           paths encode the publication date (/202603/ for FY2025).
        """
        patterns = REPORT_PATTERNS.get(report_type, [report_type.lower()])
        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []
        year_next = str(int(year_str) + 1)

        def _year_in_path(href: str) -> bool:
            """Check if year or year+1 appears in URL path segments."""
            return f"/{year_str}" in href or f"/{year_next}" in href

        def _year_in_text(text: str) -> bool:
            """Check if year (full or short) appears in text."""
            t = text.lower()
            return year_str in t or year_short in t

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            href_lower = href.lower()

            # Must be a PDF
            if not href_lower.endswith(".pdf"):
                continue

            # Must match one of the report-type patterns
            type_match = any(p in href_lower for p in patterns)

            # Must reference the target year (href, path, or link text)
            year_in_href = year_str in href or year_short in href
            year_in_path = _year_in_path(href)
            year_in_text = _year_in_text(a_tag.get_text(strip=True))

            if year_in_href or year_in_path or year_in_text:
                full_url = urljoin(base_url, href)
                if full_url not in links:
                    links.append(full_url)

        # Relaxed fallback: any PDF with year (via href, path, or text)
        if not links:
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                href_lower = href.lower()
                if not href_lower.endswith(".pdf"):
                    continue
                if (
                    year_str in href
                    or year_short in href
                    or _year_in_path(href)
                    or _year_in_text(a_tag.get_text(strip=True))
                ):
                    full_url = urljoin(base_url, href)
                    if full_url not in links:
                        links.append(full_url)

        return links

    def _construct_candidates(
        self, bank: BankSpec, year_str: str, year_short: str
    ) -> list[str]:
        """Build candidate URLs from per-market heuristics (last-resort fallback)."""
        domain = bank.domain
        domain_brand = domain.split(".")[-2] if domain else ""
        name_short = bank.name.split()[0].lower()

        # Use market-specific templates
        templates = MARKET_HEURISTICS.get(
            bank.market, MARKET_HEURISTICS["US"]
        )

        candidates: list[str] = []
        ir_base = bank.sources.investor_relations.rstrip("/")

        for template in templates:
            url_path = template.format(
                domain_brand=domain_brand,
                bank_name_short=name_short,
                year=year_str,
            )
            if not url_path.startswith("http"):
                candidates.append(f"{ir_base}{url_path}")
            else:
                candidates.append(url_path)

        # Also try the IR base + common patterns
        for suffix in [
            f"/{year_str}/annual-report-{year_str}.pdf",
            f"/reports/{year_str}/annual-report-{year_str}.pdf",
            f"/annual-report-{year_str}.pdf",
            f"/Annual-Report-{year_str}.pdf",
            f"/{name_short}-annual-report-{year_str}.pdf",
        ]:
            candidates.append(f"{ir_base}{suffix}")

        return candidates

    def _url_score(self, url: str, year_str: str) -> int:
        """Score a URL for relevance (higher = better match)."""
        score = 0
        url_lower = url.lower()
        if year_str in url:
            score += 3
        if "annual" in url_lower:
            score += 2
        if "report" in url_lower:
            score += 1
        # Prefer official domains over CDNs
        if "cdn" not in url_lower and "static" not in url_lower:
            score += 1
        return score

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def _download(
        self,
        url: str,
        target: Path,
        bank: BankSpec,
        report_type: str,
        year: int,
        period: str,
    ) -> Report:
        """Download a PDF to the target path. Retries on network errors."""
        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        target.parent.mkdir(parents=True, exist_ok=True)

        async with self._semaphore:
            async with self._get_session() as session:
                async with session.get(
                    url, allow_redirects=True, max_redirects=5
                ) as resp:
                    if resp.status != 200:
                        raise aiohttp.ClientResponseError(
                            request_info=resp.request_info,
                            history=resp.history,
                            status=resp.status,
                            message=f"HTTP {resp.status}",
                            headers=resp.headers,
                        )

                    content = await resp.read()

        # Validate PDF magic bytes
        if len(content) < 4 or content[:4] != b"%PDF":
            raise ValueError(f"Downloaded file is not a valid PDF (missing %%PDF header): {url}")

        target.write_bytes(content)
        file_size = len(content)
        content_hash = self._compute_hash(target)

        logger.info(
            "Downloaded %s (%s) → %s (%.1f MB)",
            bank.ticker, report_type, target.name, file_size / (1024 * 1024),
        )

        return Report(
            bank_ticker=bank.ticker,
            report_type=report_type,
            year=year,
            period=period,
            local_path=target,
            url=url,
            file_size=file_size,
            content_hash=content_hash,
        )
