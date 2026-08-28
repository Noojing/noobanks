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
)

logger = logging.getLogger(__name__)


def extract_pdf_links(
    html: str,
    base_url: str,
) -> list[tuple[str, str]]:
    """Parse HTML and extract all PDF hrefs.

    Returns every link whose href ends with ``.pdf``.  Relevance filtering
    (year, report type, etc.) is deferred to the scoring step so that no
    candidate is missed by pre-filtering.

    Args:
        html: Raw HTML of the page.
        base_url: Base URL for resolving relative links.

    Returns:
        List of (url, anchor_text) tuples.  Anchor text is the stripped
        text of the <a> tag, or an empty string when unavailable.
    """

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

        full_url = urljoin(base_url, href)
        if full_url not in seen:
            seen.add(full_url)
            links.append((full_url, str(a_tag)))

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


def _validate_html_content(html: str, url: str) -> dict[str, bool | str]:
    """Check whether *html* contains real page content (not a JS shell)."""
    html_size = len(html)
    soup = BeautifulSoup(html, "lxml")
    link_count = len(soup.find_all("a", href=True))

    if html_size < 1000 and link_count == 0:
        error = (
            f"[{html_size} bytes, {link_count} links)] "
            f"URL appears to be a JS-rendered shell. {url}"
        )
        return {"valid": False, "error": error}

    logger.debug(
        "HTML validation [passed]: [%d bytes, %d links] %s",
        html_size,
        link_count,
        url,
    )
    return {"valid": True}


async def validate_doc_url(
    url: str,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[str]:
    """HEAD-check a URL to retrieve its content type.

    Makes a HEAD request and returns the ``Content-Type`` header on success.
    Falls back to a GET request if HEAD returns a non-200 status (e.g. when
    a WAF blocks HEAD specifically).
    Returns ``None`` if the request fails entirely.

    Args:
        url: Candidate URL to check.
        session: Optional shared session; if None a temporary
            session is created and closed automatically.

    Returns:
        The content-type string, or ``None`` on failure.
    """

    owns_session = session is None
    if owns_session:
        session = aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=15),
        )

    try:
        try:
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
                max_redirects=5,
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if resp.status != 200:
                    logger.debug(
                        "HEAD %s → HTTP %d, falling back to GET",
                        url,
                        resp.status,
                    )
                else:
                    logger.debug("HEAD %s → HTTP %d", url, resp.status)
                    return content_type
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug(
                "HEAD %s → error: %s, falling back to GET",
                url,
                exc,
            )

        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
                max_redirects=5,
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if resp.status != 200:
                    logger.debug("GET %s → HTTP %d", url, resp.status)
                return content_type
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("GET %s → error: %s", url, exc)
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
    page_getters: list[Callable[[str], Awaitable[Optional[str]]]],
    rate_limiter: Optional[Callable[[str], Awaitable[None]]] = None,
    score_func: Optional[Callable[..., int]] = None,
    score_threshold: int = 0,
    validator: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
    period: str = "FY",
    aliases: Optional[list[str]] = None,
) -> Optional[tuple[str, str]]:
    """BFS-crawl pages for the first valid PDF link with scoring and optional validation.

    Fetches pages via *page_getters* (tried in order for each page — the
    first getter to return non-JS-shell HTML is used), extracts PDF and
    navigation links, and follows nav links breadth-first up to *max_depth*.

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
        page_getters: Ordered list of async callables ``(url) -> html`` or
            ``None`` on failure.  For each page the getters are tried in
            sequence; the first one whose HTML passes JS-shell validation
            is used for link extraction.
        rate_limiter: Optional async callable ``(domain) -> None`` for throttling.
        score_func: Optional scoring callable ``(url, year_str, report_type,
            link_text, period) -> int``.
        score_threshold: Minimum score to keep a link (0 = keep all when
            *score_func* is set).
        validator: Optional async HEAD-validator ``(url) -> Optional[str]``.
            When provided, only links that HEAD-verify successfully are returned.
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
        logger.debug("crawling: depth [%s], url [%s]", depth, url)

        if url in visited:
            continue
        visited.add(url)

        if max_pages is not None and pages_fetched >= max_pages:
            break

        if rate_limiter:
            await rate_limiter(domain)

        html = None
        for getter in page_getters:
            logger.debug("\ttrying via %s", getter.__qualname__)
            type = await validate_doc_url(url)
            if type is None or "html" not in type:
                logger.debug("\turl has type [%s]; skipped", type or "N/A")
                continue
            html = await getter(url)
            if html is None:
                continue
            validation = _validate_html_content(html, url)
            if validation.get("valid"):
                break
            logger.debug("\turl failed with error: %s", validation.get("error"))
            html = None

        if html is None:
            continue
        pages_fetched += 1

        if aliases:
            html_lower = html.lower()
            url_lower = url.lower()
            if not any(
                alias.lower() in html_lower or alias.lower() in url_lower
                for alias in aliases
            ):
                logger.debug(
                    "\t[depth %d] %s → no alias match, skipping",
                    depth,
                    url,
                )
                continue

        pdf_links = extract_pdf_links(html, url)
        logger.debug(
            "\t[%d] PDF links found",
            len(pdf_links),
        )

        for link, text in pdf_links:
            if score_func:
                score = score_func(
                    link,
                    year_str,
                    report_type,
                    link_text=text,
                    period=period,
                    aliases=aliases,
                )
                if score < score_threshold:
                    logger.debug(
                        "\t\tskip low-score PDF (score=%d < %d): %s",
                        score,
                        score_threshold,
                        link,
                    )
                    continue

            if validator:
                type = await validator(link)
                if type is None or "pdf" not in type:
                    logger.debug(
                        "\t\tskip invalid PDF (HEAD failed - type [%s]): %s",
                        type or "N/A",
                        link,
                    )
                    continue

            logger.info(
                "\t\tfound valid PDF after %d pages: %s",
                pages_fetched,
                link,
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
                    depth,
                    url,
                    len(nav_links),
                    nav_count,
                )

    logger.info(
        "crawl complete: %d pages fetched, no valid PDF found for %s %s",
        pages_fetched,
        report_type,
        year_str,
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
                url,
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status != 200:
                    logger.debug("Skip %s → HTTP %d", url, resp.status)
                    return None
                return await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Skip %s → %s", url, exc)
            return None

    return fetch_page


_STALTH_SCRIPT = r"""
Object.defineProperty(navigator, 'webdriver', {
    get: () => false,
});

Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});

