"""IrAdapter — scrapes bank investor-relations websites for PDF reports."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from noobanks.config.models import BankSpec
from noobanks.sources.base_adapter import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.webutils import (
    crawl_pdf_links,
    make_browser_page_getter,
    make_static_page_getter,
    validate_doc_url,
)
from noobanks.sources.scoring import score_candidate
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["IrAdapter"]


class IrAdapter(SourceAdapter):
    """Scrapes bank investor-relations websites to discover PDF reports.

    Strategy:
    1. Scrape the IR landing page → extract PDF links matching report type + year
    2. Follow navigation links and scrape those too (BFS up to ``max_depth``)
    3. Each PDF link is scored; only links meeting ``score_threshold`` are kept
    4. High-scoring links are HEAD-verified before being returned
    5. For each page, a static (aiohttp) page getter is tried first; if the
       returned HTML is a JS shell the browser (Playwright) getter is used
       as fallback — all within a single :func:`crawl_pdf_links` call
    6. Returns a list of URLs (or ``None`` entries), one per input spec.
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        score_threshold: int = 12,
        browser_max_pages: Optional[int] = None,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
        )
        self.score_threshold = score_threshold
        self.browser_max_pages = browser_max_pages

    async def discover_urls(
        self,
        bank: BankSpec,
        year: int,
        report_specs: list[tuple[str, str]],
    ) -> list[Optional[str]]:
        ir_base = bank.sources.investor_relations
        year_str = str(year)

        session = self._get_session()
        static_getter = make_static_page_getter(session)
        browser_getter = make_browser_page_getter(
            timeout=self.timeout,
            user_agent=self.user_agent,
        )

        async def _validator(url: str) -> Optional[str]:
            return await validate_doc_url(url, session=session)

        crawl_results = await crawl_pdf_links(
            ir_base,
            year_str,
            report_specs,
            max_depth=2,
            max_pages=self.browser_max_pages,
            page_getters=[static_getter, browser_getter],
            rate_limiter=self._rate_limit,
            score_func=score_candidate,
            score_threshold=self.score_threshold,
            validator=_validator,
            aliases=bank.aliases,
        )

        urls: list[Optional[str]] = []
        for spec, crawl_result in zip(report_specs, crawl_results):
            report_type, period = spec
            if crawl_result is None:
                logger.info(
                    "No valid PDF found for %s %s %s %s",
                    bank.ticker,
                    report_type,
                    period,
                    year,
                )
                urls.append(None)
            else:
                url, _ = crawl_result
                logger.info(
                    "Discovered URL for %s %s %s %s: %s",
                    bank.ticker,
                    report_type,
                    period,
                    year,
                    url,
                )
                urls.append(url)

        return urls
