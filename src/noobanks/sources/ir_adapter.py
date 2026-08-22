"""IrAdapter — scrapes bank investor-relations websites for PDF reports."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp

from noobanks.config.models import BankSpec
from noobanks.sources.base import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.extraction import extract_nav_links, extract_pdf_links
from noobanks.sources.scoring import score_candidate
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["IrAdapter"]


class IrAdapter(SourceAdapter):
    """Scrapes bank investor-relations websites to discover and download PDF reports.

    Strategy:
    1. Scrape the IR landing page → extract all PDF links matching report type + year
    2. Follow common IR sub-paths and scrape those too
    3. If the static crawl finds nothing, render the IR page in a headless
       browser (Playwright) to capture JS-rendered content
    4. HEAD-verify candidates before download
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
        browser_fallback: bool = True,
        browser_max_pages: int = 3,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
            max_concurrent=max_concurrent,
        )
        self.browser_fallback = browser_fallback
        self.browser_max_pages = browser_max_pages

    def _sort_candidates(
        self,
        candidates: list[tuple[str, str]],
        year_str: str,
        report_type: str,
        period: str = "FY",
    ) -> list[str]:
        """Sort ``(url, anchor_text)`` candidates by relevance score."""
        return [
            url
            for url, _ in sorted(
                candidates,
                key=lambda ut: score_candidate(
                    ut[0], year_str, link_text=ut[1],
                    report_type=report_type, period=period,
                ),
                reverse=True,
            )
        ]

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        ir_base = bank.sources.investor_relations.rstrip("/")
        candidates: dict[str, str] = {}
        year_str = str(year)

        async with self._get_session() as session:
            validation = await self._validate_ir_url(session, ir_base)
            if not validation["valid"]:
                logger.warning(
                    "IR URL invalid for %s: %s", bank.ticker, validation["error"]
                )

            crawl_links = await self._find_pdf_links(
                session, ir_base, report_type, year_str, max_depth=2,
            )
            for url, text in crawl_links:
                if url not in candidates:
                    candidates[url] = text

        candidates_list = self._sort_candidates(
            list(candidates.items()), year_str, report_type, period,
        )

        if not candidates_list and self.browser_fallback:
            logger.info(
                "Crawl found no PDFs for %s %s %s; trying headless-browser render",
                bank.ticker, report_type, year,
            )
            browser_links = await self._discover_via_browser(
                ir_base, report_type, year_str
            )
            candidates_list = self._sort_candidates(
                browser_links, year_str, report_type, period,
            )

        logger.info(
            "Discovered %d candidate URLs for %s %s %s",
            len(candidates_list), bank.ticker, report_type, year,
        )
        return candidates_list

    async def _discover_via_browser(
        self,
        ir_base: str,
        report_type: str,
        year_str: str,
        max_pages: Optional[int] = None,
    ) -> list[tuple[str, str]]:
        domain = urlparse(ir_base).netloc
        await self._rate_limit(domain)

        html = await self._render_page(ir_base)
        if html is None:
            return []

        pdfs = extract_pdf_links(
            html, ir_base, report_type, year_str
        )
        if pdfs:
            return pdfs

        nav_links = extract_nav_links(html, ir_base)
        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        pages_rendered = 0
        for nav_url in nav_links:
            if urlparse(nav_url).netloc != domain:
                continue
            if pages_rendered >= (max_pages or self.browser_max_pages):
                break
            pages_rendered += 1

            await self._rate_limit(domain)
            nav_html = await self._render_page(nav_url)
            if nav_html is None:
                continue
            for link, text in extract_pdf_links(
                nav_html, nav_url, report_type, year_str,
            ):
                if link not in seen:
                    seen.add(link)
                    results.append((link, text))

        return results

    async def _render_page(
        self, url: str, timeout: Optional[int] = None,
    ) -> Optional[str]:
        """Render a page in a headless browser and return its HTML.

        Falls back gracefully when Playwright is not installed.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning(
                "Playwright not installed; run 'uv sync' to install project "
                "dependencies and 'uv run playwright install chromium' for the "
                "browser binaries",
            )
            return None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    context = await browser.new_context(
                        user_agent=self.user_agent,
                    )
                    page = await context.new_page()
                    await page.goto(
                        url,
                        wait_until="networkidle",
                        timeout=(timeout or self.timeout) * 1000,
                    )
                    return await page.content()
                finally:
                    await browser.close()
        except Exception as exc:
            logger.warning("Browser render failed for %s: %s", url, exc)
            return None