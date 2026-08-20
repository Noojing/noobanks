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
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

# Common report-type keywords for matching PDF links
REPORT_PATTERNS: dict[str, list[str]] = {
    "annual_report": [
        "annual.report", "annual_report", "annualreport",
        "annual-report", "full-year", "full_year", "fy-report",
        "年报", "年度报告",
    ],
    "interim_report": [
        "interim.report", "interim_report", "interim-report",
        "half-year", "half_year", "h1", "h2", "halfyear",
        "中期报告", "半年报", "半年度报告",
    ],
    "quarterly_report": [
        "quarterly.report", "quarterly_report", "quarterly-report",
        "q1", "q2", "q3", "q4", "interim",
        "季度报告", "季报", "一季度", "二季度", "三季度", "四季度",
        "第1季度", "第2季度", "第3季度", "第4季度",
    ],
    "10-K": ["10-k", "10k", "form-10-k", "form 10-k"],
    "10-Q": ["10-q", "10q", "form-10-q", "form 10-q"],
    "8-K": ["8-k", "8k", "form-8-k", "form 8-k"],
    "6-K": ["6-k", "6k", "form-6-k", "form 6-k"],
    "pillar3": ["pillar.3", "pillar-3", "pillar3", "pillar_3", "第三支柱"],
}

# Human-readable labels for search-fallback DuckDuckGo queries.
# Maps report_type keys to phrases used in web search queries.
REPORT_TYPE_LABELS: dict[str, str] = {
    "annual_report": "annual report 年报 年度报告",
    "10-K": "10-K annual report 10-K 年报",
    "10-Q": "10-Q quarterly report 10-Q 季度报告",
    "8-K": "8-K current report 8-K 当期报告",
    "6-K": "6-K current report 6-K 当期报告",
    "interim_report": "interim report 中期报告 半年报",
    "quarterly_report": "quarterly report 季度报告 季报",
    "pillar3": "pillar 3 disclosures 第三支柱",
}

# Report-type keywords for candidate scoring. Used by _url_score to reward
# URLs/link texts that match the requested report type and penalize those
# that match a different type (e.g. "interim" files when fetching an annual
# report). Deliberately curated: bare "h1"/"q1" etc. are excluded because
# short substrings produce too many false positives.
REPORT_TYPE_SCORE_KEYWORDS: dict[str, list[str]] = {
    "annual_report": [
        "annual report", "annual-report", "annual_report", "annualreport",
        "full-year", "full_year",
        "年报", "年度报告",
    ],
    "interim_report": [
        "interim report", "interim-report", "interim_report", "interimreport",
        "interim results", "interim-results", "interim_results", "interimresults",
        "half-year", "half_year", "half year", "half-year results",
        "中期报告", "半年报", "半年度报告",
    ],
    "quarterly_report": [
        "quarterly report", "quarterly-report", "quarterly_report", "quarterlyreport",
        "季度报告", "季报",
    ],
    "10-K": ["10-k", "10k"],
    "10-Q": ["10-q", "10q"],
    "8-K": ["8-k", "8k"],
    "6-K": ["6-k", "6k"],
    "pillar3": ["pillar 3", "pillar-3", "pillar3", "pillar_3", "第三支柱"],
}

# Period keywords for candidate scoring. Used by _url_score to reward
# candidates whose URL or link text matches the target period.
PERIOD_SCORE_KEYWORDS: dict[str, list[str]] = {
    "FY": ["fy", "full year", "full-year", "annual", "yearly", "年报", "年度报告"],
    "Q1": ["q1", "quarter 1", "1st quarter", "first quarter", "一季度", "第1季度"],
    "Q2": ["q2", "quarter 2", "2nd quarter", "second quarter", "二季度", "第2季度"],
    "Q3": ["q3", "quarter 3", "3rd quarter", "third quarter", "三季度", "第3季度"],
    "Q4": ["q4", "quarter 4", "4th quarter", "fourth quarter", "四季度", "第4季度"],
    "H1": ["h1", "half-year 1", "first half", "上半年", "半年报", "中期报告"],
    "H2": ["h2", "half-year 2", "second half", "下半年"],
}

