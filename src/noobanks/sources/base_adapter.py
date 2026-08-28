"""Abstract base classes for report source adapters."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import ssl
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiohttp
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
)

from noobanks.config.models import BankSpec
from noobanks.storage.store import DEFAULT_DATA_DIR

logger = logging.getLogger(__name__)


@dataclass
class Report:
    """A downloaded financial report file."""

    bank_ticker: str
    report_type: str  # 10-K, annual_report, interim_report, etc.
    year: int
    period: str  # FY, Q1-Q4, H1, H2
    local_path: Path
    downloaded_from: str
    file_size: int = 0
    downloaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    content_hash: str = ""

    @property
    def filename(self) -> str:
        return self.local_path.name

    @property
    def size_mb(self) -> float:
        return self.file_size / (1024 * 1024)


@dataclass
class FetchResult:
    """Result of a fetch operation for a single bank."""

    bank: BankSpec
    report: Report | None = None
    error: str | None = None

    @property
    def succeeded(self) -> int:
        return 1 if self.report else 0

    @property
    def failed(self) -> int:
        return 1 if self.error else 0

    @property
    def ok(self) -> bool:
        return self.report is not None and self.error is None


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class SourceAdapter(ABC):
    """Abstract base for report-fetching adapters.

    Concrete implementations:
    - IrAdapter:        crawls bank investor-relations websites for PDF links
    - DdgsAdapter:      discovers PDF URLs via DuckDuckGo web search
    - CompositeAdapter: Delegate to a list of adapters, with default [IrAdapter, DdgsAdapter]

    Provides shared infrastructure for all subclasses:
    - :meth:`fetch` — template method: cache check → discover → verify → download
    - :meth:`_rate_limit` — per-domain request throttling
    - :meth:`_download` — reliable PDF download with retry
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self._domain_timers: dict[str, float] = {}

    # ── HTTP session helpers ────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        """Return a shared, persistent aiohttp client session.

        The session is created lazily on first use and reused for all
        subsequent requests.  Call :meth:`close` when the adapter is
        no longer needed to release the underlying connection pool.
        """
        session = getattr(self, "_shared_session", None)
        if session is None or session.closed:
            ctx = ssl.create_default_context()
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
            session = aiohttp.ClientSession(
                headers={"User-Agent": self.user_agent},
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                connector=aiohttp.TCPConnector(ssl=ctx),
            )
            self._shared_session = session
        return session

    async def __aenter__(self) -> "SourceAdapter":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        session = getattr(self, "_shared_session", None)
        if session is not None and not session.closed:
            await session.close()
            self._shared_session = None

    # ── Shared rate-limiting ────────────────────────────────────────────

    async def _rate_limit(self, domain: str) -> None:
        """Throttle requests to *domain* based on configured delay."""
        now = time.monotonic()
        last = self._domain_timers.get(domain, 0)
        wait = self.rate_limit_delay - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._domain_timers[domain] = time.monotonic()

    # ── Shared download ─────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def _download(
        self,
        url: str,
        target: Path,
        bank: BankSpec,
        report_type: str,
        year: int,
        period: str,
    ) -> Report:
        """Download a PDF report with retry and validation."""
        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        target.parent.mkdir(parents=True, exist_ok=True)
        session = self._get_session()

        async with session.get(
            url, allow_redirects=True, max_redirects=5
        ) as resp:
            if resp.status != 200:
                raise aiohttp.ClientResponseError(
                    request_info=resp.request_info,
                    history=resp.history,
                    status=resp.status,
                    message=f"HTTP {resp.status}",
                    headers=resp.headers,
                )

            content = await resp.read()

        if len(content) < 4 or content[:4] != b"%PDF":
            raise ValueError(
                f"Downloaded file is not a valid PDF (missing %%PDF header): {url}"
            )

        target.write_bytes(content)
        file_size = len(content)
        content_hash = self._compute_hash(target)

        logger.info(
            "Downloaded %s (%s) → %s (%.1f MB)",
            bank.ticker, report_type, target.name, file_size / (1024 * 1024),
        )

        return Report(
            bank_ticker=bank.ticker,
            report_type=report_type,
            year=year,
            period=period,
            local_path=target,
            downloaded_from=url,
            file_size=file_size,
            content_hash=content_hash,
        )

    # ── Template method ──────────────────────────────────────────────────

    async def fetch(
        self,
        bank: BankSpec,
        report_type: str,
        year: int,
        period: str = "FY",
        *,
        force: bool = False,
    ) -> FetchResult:
        """Fetch reports for a bank — template method.

        1. Check local cache (skip if already downloaded)
        2. Call :meth:`discover_url` for the first valid candidate URL
        3. Download the verified PDF
        """
        result = FetchResult(bank=bank)
        target = self.target_path(
            self.data_dir, year, bank.ticker_safe, report_type, period
        )

        if not force and target.exists():
            logger.info("Already downloaded, skipping: %s", target.name)
            result.report = Report(
                bank_ticker=bank.ticker,
                report_type=report_type,
                year=year,
                period=period,
                local_path=target,
                downloaded_from="(stored)",
                file_size=target.stat().st_size,
            )
            return result

        url = await self.discover_url(bank, report_type, year, period)
        if url is None:
            result.error = (
                f"No PDF URL found for {bank.ticker} {report_type} {year}"
            )
            return result

        try:
            report = await self._download(
                url, target, bank, report_type, year, period
            )
            result.report = report
        except Exception as exc:
            logger.warning("Download failed for %s: %s", url, exc)
            result.error = f"{url}: {exc}"

        return result

    # ── Abstract interface ──────────────────────────────────────────────

    @abstractmethod
    async def discover_url(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> Optional[str]:
        """Discover the first valid PDF URL for the given bank/report/year.

        Args:
            bank: Bank specification.
            report_type: Type of report to find.
            year: Target fiscal year.
            period: Target period (FY, Q1-Q4, H1, H2).

        Returns:
            The first valid PDF URL, or ``None`` if none found.
        """
        ...

    # ── Utility helpers ────────────────────────────────────────────────

    def _compute_hash(self, filepath: Path) -> str:
        """Compute SHA-256 hash of a file."""
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()

    @staticmethod
    def target_path(
        base_dir: Path, year: int, ticker_safe: str, report_type: str, period: str
    ) -> Path:
        """Build the target file path for a report download.

        Follows the convention: {base_dir}/raw/{year}/{ticker}_{type}_{period}.pdf
        """
        filename = f"{ticker_safe}_{report_type}_{period}.pdf"
        return base_dir / "raw" / str(year) / filename
