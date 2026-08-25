"""Web scraping utilities for finding PDF links and navigation URLs."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Callable, Awaitable, Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

from noobanks.sources.keywords import (
    NAV_EXCLUDE_TEXT,
    NAV_KEYWORDS,
    REPORT_PATTERNS,
)

logger = logging.getLogger(__name__)


def extract_pdf_links(
    html: str,
    base_url: str,
    report_type: str,
    year_str: str,
    *,
    report_patterns: Optional[dict[str, list[str]]] = None,
) -> list[tuple[str, str]]:
    """Parse HTML and extract PDF hrefs matching the report type + year.

    A PDF link is returned when both year and report-type information
    are present, though they may appear in different signals:
    - Year may appear in the href or the <a> tag text.
    - Report-type keywords may appear in the href or the <a> tag text.

    Args:
        html: Raw HTML of the page.
        base_url: Base URL for resolving relative links.
        report_type: Target report type key (e.g. "annual_report", "10-K").
        year_str: 4-digit year as string (e.g. "2025").
        report_patterns: Dict of report_type -> pattern list. If None,
            uses the default REPORT_PATTERNS from the keywords module.

    Returns:
        List of (url, anchor_text) tuples.  Anchor text is the stripped
        text of the <a> tag, or an empty string when unavailable.
    """
    if report_patterns is None:
        report_patterns = REPORT_PATTERNS
    patterns = [p.lower() for p in report_patterns.get(report_type, [report_type.lower()])]
    year_short = year_str[2:]

    soup = BeautifulSoup(html, "lxml")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a"):
        href_value = a_tag.get("href") or a_tag.get("name")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        href_lower = href.lower()

        if not href_lower.endswith(".pdf"):
            continue

        link_text = a_tag.get_text(strip=True)

        year_present = (
            year_str in href
            or year_short in href
            or year_str in link_text
            or year_short in link_text
            or year_str in base_url
            or year_short in base_url
        )

        type_present = any(
            p in href_lower or p in link_text.lower() or p in base_url.lower()
            for p in patterns
        )

        if year_present and type_present:
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                links.append((full_url, link_text))

    return links


def extract_nav_links(html: str, base_url: str) -> list[str]:
    """Extract report-related navigation links from an webpage.

    Finds <a> tags whose text or href contains report-related keywords.
    Excludes PDF links, generic navigation, and already-seen URLs.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []

    for a_tag in soup.find_all("a"):
        href_value = a_tag.get("href") or a_tag.get("name")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        href_lower = href.lower()

        if href_lower.endswith(".pdf"):
            continue
        if href.startswith(("javascript:", "mailto:", "#")):
            continue

        link_text = a_tag.get_text(strip=True).lower()

        if link_text in NAV_EXCLUDE_TEXT:
            continue

        text_or_href = f"{href_lower} {link_text}"
        if not any(kw in text_or_href for kw in NAV_KEYWORDS):
            continue

        full_url = urljoin(base_url, href)
        if full_url not in seen:
            seen.add(full_url)
            links.append(full_url)

    return links


