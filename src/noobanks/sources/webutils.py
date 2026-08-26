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


async def validate_page(
    session: aiohttp.ClientSession,
    url: str,
    rate_limiter: Optional[Callable[[str], Awaitable[None]]] = None,
) -> dict[str, bool | str]:
    """GET-check a URL to verify it returns real HTML (not a JS shell).

    Returns ``{"valid": True}`` or ``{"valid": False, "error": "..."}``.
    """
    domain = urlparse(url).netloc
    if rate_limiter:
        await rate_limiter(domain)

    try:
        async with session.get(
            url, allow_redirects=True, max_redirects=3
        ) as resp:
            if resp.status != 200:
                return {
                    "valid": False,
                    "error": f"URL returned HTTP {resp.status}: {url}",
                }

            html = await resp.text(errors="replace")
            html_size = len(html)

            soup = BeautifulSoup(html, "lxml")
            link_count = len(soup.find_all("a", href=True))

            if html_size < 1000 and link_count == 0:
                return {
                    "valid": False,
                    "error": (
                        f"URL appears to be a JS-rendered shell "
                        f"({html_size} bytes, {link_count} links): {url}"
                    ),
                }

            logger.debug(
                "URL validated: %s (%d bytes, %d links)",
                url, html_size, link_count,
            )
            return {"valid": True}

    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return {
            "valid": False,
            "error": f"URL connection failed: {url} — {exc}",
        }


async def validate_doc_url(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[dict]:
    """HEAD-check a URL to verify it points to a valid PDF.

    Args:
        url: Candidate URL to check.
        session: Optional shared session; if None a temporary
            session is created and closed automatically.

    Returns:
        Dict with ``status``, ``content_type``, ``content_length``
        if the URL points to a valid PDF, or ``None`` otherwise.
    """
    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        )
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
    finally:
        if owns_session:
            await session.close()


async def crawl_pdf_link(
    base_url: str,
    report_type: str,
    year_str: str,
    *,
    max_depth: int = 0,
    max_pages: Optional[int] = None,
    page_getter: Callable[[str], Awaitable[Optional[str]]],
    rate_limiter: Optional[Callable[[str], Awaitable[None]]] = None,
    score_func: Optional[Callable[..., int]] = None,
    score_threshold: int = 0,
    validator: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
    period: str = "FY",
) -> Optional[tuple[str, str]]:
    """BFS-crawl pages for the first valid PDF link with scoring and optional validation.

    Fetches pages via *page_getter*, extracts PDF and navigation links,
    and follows nav links breadth-first up to *max_depth*.

    When *score_func* is provided each PDF link is scored; only links whose
    score meets *score_threshold* are considered.  When *validator* is also
    provided, high-scoring links are HEAD-verified before being returned.

    Returns the **first** ``(url, anchor_text)`` tuple that passes all checks,
    or ``None`` when no qualifying link is found after the full BFS crawl.

    Args:
        base_url: Starting URL for the crawl.
        report_type: Target report type key (e.g. ``"annual_report"``).
        year_str: 4-digit year as string (e.g. ``"2025"``).
        max_depth: BFS depth limit (0 = base_url only, 1 = base + nav, ...).
        max_pages: Maximum pages to fetch across all levels.  ``None`` = unlimited.
        page_getter: Async callable ``(url) -> html`` or ``None`` on failure.
        rate_limiter: Optional async callable ``(domain) -> None`` for throttling.
        score_func: Optional scoring callable ``(url, year_str, report_type,
            link_text, period) -> int``.
        score_threshold: Minimum score to keep a link (0 = keep all when
            *score_func* is set).
        validator: Optional async HEAD-validator ``(url) -> Optional[dict]``.
            When provided, only links that HEAD-verify as valid PDFs are returned.
        period: Period string passed to *score_func* (e.g. ``"FY"``, ``"Q1"``).

    Returns:
        The first ``(url, anchor_text)`` tuple that passes all checks,
        or ``None`` when no qualifying link is found.
    """
    domain = urlparse(base_url).netloc
    visited: set[str] = set()
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
        if pdf_links:
            logger.debug(
                "  [depth %d] %s → %d PDF links found",
                depth, url, len(pdf_links),
            )

        for link, text in pdf_links:
            if score_func:
                score = score_func(
                    link, year_str, report_type,
                    link_text=text, period=period,
                )
                if score < score_threshold:
                    logger.debug(
                        "Skip low-score PDF (score=%d < %d): %s",
                        score, score_threshold, link,
                    )
                    continue

            if validator:
                result = await validator(link)
                if result is None:
                    logger.debug("Skip invalid PDF (HEAD failed): %s", link)
                    continue

            logger.info(
                "Found valid PDF after %d pages: %s",
                pages_fetched, link,
            )
            return (link, text)

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
        "Crawl complete: %d pages fetched, no valid PDF found for %s %s",
        pages_fetched, report_type, year_str,
    )
    return None


# ── Page-getter factories ────────────────────────────────────────────


def make_static_page_getter(
    session: aiohttp.ClientSession,
) -> Callable[[str], Awaitable[Optional[str]]]:
    """Return an aiohttp-based page getter for :func:`crawl_pdf_link`.

    The returned coroutine-safe callable catches HTTP/network errors
    and returns ``None`` so that :func:`crawl_pdf_link` can skip
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
    """Return a Playwright-based page getter for :func:`crawl_pdf_link`.

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
                                wait_until="load",
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
                    # wait a bit further in case page not fully loaded
                    await asyncio.sleep(5)
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("Browser render failed for %s: %s", url, exc)
            return None

    return render_page