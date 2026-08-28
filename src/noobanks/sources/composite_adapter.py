"""CompositeAdapter — chains multiple source adapters, returning the first success."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from noobanks.config.models import BankSpec
from noobanks.sources.base_adapter import (
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
    to return a non-``None`` URL wins.

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
        adapters: list[SourceAdapter],
        *,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
    ):
        super().__init__(
            data_dir=data_dir,
            timeout=timeout,
            user_agent=user_agent,
            rate_limit_delay=rate_limit_delay,
        )

        if not adapters:
            raise ValueError("CompositeAdapter requires at least one adapter")

        self.adapters: list[SourceAdapter] = list(adapters)

    async def discover_urls(
        self,
        bank: BankSpec,
        year: int,
        report_specs: list[tuple[str, str]],
    ) -> list[Optional[str]]:
        """Try each adapter in order, returning the first valid URL per spec.

        Each adapter is called once with the full batch of specs.
        For each spec position, the first adapter that returns a non-``None``
        URL wins.  Adapters are tried sequentially so that higher-trust sources.
        """

        merged: list[Optional[str]] = [None] * len(report_specs)

        for adapter in self.adapters:
            if all(url is not None for url in merged):
                break
            urls = await adapter.discover_urls(bank, year, report_specs)
            for i, url in enumerate(urls):
                if url is not None and merged[i] is None:
                    merged[i] = url
                    logger.info(
                        "Adapter %s found URL for %s %s %s %d",
                        type(adapter).__name__,
                        bank.ticker,
                        report_specs[i][0],
                        report_specs[i][1],
                        year,
                    )

        missing = [report_specs[i] for i, url in enumerate(merged) if url is None]
        if missing:
            logger.warning(
                "No URL found by any adapter for %s: %s",
                bank.ticker,
                missing,
            )

        return merged

    async def __aenter__(self) -> "CompositeAdapter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        for adapter in self.adapters:
            await adapter.__aexit__(*exc_info)
        await super().__aexit__(*exc_info)
