"""IrAdapter — scrapes bank investor-relations websites for PDF reports."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from noobanks.config.models import BankSpec
from noobanks.sources.base import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.webutils import (
    crawl_pdf_links,
    make_browser_page_getter,
    make_static_page_getter,
)
from noobanks.sources.scoring import score_candidate
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["IrAdapter"]


class IrAdapter(SourceAdapter):
    """Scrapes bank investor-relations websites to discover and download PDF reports.

    Strategy:
    1. Scrape the IR landing page → extract all PDF links matching report type + year
    2. Follow navigation links and scrape those too (BFS up to ``max_depth``)
    3. Only if the static crawl finds nothing, fall back to rendering the IR
       page in a headless browser (Playwright) to capture JS-rendered content
    4. HEAD-verify candidates before download

    Both static and browser modes use the unified :func:`crawl_pdf_links`
    BFS engine; the *mode* is selected by passing a different *page_getter*
    callable (aiohttp-based vs. Playwright-based).
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
        browser_max_pages: Optional[int] = None,
        browser_max_retries: int = 2,
        score_threshold: int = 9,
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
        self.browser_max_retries = browser_max_retries
        self.score_threshold = score_threshold

    def _score_candidates(
        self,
        candidates: list[tuple[str, str]],
        year_str: str,
        report_type: str,
        period: str = "FY",
    ) -> list[tuple[str, int]]:
        """Score ``(url, anchor_text)`` candidates and return ``(url, score)`` sorted descending."""
        scored = [
            (url, score_candidate(
                url, year_str, link_text=text,
                report_type=report_type, period=period,
            ))
            for url, text in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        ir_base = bank.sources.investor_relations
        candidates: dict[str, str] = {}
        year_str = str(year)

        async with self._get_session() as session:
            validation = await self._validate_ir_url(session, ir_base)
            if not validation["valid"]:
                logger.warning(
                    "IR URL invalid for %s: %s", bank.ticker, validation["error"]
                )

            static_getter = make_static_page_getter(session)
            crawl_links = await crawl_pdf_links(
                ir_base, report_type, year_str,
                max_depth=2,
                page_getter=static_getter,
                rate_limiter=self._rate_limit,
            )
            for url, text in crawl_links:
                if url not in candidates:
                    candidates[url] = text

        scored = self._score_candidates(
            list(candidates.items()), year_str, report_type, period,
        )
        best_score = scored[0][1] if scored else 0

        if self.browser_fallback and (
            not scored or best_score < self.score_threshold
        ):
            logger.info(
                "All[%d] crawl candidates scored below threshold %d "
                "(best=%d) for %s %s %s; trying headless-browser render",
                len(scored), self.score_threshold, best_score,
                bank.ticker, report_type, year,
            )
            browser_getter = make_browser_page_getter(
                timeout=self.timeout,
                browser_max_retries=self.browser_max_retries,
                user_agent=self.user_agent,
            )
            browser_links = await crawl_pdf_links(
                ir_base, report_type, year_str,
                max_depth=2,
                max_pages=self.browser_max_pages,
                page_getter=browser_getter,
                rate_limiter=self._rate_limit,
            )
            for url, text in browser_links:
                if url not in candidates:
                    candidates[url] = text
            scored = self._score_candidates(
                list(candidates.items()), year_str, report_type, period,
            )

        all_scored = [url for url, _ in scored]

        logger.info(
            "Discovered %d candidate URLs for %s %s %s",
            len(all_scored), bank.ticker, report_type, year,
        )
        return all_scored