Object.defineProperty(navigator, 'platform', {
    get: () => 'Win32',
});

Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
});

Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
});

window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {},
};

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

const toDataURL = HTMLCanvasElement.prototype.toDataURL;
HTMLCanvasElement.prototype.toDataURL = function(type) {
    if (type === 'image/png' && this.width === 220 && this.height === 30) {
        return 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
    }
    return toDataURL.apply(this, arguments);
};

const originalToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (this.name && this.name.startsWith('get')) {
        return 'function ' + this.name + '() { [native code] }';
    }
    return originalToString.apply(this, arguments);
};

window.addEventListener('DOMContentLoaded', () => {
    const originalQuery = window.navigator.permissions.query;
});
"""


def make_browser_page_getter(
    *,
    timeout: int = 30,
    browser_max_retries: int = 2,
    user_agent: str,
    stealth: bool = True,
) -> Callable[[str], Awaitable[Optional[str]]]:
    """Return a Playwright-based page getter for :func:`crawl_pdf_link`.

    Renders pages in a headless Chromium browser.  Uses ``domcontentloaded``
    instead of ``networkidle`` to avoid hanging on JS-heavy sites.
    Retries ``page.goto`` on timeout up to *browser_max_retries* times
    with exponential backoff.  Returns ``None`` gracefully when Playwright
    is not installed or rendering fails.

    When *stealth* is ``True`` (the default), anti-bot-detection patches
    are injected to evade WAFs (e.g. Imperva/Perimeter X).
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
                        viewport={"width": 1920, "height": 1080},
                    )
                    page = await context.new_page()
                    if stealth:
                        await page.add_init_script(_STALTH_SCRIPT)
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
                            wait = 2 * (2**attempt)
                            logger.warning(
                                "page.goto timeout for %s "
                                "(attempt %d/%d); retrying in %ds",
                                url,
                                attempt + 1,
                                1 + browser_max_retries,
                                wait,
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
