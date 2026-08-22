"""CompositeAdapter — chains multiple source adapters, returning the first success."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from noobanks.config.models import BankSpec
from noobanks.sources.base import (
    DEFAULT_USER_AGENT,
    SourceAdapter,
)
from noobanks.sources.ddgs_adapter import DdgsAdapter
from noobanks.sources.ir_adapter import IrAdapter
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)

__all__ = ["CompositeAdapter"]


class CompositeAdapter(SourceAdapter):
    """Adapter that chains multiple source adapters, returning the first success.

    Accepts an ordered list of :class:`SourceAdapter` instances.  During
    :meth:`discover_urls` each adapter is tried in sequence — the first one
    to return non-empty URLs wins.

    :meth:`fetch` is inherited from :class:`SourceAdapter` and automatically
    uses the chained :meth:`discover_urls`.

    If *adapters* is not supplied a sensible default chain is built:

    1. :class:`IrAdapter`  — crawls investor-relations sites
    2. :class:`DdgsAdapter` — DDG web-search fallback

    Examples::

        # Default IR + DDGS chain
        adapter = CompositeAdapter()

        # Fully custom chain
        adapter = CompositeAdapter(adapters=[CustomAdapterA(), CustomAdapterB()])
    """

    def __init__(
        self,
        adapters: Optional[list[SourceAdapter]] = None,
        *,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
        **kwargs,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
            max_concurrent=max_concurrent,
        )

        if adapters is None:
            ir_kwargs = {
                k: v for k, v in kwargs.items()
                if k in ("browser_fallback", "browser_max_pages")
            }
            adapters = [
                IrAdapter(
                    data_dir=data_dir,
                    timeout=timeout,
                    user_agent=user_agent,
                    rate_limit_delay=rate_limit_delay,
                    max_concurrent=max_concurrent,
                    **ir_kwargs,
                ),
                kwargs.pop("fallback_adapter", None) or DdgsAdapter(
                    data_dir=data_dir,
                    timeout=timeout,
                    user_agent=user_agent,
                    rate_limit_delay=rate_limit_delay,
                    max_concurrent=max_concurrent,
                ),
            ]

        if not adapters:
            raise ValueError("CompositeAdapter requires at least one adapter")

        self.adapters: list[SourceAdapter] = list(adapters)

    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        for adapter in self.adapters:
            urls = await adapter.discover_urls(bank, report_type, year, period)
            if urls:
                logger.info(
                    "Adapter %s found %d URLs for %s %s %d",
                    type(adapter).__name__, len(urls),
                    bank.ticker, report_type, year,
                )
                return urls
        logger.warning(
            "No URLs found by any adapter for %s %s %d",
            bank.ticker, report_type, year,
        )
        return []