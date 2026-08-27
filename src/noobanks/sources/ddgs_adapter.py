"""DdgsAdapter — discovers bank report PDF URLs via DuckDuckGo web search."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
from ddgs import DDGS

from noobanks.config.models import BankSpec
from noobanks.sources.base_adapter import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.keywords import REPORT_TYPE_LABELS
from noobanks.sources.scoring import score_candidate
from noobanks.sources.webutils import (
    crawl_pdf_link, 
    make_static_page_getter, 
    validate_doc_url
)
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["DdgsAdapter"]

DEFAULT_DDGS_MAX_RESULTS = 10


class DdgsAdapter(SourceAdapter):
    """Discovers report PDF URLs via DuckDuckGo web search.

    Runs a DDGS text search with the bank name + year + report type,
    collects direct PDF links and scrapes non-PDF result pages for
    embedded PDFs.

    Returns the **first** valid PDF URL found, or ``None`` if none found.
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        max_results: int = DEFAULT_DDGS_MAX_RESULTS,
        score_threshold: int = 12,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
        )
        self.max_results = max_results
        self.score_threshold = score_threshold

    def _run_search(self, query: str, max_results: int) -> list[dict[str, Any]]:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=max_results))

    def _build_search_query(
        self,
        bank: BankSpec,
        report_type: str,
        year_str: str,
    ) -> str:
        label = REPORT_TYPE_LABELS.get(
            report_type, report_type.replace("_", " "),
        )
        return f"{bank.name} {year_str} {label} financial report PDF"

    async def discover_url(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> Optional[str]:
        year_str = str(year)
        query = self._build_search_query(bank, report_type, year_str)
        logger.info("DDGS search: query=%s", query)

        try:
            results = await asyncio.to_thread(
                self._run_search, query, self.max_results,
            )
        except Exception as exc:
            logger.warning("DDGS search failed: %s", exc)
            return None

        session = self._get_session()
        static_getter = make_static_page_getter(session)
        for result in results:
            href = result.get("href", "")
            if not href:
                continue

            if href.lower().endswith(".pdf"):
                verified = await validate_doc_url(href, session=session)
                if verified is not None:
                    link_text = result.get("title", "")
                    s = score_candidate(
                        href, year_str, report_type,
                        link_text=link_text, period=period,
                        aliases=bank.aliases,
                    )
                    if s >= self.score_threshold:
                        logger.info(
                            "DDGS found direct PDF for %s %s %d: %s (score=%d)",
                            bank.ticker, report_type, year, href, s,
                        )
                        return href
                    logger.debug(
                        "DDGS direct PDF below threshold (%d < %d): %s",
                        s, self.score_threshold, href,
                    )
                continue

            try:
                page_pdf = await crawl_pdf_link(
                    href, report_type, year_str,
                    page_getters=[static_getter],
                )
                if page_pdf is not None:
                    pdf_url, _ = page_pdf
                    logger.info(
                        "DDGS found PDF via page crawl for %s %s %d: %s",
                        bank.ticker, report_type, year, pdf_url,
                    )
                    return pdf_url
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                continue

        logger.info(
            "DDGS found no valid PDF for %s %s %d",
            bank.ticker, report_type, year,
        )
        return None