"""DdgsAdapter — discovers bank report PDF URLs via DuckDuckGo web search."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import aiohttp
from ddgs import DDGS

from noobanks.config.models import BankSpec
from noobanks.sources.base import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.keywords import REPORT_TYPE_LABELS
from noobanks.sources.scoring import score_candidate
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["DdgsAdapter"]

DEFAULT_DDGS_MAX_RESULTS = 10


class DdgsAdapter(SourceAdapter):
    """Discovers report PDF URLs via DuckDuckGo web search.

    Runs a DDGS text search with the bank name + year + report type,
    collects direct PDF links and scrapes non-PDF result pages for
    embedded PDFs.

    Only :meth:`discover_urls` is implemented; :meth:`fetch` and other
    helpers are inherited from :class:`SourceAdapter`.
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
        max_results: int = DEFAULT_DDGS_MAX_RESULTS,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
            max_concurrent=max_concurrent,
        )
        self.max_results = max_results

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

    def _sort_urls(
        self,
        urls: list[str],
        year_str: str,
        report_type: str,
        period: str = "FY",
    ) -> list[str]:
        """Sort plain URL strings by relevance score."""
        return sorted(
            urls,
            key=lambda u: score_candidate(
                u, year_str, report_type=report_type, period=period,
            ),
            reverse=True,
        )

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        year_str = str(year)
        query = self._build_search_query(bank, report_type, year_str)
        logger.info("DDGS search: query=%s", query)

        try:
            results = await asyncio.to_thread(
                self._run_search, query, self.max_results,
            )
        except Exception as exc:
            logger.warning("DDGS search failed: %s", exc)
            return []

        candidates: list[str] = []

        async with self._get_session() as session:
            for result in results:
                href = result.get("href", "")
                if not href:
                    continue

                if href.lower().endswith(".pdf"):
                    verified = await self.verify_url(href, session=session)
                    if verified is not None and href not in candidates:
                        candidates.append(href)
                    continue

                try:
                    page_pdfs = await self._find_pdf_links(
                        session, href, report_type, year_str,
                    )
                    for pdf_url, _ in page_pdfs:
                        if pdf_url not in candidates:
                            candidates.append(pdf_url)
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    continue

        if candidates:
            candidates = self._sort_urls(
                candidates, year_str, report_type, period,
            )

        logger.info(
            "DDGS search found %d candidates for %s %s %d",
            len(candidates), bank.ticker, report_type, year,
        )
        return candidates