# Filenames that signal a non-report document (results announcements,
# circulars, notices, Q&A records, briefing slides). Penalized in
# _url_score's text-aware branch — these commonly co-exist with real
# reports on IR listing pages (e.g. ICBC's opaque CamelCase filenames).
# Anchor-text positives (+5/+6) outvote these URL-level penalties, so a
# link whose text says "Annual Report" is unaffected.
NON_REPORT_SCORE_KEYWORDS: list[str] = [
    "announcement", "circular", "notice", "briefing", "qarecord",
    "annual results", "annual-results", "annual_results", "annualresults",
    "公告", "通告", "通知", "通函", "新闻稿", "会议纪要", "路演", "问答",
    "简讯", "简报",
]

# Keywords for detecting report-related navigation links in HTML.
# Matched against <a> tag text OR href. Links matching these keywords
# are followed during recursive crawling to find report download pages.
NAV_KEYWORDS: list[str] = [
    "annual-report", "annual_reports", "annual report",
    "interim-report", "interim_reports", "interim report",
    "quarterly-report", "quarterly_reports", "quarterly report",
    "financial-report", "financial_reports", "financial report",
    "financial-results", "financial_results", "financial results",
    "performance-report", "performance_reports", "performance report",
    "results-and-reports", "results-and-announcements",
    "reports-and-events", "reports-and-presentations",
    "earnings", "filings", "sec-filings",
    "regulatory-news", "regulatory_filings",
    "pillar-3", "pillar_3", "pillar3",
    # Chinese report-related navigation keywords
    "年报", "年度报告", "年報", "年度報告",
    "中期报告", "中期報告", "半年报", "半年報",
    "季度报告", "季度報告", "季报", "季報",
    "财务报告", "財務報告", "财务报表", "財務報表",
    "业绩报告", "業績報告", "业绩公告", "業績公告",
    "投资者关系", "投資者關係", "投资者", "投資者",
    "信息披露", "信息揭露", "定期报告", "定期報告",
    "公告", "报告", "報告",
]

# Navigation-link text that should be excluded (too generic / not report-related)
NAV_EXCLUDE_TEXT: set[str] = {
    "home", "about", "about us", "contact", "contact us",
    "careers", "news", "media", "press", "search", "login",
    "share price", "stock", "corporate governance", "sustainability",
    "csr", "esg", "cookie", "privacy", "terms", "accessibility",
    "sitemap", "rss", "email alerts", "subscribe",
    # Chinese generic navigation text to exclude
    "首页", "关于", "关于我们", "关于我們",
    "联系我们", "聯繫我們", "加入我们", "加入我們",
    "招聘", "人才招聘", "职位", "職位",
    "搜索", "搜尋", "登录", "登錄", "注册", "註冊",
    "股价", "股價", "股票", "公司治理", "公司管治",
    "可持续发展", "可持續發展", "社会责任", "社會責任",
    "隐私", "隱私", "条款", "條款", "无障碍", "無障礙",
    "网站地图", "網站地圖", "邮件提醒", "郵件提醒", "订阅", "訂閱",
}

