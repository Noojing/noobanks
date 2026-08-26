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
    crawl_pdf_link,
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
       as fallback — all within a single :func:`crawl_pdf_link` call
    6. Returns the **first** valid PDF URL found, or ``None`` if none found
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        score_threshold: int = 9,
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

    async def discover_url(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> Optional[str]:
        ir_base = bank.sources.investor_relations
        year_str = str(year)

        session = self._get_session()
        static_getter = make_static_page_getter(session)
        browser_getter = make_browser_page_getter(
            timeout=self.timeout,
            user_agent=self.user_agent,
        )

        async def _validator(url: str):
            return await validate_doc_url(url, session=session)

        crawl_result = await crawl_pdf_link(
            ir_base, report_type, year_str,
            max_depth=2,
            max_pages=self.browser_max_pages,
            page_getters=[static_getter, browser_getter],
            rate_limiter=self._rate_limit,
            score_func=score_candidate,
            score_threshold=self.score_threshold,
            validator=_validator,
            period=period,
        )

        if crawl_result is None:
            logger.info(
                "No valid PDF found for %s %s %s",
                bank.ticker, report_type, year,
            )
            return None

        url, _ = crawl_result
        logger.info(
            "Discovered URL for %s %s %s: %s",
            bank.ticker, report_type, year, url,
        )
        return url