async def crawl_pdf_links(
    base_url: str,
    report_type: str,
    year_str: str,
    *,
    max_depth: int = 0,
    max_pages: Optional[int] = None,
    page_getter: Callable[[str], Awaitable[Optional[str]]],
    rate_limiter: Optional[Callable[[str], Awaitable[None]]] = None,
) -> list[tuple[str, str]]:
    """BFS-crawl pages for PDF links.

    Fetches pages via *page_getter*, extracts PDF and navigation links,
    and follows nav links breadth-first up to *max_depth*.

    Args:
        base_url: Starting URL for the crawl.
        report_type: Target report type key (e.g. ``"annual_report"``).
        year_str: 4-digit year as string (e.g. ``"2025"``).
        max_depth: BFS depth limit (0 = base_url only, 1 = base + nav, ...).
        max_pages: Maximum pages to fetch across all levels.  ``None`` = unlimited.
        page_getter: Async callable ``(url) -> html`` or ``None`` on failure.
        rate_limiter: Optional async callable ``(domain) -> None`` for throttling.

    Returns:
        List of ``(url, anchor_text)`` tuples.
    """
    domain = urlparse(base_url).netloc
    visited: set[str] = set()
    all_pdf_links: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    pages_fetched = 0

    queue: deque[tuple[str, int]] = deque([(base_url, 0)])

    while queue:
        url, depth = queue.popleft()

        if url in visited:
            continue
        visited.add(url)

        if max_pages is not None and pages_fetched >= max_pages:
            break

        if rate_limiter:
            await rate_limiter(domain)

        html = await page_getter(url)
        if html is None:
            continue
        pages_fetched += 1

        pdf_links = extract_pdf_links(html, url, report_type, year_str)
        new_pdf = 0
        for link, text in pdf_links:
            if link not in seen_urls:
                seen_urls.add(link)
                all_pdf_links.append((link, text))
                new_pdf += 1
        if pdf_links:
            logger.debug(
                "  [depth %d] %s → %d PDF links found (%d new)",
                depth, url, len(pdf_links), new_pdf,
            )

        nav_count = 0
        if depth < max_depth:
            nav_links = extract_nav_links(html, url)
            for nav_url in nav_links:
                nav_domain = urlparse(nav_url).netloc
                if nav_domain == domain and nav_url not in visited:
                    queue.append((nav_url, depth + 1))
                    nav_count += 1
            if nav_links:
                logger.debug(
                    "  [depth %d] %s → %d nav links (%d enqueued)",
                    depth, url, len(nav_links), nav_count,
                )

    logger.info(
        "Crawl complete: %d pages fetched, %d PDF links found for %s %s",
        pages_fetched, len(all_pdf_links), report_type, year_str,
    )
    return all_pdf_links


# ── Page-getter factories ────────────────────────────────────────────


def make_static_page_getter(
    session: aiohttp.ClientSession,
) -> Callable[[str], Awaitable[Optional[str]]]:
    """Return an aiohttp-based page getter for :func:`crawl_pdf_links`.

    The returned coroutine-safe callable catches HTTP/network errors
    and returns ``None`` so that :func:`crawl_pdf_links` can skip
    failing pages gracefully.
    """

    async def fetch_page(url: str) -> Optional[str]:
        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=3,
            ) as resp:
                if resp.status != 200:
                    logger.debug("Skip %s → HTTP %d", url, resp.status)
                    return None
                return await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Skip %s → %s", url, exc)
            return None

    return fetch_page


def make_browser_page_getter(
    *,
    timeout: int = 30,
    browser_max_retries: int = 2,
    user_agent: str,
) -> Callable[[str], Awaitable[Optional[str]]]:
    """Return a Playwright-based page getter for :func:`crawl_pdf_links`.

    Renders pages in a headless Chromium browser.  Uses ``domcontentloaded``
    instead of ``networkidle`` to avoid hanging on JS-heavy sites.
    Retries ``page.goto`` on timeout up to *browser_max_retries* times
    with exponential backoff.  Returns ``None`` gracefully when Playwright
    is not installed or rendering fails.
    """

    async def render_page(url: str) -> Optional[str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "Playwright not installed; run 'uv sync' to install project "
                "dependencies and 'uv run playwright install chromium' for the "
                "browser binaries",
            )
            return None

        timeout_ms = timeout * 1000

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=user_agent,
                    )
                    page = await context.new_page()
                    for attempt in range(1 + browser_max_retries):
                        try:
                            await page.goto(
                                url,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                            break
                        except TimeoutError:
                            if attempt >= browser_max_retries:
                                raise
                            wait = 2 * (2 ** attempt)
                            logger.warning(
                                "page.goto timeout for %s "
                                "(attempt %d/%d); retrying in %ds",
                                url, attempt + 1,
                                1 + browser_max_retries, wait,
                            )
                            await asyncio.sleep(wait)
                    await page.wait_for_timeout(5_000)
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("Browser render failed for %s: %s", url, exc)
            return None

    return render_page