class GenericIrAdapter(SourceAdapter):
    """Scrapes bank investor-relations websites to discover and download PDF reports.

    Ports the URL discovery heuristics from the report-fetcher agent into Python:
    1. Scrape the IR landing page → extract all PDF links matching report type + year
    2. Follow common IR sub-paths and scrape those too
    3. If the static crawl finds nothing, render the IR page in a headless
       browser (Playwright) to capture JS-rendered content
    4. Construct candidate URLs from per-market patterns as fallback
    5. HEAD-verify candidates before download
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        ),
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
        browser_fallback: bool = True,
        browser_max_pages: int = 3,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self.browser_fallback = browser_fallback
        self.browser_max_pages = browser_max_pages
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

        urls = await self.discover_urls(bank, report_type, year, period)
        if not urls:
            result.errors.append(
                f"No PDF URLs found for {bank.ticker} {report_type} {year}"
            )
            return result

        tried: set[str] = set()
        for url in urls:
            tried.add(url)
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

        # All primary URLs failed — try search fallback as last resort
        year_str = str(year)
        search_urls = await self._search_fallback(bank, report_type, year_str)
        for url in search_urls:
            if url in tried:
                continue
            verified = await self.verify_url(url)
            if verified is None:
                continue
            try:
                report = await self._download(url, target, bank, report_type, year, period)
                result.reports.append(report)
                logger.info("Search fallback succeeded: %s", url)
                return result
            except Exception as exc:
                logger.warning("Search fallback download failed for %s: %s", url, exc)

        if not result.reports:
            result.errors.append(
                f"All {len(urls)} candidate URLs failed for {bank.ticker}"
            )
        return result

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        """Discover PDF URLs by crawling from the IR landing page.

        Strategy:
        1. Validate the IR URL is reachable (surface dead config early)
        2. BFS crawl from the IR landing page, following report-related
           navigation links to find pages with PDF downloads
        3. Fall back to per-market URL construction heuristics if crawl fails

        Scoring considers year, report type, and period (Q1/Q2/Q3/Q4/H1/H2)
        to rank candidates by relevance.
        """
        ir_base = bank.sources.investor_relations.rstrip("/")
        candidates: dict[str, str] = {}  # url -> link_text
        year_short = str(year % 100)
        year_str = str(year)

        async with self._get_session() as session:
            # 1. Validate IR URL before crawling
            validation = await self._validate_ir_url(session, ir_base)
            if not validation["valid"]:
                logger.warning(
                    "IR URL invalid for %s: %s", bank.ticker, validation["error"]
                )

            # 2. Crawl from the IR landing page (even if validation warns —
            #    a "probably JS" page might still have static links we can find)
            crawl_links = await self._crawl_for_report_pages(
                session, ir_base, report_type, year_str, year_short
            )
            for url, text in crawl_links:
                if url not in candidates:
                    candidates[url] = text

        # 3. Deduplicate and sort (prefer PDFs with year in name,
        #    boosted by link text, report-type, and period relevance)
        candidates_list = sorted(
            candidates,
            key=lambda u: self._url_score(
                u, year_str, link_text=candidates[u],
                report_type=report_type, period=period,
            ),
            reverse=True,
        )

        if not candidates_list and self.browser_fallback:
            # 4. Fallback: render the IR page in a headless browser to
            #    capture JS-rendered content (e.g. ICBC-style AJAX shells)
            logger.info(
                "Crawl found no PDFs for %s %s %s; trying headless-browser render",
                bank.ticker, report_type, year,
            )
            browser_links = await self._discover_via_browser(
                ir_base, report_type, year_str, year_short
            )
            candidates_list = [
                url
                for url, _ in sorted(
                    browser_links,
                    key=lambda ut: self._url_score(
                        ut[0], year_str, link_text=ut[1],
                        report_type=report_type, period=period,
                    ),
                    reverse=True,
                )
            ]

        if not candidates_list:
            # 5. Fallback: try constructed URLs from common patterns
            logger.info(
                "Crawl found no PDFs for %s %s %s; trying URL construction",
                bank.ticker, report_type, year,
            )
            candidates_list = self._construct_candidates(bank, year_str, year_short)

        if not candidates_list:
            # 6. Last resort: DuckDuckGo web search fallback
            logger.info(
                "Crawl and construction both failed for %s %s %s; trying web search",
                bank.ticker, report_type, year,
            )
            search_results = await self._search_fallback(
                bank, report_type, year_str
            )
            candidates_list = sorted(
                search_results,
                key=lambda u: self._url_score(u, year_str, period=period),
                reverse=True,
            )

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

                html = await resp.text(errors="replace")
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
                html = await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("Failed to fetch %s: %s", base_url, exc)
            return []

        return self._extract_pdf_links(html, base_url, report_type, year_str, year_short)

    def _extract_pdf_links(
        self,
        html: str,
        base_url: str,
        report_type: str,
        year_str: str,
        year_short: str,
        *,
        with_text: bool = False,
    ) -> list[str] | list[tuple[str, str]]:
        """Parse HTML and extract PDF hrefs matching the report type + year.

        Matching is done against three signals (any one is sufficient):
        1. href contains year + report-type keywords
        2. <a> tag text (link text) contains year + report-type keywords
        3. URL path segment contains the target year or publication year (year+1)
           — needed for ABC where filenames are opaque (P02026042…pdf) but
           paths encode the publication date (/202603/ for FY2025).

        Args:
            with_text: When True, return (url, anchor_text) pairs so callers
                can score candidates with the link text (see _url_score).
                Defaults to plain URL strings.
        """
        patterns = REPORT_PATTERNS.get(report_type, [report_type.lower()])
        soup = BeautifulSoup(html, "lxml")
        links: list[str] | list[tuple[str, str]] = []
        seen: set[str] = set()

        def _append(full_url: str, a_tag) -> None:
            if full_url in seen:
                return
            seen.add(full_url)
            if with_text:
                links.append((full_url, a_tag.get_text(strip=True)))  # type: ignore[arg-type]
            else:
                links.append(full_url)  # type: ignore[arg-type]

        def _year_in_path(href: str) -> bool:
            """Check if the target fiscal year appears in URL path segments."""
            return f"/{year_str}" in href

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
                _append(urljoin(base_url, href), a_tag)

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
                    _append(urljoin(base_url, href), a_tag)

        return links

    def _extract_nav_links(self, html: str, base_url: str) -> list[str]:
        """Extract report-related navigation links from an IR page.

        Finds <a> tags whose text or href contains report-related keywords
        (e.g. 'Annual Reports', 'Financial Results', 'Performance Reports').
        These are URLs to follow during recursive crawling — not direct PDF links.

        Excludes:
        - Direct PDF links (handled separately by _extract_pdf_links)
        - Generic navigation (Home, Contact, About, Careers, etc.)
        - Already-seen URLs (dedup)

        Args:
            html: Raw HTML of the page.
            base_url: Base URL for resolving relative links.

        Returns:
            List of full URLs to follow, ordered with most report-relevant first.
        """
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        links: list[str] = []

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"].strip()
            href_lower = href.lower()

            # Skip PDFs, javascript, mailto, anchors
            if href_lower.endswith(".pdf"):
                continue
            if href.startswith(("javascript:", "mailto:", "#")):
                continue

            link_text = a_tag.get_text(strip=True).lower()

            # Skip generic navigation links
            if link_text in NAV_EXCLUDE_TEXT:
                continue
            # Check if either href or link text matches a report keyword,
            # or if the link text is a year selector (e.g. "2024", "2025年")
            # — these are valid year-specific report page links on IR sites
            text_or_href = f"{href_lower} {link_text}"
            matches = any(kw in text_or_href for kw in NAV_KEYWORDS)
            is_year_link = bool(
                link_text
                and link_text.replace("年", "").replace(" ", "").strip().isdigit()
                and len(link_text.replace("年", "").replace(" ", "").strip()) == 4
            )
            if not matches and not is_year_link:
                continue

            if matches:
                full_url = urljoin(base_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    links.append(full_url)

        return links

    async def _crawl_for_report_pages(
        self,
        session: aiohttp.ClientSession,
        ir_base: str,
        report_type: str,
        year_str: str,
        year_short: str,
        max_depth: int = 2,
    ) -> list[tuple[str, str]]:
        """BFS crawl from IR landing page to discover report PDF URLs.

        Starting from ir_base, fetches each page, extracts:
        1. Direct PDF links (via _extract_pdf_links)
        2. Navigation links (via _extract_nav_links) to follow next

        Crawling rules:
        - Same domain only: won't follow links to external sites
        - Max depth: stops after following links N levels deep
        - Cycle prevention: tracks visited URLs in a set
        - Rate limiting: respects per-domain delay between requests

        Args:
            session: aiohttp session for HTTP requests.
            ir_base: Starting URL (the bank's IR landing page).
            report_type: Target report type key (annual_report, 10-K, etc.).
            year_str: 4-digit year as string ("2025").
            year_short: 2-digit year as string ("25").
            max_depth: How many levels of links to follow. 0 = only ir_base.

        Returns:
            List of (PDF URL, anchor text) tuples discovered during the crawl.
        """
        domain = urlparse(ir_base).netloc
        visited: set[str] = set()
        all_pdf_links: list[tuple[str, str]] = []
        seen_urls: set[str] = set()

        from collections import deque
        queue: deque[tuple[str, int]] = deque([(ir_base, 0)])

        while queue:
            url, depth = queue.popleft()

            if url in visited:
                continue
            visited.add(url)

            # Fetch the page
            await self._rate_limit(domain)
            try:
                async with session.get(
                    url, allow_redirects=True, max_redirects=3
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Crawl skip %s → HTTP %d", url, resp.status)
                        continue
                    html = await resp.text(errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug("Crawl skip %s → %s", url, exc)
                continue

            # Extract PDF links from this page
            pdf_links = self._extract_pdf_links(
                html, url, report_type, year_str, year_short,
                with_text=True,
            )
            for link, text in pdf_links:  # type: ignore[misc]
                if link not in seen_urls:
                    seen_urls.add(link)
                    all_pdf_links.append((link, text))

            # If we found PDFs at this depth, don't crawl deeper
            # (we already have results from the right page)
            if pdf_links and depth > 0:
                continue

            # Follow navigation links if we haven't hit max depth
            if depth < max_depth:
                nav_links = self._extract_nav_links(html, url)
                for nav_url in nav_links:
                    nav_domain = urlparse(nav_url).netloc
                    # Stay on the same domain
                    if nav_domain == domain and nav_url not in visited:
                        queue.append((nav_url, depth + 1))

        logger.debug(
            "Crawl from %s: visited %d pages, found %d PDFs (max depth %d)",
            ir_base, len(visited), len(all_pdf_links), max_depth,
        )
        return all_pdf_links

    async def _render_page(
        self, url: str, timeout: Optional[int] = None
    ) -> Optional[str]:
        """Render a page in a headless browser and return the post-JS HTML.

        Used as a fallback when the static crawl finds no PDF links — the
        raw HTML of JS-rendered IR pages (e.g. ICBC) is an AJAX shell whose
        report links only exist after JavaScript runs in a real browser.

        Playwright is a project dependency (installed by `uv sync`), but
        the browser binaries need a one-time
        `uv run playwright install chromium`. Every failure mode —
        missing package, missing browser binary, timeouts, anti-bot blocks —
        degrades to a warning + None so the caller falls through to the
        next discovery strategy.

        Args:
            url: Page to render.
            timeout: Navigation timeout in seconds (defaults to self.timeout).

        Returns:
            Rendered HTML as a string, or None if rendering failed.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "Playwright not installed; run 'uv sync' to install project "
                "dependencies and 'uv run playwright install chromium' for the "
                "browser binaries"
            )
            return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(user_agent=self.user_agent)
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=(timeout or self.timeout) * 1000,  # playwright uses ms
                    )
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("Browser render failed for %s: %s", url, exc)
            return None

    async def _discover_via_browser(
        self,
        ir_base: str,
        report_type: str,
        year_str: str,
        year_short: str,
        max_pages: Optional[int] = None,
    ) -> list[tuple[str, str]]:
        """Discover PDF links by rendering the IR page in a headless browser.

        Renders ir_base (running any JavaScript), extracts PDF links from
        the rendered DOM via _extract_pdf_links. If the landing page has no
        PDFs, follows up to browser_max_pages same-domain navigation links
        from the rendered DOM and extracts PDFs from those pages too.

        Args:
            ir_base: The bank's IR landing page URL.
            report_type: Report type key (annual_report, 10-K, ...).
            year_str: 4-digit year as string ("2025").
            year_short: 2-digit year as string ("25").
            max_pages: Max nav pages to render (defaults to
                self.browser_max_pages).

        Returns:
            (url, anchor_text) pairs discovered in rendered pages — anchor
            text lets callers score candidates with _url_score.
        """
        domain = urlparse(ir_base).netloc
        await self._rate_limit(domain)

        html = await self._render_page(ir_base)
        if html is None:
            return []

        pdfs = self._extract_pdf_links(
            html, ir_base, report_type, year_str, year_short, with_text=True
        )
        if pdfs:
            return pdfs

        # No direct PDFs on the landing page — follow report-related
        # navigation links found in the rendered DOM.
        nav_links = self._extract_nav_links(html, ir_base)
        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        pages_rendered = 0
        for nav_url in nav_links:
            if urlparse(nav_url).netloc != domain:
                continue  # stay on the same domain (mirrors the crawl)
            if pages_rendered >= (max_pages or self.browser_max_pages):
                break
            pages_rendered += 1

            await self._rate_limit(domain)
            nav_html = await self._render_page(nav_url)
            if nav_html is None:
                continue
            for link, text in self._extract_pdf_links(
                nav_html, nav_url, report_type, year_str, year_short,
                with_text=True,
            ):
                if link not in seen:
                    seen.add(link)
                    results.append((link, text))

        return results

    def _construct_candidates(
        self, bank: BankSpec, year_str: str, year_short: str
    ) -> list[str]:
        """Build candidate URLs from common patterns (last-resort fallback).

        Used only when the IR crawl finds no PDFs. Constructs plausible
        URLs from the IR base + common annual-report filename patterns.
        """
        name_short = bank.name.split()[0].lower()
        candidates: list[str] = []
        ir_base = bank.sources.investor_relations.rstrip("/")

        # Strip page filename if ir_base ends in .html / .aspx / .shtml etc.
        # ICBC: ".../page/1220435982957096960.html" → ".../page/"
        parsed = urlparse(ir_base)
        path = parsed.path
        last_seg = path.rsplit("/", 1)[-1]
        if "." in last_seg and not last_seg.startswith("."):
            dir_path = path.rsplit("/", 1)[0] + "/"
            ir_base = f"{parsed.scheme}://{parsed.netloc}{dir_path}".rstrip("/")

        for suffix in [
            f"/{year_str}/annual-report-{year_str}.pdf",
            f"/reports/{year_str}/annual-report-{year_str}.pdf",
            f"/annual-report-{year_str}.pdf",
            f"/Annual-Report-{year_str}.pdf",
            f"/{name_short}-annual-report-{year_str}.pdf",
            f"/{year_str}/annual_report_{year_str}.pdf",
            f"/reports-and-events/annual-reports/{year_str}/annual-report-{year_str}.pdf",
        ]:
            candidates.append(f"{ir_base}{suffix}")

        return candidates

    def _run_ddg_search(self, query: str, max_results: int) -> list[dict]:
        """Synchronous DDGS search callable via asyncio.to_thread."""
        from ddgs import DDGS

        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    async def _search_fallback(
        self,
        bank: BankSpec,
        report_type: str,
        year_str: str,
        max_results: int = 10,
    ) -> list[str]:
        """Use DuckDuckGo search to find report PDF URLs as a last-resort fallback.

        Triggered when both the IR crawl and constructed URL candidates fail.
        Searches for: "<Bank Name> <Year> <report type label> financial report PDF"

        Strategy:
        1. Run a DDG text search with the bank name + year + report type
        2. Collect URLs from search results that end in .pdf (direct PDF links)
        3. For non-PDF result pages, scrape them for embedded PDF links
        4. Deduplicate and return all candidate PDF URLs

        Args:
            bank: Bank specification from config.
            report_type: Type key (annual_report, 10-K, etc.).
            year_str: 4-digit year as string ("2025").
            max_results: Max DDG search results to examine.

        Returns:
            List of candidate PDF URLs discovered via search.
        """
        label = REPORT_TYPE_LABELS.get(report_type, report_type.replace("_", " "))
        query = f"{bank.name} {year_str} {label} financial report PDF"

        logger.info("Search fallback: query=%s", query)

        # Run synchronous DDGS search in a thread to avoid blocking
        try:
            results = await asyncio.to_thread(
                self._run_ddg_search, query, max_results
            )
        except Exception as exc:
            logger.warning("Search fallback failed: %s", exc)
            return []

        candidates: list[str] = []

        async with self._get_session() as session:
            for result in results:
                href = result.get("href", "")
                if not href:
                    continue

                # Direct PDF links from search results — verify before including
                if href.lower().endswith(".pdf"):
                    verified = await self.verify_url(href)
                    if verified is not None:
                        if href not in candidates:
                            candidates.append(href)
                    continue

                # Non-PDF result pages: scrape for embedded PDF links
                year_short = str(int(year_str) % 100)
                try:
                    page_pdfs = await self._scrape_page_for_pdfs(
                        session, href, report_type, year_str, year_short
                    )
                    for pdf_url in page_pdfs:
                        if pdf_url not in candidates:
                            candidates.append(pdf_url)
                except Exception:
                    continue

        logger.info(
            "Search fallback found %d candidates for %s %s %s",
            len(candidates), bank.ticker, report_type, year_str,
        )
        return candidates

    def _url_score(
        self,
        url: str,
        year_str: str,
        link_text: Optional[str] = None,
        report_type: Optional[str] = None,
        period: Optional[str] = None,
    ) -> int:
        """Score a candidate for relevance (higher = better match).

        With link_text/report_type omitted, keeps the original URL-only
        weights. When scoring rendered-page candidates, the <a> anchor text
        — what a human reads on the page — is weighted above the URL, and
        keywords of the requested report type add points while keywords of
        other report types (e.g. "interim" when fetching an annual report)
        subtract them. Period scoring (Q1/Q2/Q3/Q4/H1/H2) rewards candidates
        whose URL or link text matches the target period.
        """
        url_lower = url.lower()
        text_lower = (link_text or "").lower()

        if link_text is None and report_type is None:
            # Legacy URL-only scoring
            score = 0
            if year_str in url:
                score += 3
            if "annual" in url_lower:
                score += 2
            if "report" in url_lower:
                score += 1
            if "cdn" not in url_lower and "static" not in url_lower:
                score += 1
            if period and period in PERIOD_SCORE_KEYWORDS:
                target_keywords = PERIOD_SCORE_KEYWORDS[period]
                if any(kw in url_lower for kw in target_keywords):
                    score += 3
                for other_period, keywords in PERIOD_SCORE_KEYWORDS.items():
                    if other_period == period:
                        continue
                    if any(kw in url_lower for kw in keywords):
                        score -= 2
            return score

        score = 0
        # Year: anchor text is the stronger signal
        if year_str in text_lower:
            score += 4
        elif year_str in url_lower:
            score += 3

        # Report type: reward the requested type, penalize other types
        if report_type:
            for other_type, keywords in REPORT_TYPE_SCORE_KEYWORDS.items():
                hit_in_text = any(kw in text_lower for kw in keywords)
                hit_in_url = any(kw in url_lower for kw in keywords)
                if other_type == report_type:
                    if hit_in_text:
                        score += 4
                    elif hit_in_url:
                        score += 3
                else:
                    if hit_in_text:
                        score -= 3
                    elif hit_in_url:
                        score -= 2

        # Period: reward the requested period, penalize other periods
        if period and period in PERIOD_SCORE_KEYWORDS:
            target_keywords = PERIOD_SCORE_KEYWORDS[period]
            target_hit_text = any(kw in text_lower for kw in target_keywords)
            target_hit_url = any(kw in url_lower for kw in target_keywords)
            if target_hit_text:
                score += 4
            elif target_hit_url:
                score += 3
            for other_period, keywords in PERIOD_SCORE_KEYWORDS.items():
                if other_period == period:
                    continue
                hit_in_text = any(kw in text_lower for kw in keywords)
                hit_in_url = any(kw in url_lower for kw in keywords)
                if hit_in_text:
                    score -= 3
                elif hit_in_url:
                    score -= 2

        # Non-report documents (announcements etc.) rank below real reports
        if any(kw in text_lower for kw in NON_REPORT_SCORE_KEYWORDS):
            score -= 3
        elif any(kw in url_lower for kw in NON_REPORT_SCORE_KEYWORDS):
            score -= 2

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