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
    """Result of a fetch operation for a single report spec."""

    bank: BankSpec
    report_type: str = ""
    period: str = ""
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
        """Download a PDF report with retry and validation.

        Retrieves the file at *url*, validates it is a real PDF (``%PDF``
        magic header), writes it to *target*, and returns a
        :class:`Report` metadata object.

        Retry behavior (via ``tenacity``):
            Up to 3 attempts with exponential backoff (2s → 4s → 8s, capped
            at 30s).  Only retries on :exc:`aiohttp.ClientError` and
            :exc:`asyncio.TimeoutError`; validation errors (non-200 status,
            missing PDF header) are **not** retried — they fail immediately
            with the reason logged.

        Pipeline steps:
            1. Rate-limit the request to *url*'s domain via
               :meth:`_rate_limit`.
            2. Ensure the parent directory of *target* exists.
            3. Issue a GET request (following up to 5 redirects).
            4. Verify the response status is 200; raise
               :exc:`aiohttp.ClientResponseError` otherwise.
            5. Validate the response body starts with ``%PDF``.
            6. Write the content to *target*, compute SHA-256 hash, and
               log the download.

        Args:
            url: Direct URL to the PDF file.
            target: Destination file path (parent directory is created
                if missing).
            bank: Bank specification — used for logging and :class:`Report`
                metadata.
            report_type: Report type key (e.g. ``"annual_report"``,
                ``"10-K"``).
            year: Fiscal year of the report.
            period: Period string (e.g. ``"FY"``, ``"Q1"``).

        Returns:
            A :class:`Report` instance with the downloaded file's metadata
            (path, size, SHA-256 hash, source URL).

        Raises:
            aiohttp.ClientResponseError: If the server returns a non-200
                HTTP status (e.g. 404, 403).
            ValueError: If the downloaded content does not start with
                the ``%PDF`` magic header (corrupted or wrong file type).
            aiohttp.ClientError: On network-level failures (retried up to
                3 times before re-raising).
            asyncio.TimeoutError: On request timeout (retried up to 3 times
                before re-raising).
        """

        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        target.parent.mkdir(parents=True, exist_ok=True)
        session = self._get_session()

        async with session.get(url, allow_redirects=True, max_redirects=5) as resp:
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
            bank.ticker,
            report_type,
            target.name,
            file_size / (1024 * 1024),
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
        year: int,
        report_specs: list[tuple[str, str]],
        *,
        force: bool = False,
    ) -> list[FetchResult]:
        """Fetch reports for a bank — template method.

        Accepts a list of ``(report_type, period)`` tuples and returns a
        :class:`FetchResult` for each.  The default implementation
        iterates over *report_specs* sequentially, checking the local
        cache, discovering the URL via :meth:`discover_urls`, and downloading
        the PDF.

        Subclasses **may override** this method to batch-optimise (e.g.
        crawl the site once and extract multiple PDF links in a
        single pass).  The default sequential behaviour is a safe fallback.

        Pipeline steps (per spec):
            1. Check local cache (skip if already downloaded unless *force*).
            2. Call :meth:`discover_urls` for the first valid candidate URL per spec.
            3. Download the verified PDF via :meth:`_download`.

        Args:
            bank: Bank specification from config.
            year: Fiscal year to fetch.
            report_specs: List of ``(report_type, period)`` tuples to fetch
                (e.g. ``[("10-K", "Q4"), ("quarterly_report", "Q1")]``).
            force: If ``True``, re-download even if a local file exists.

        Returns:
            A list of :class:`FetchResult` instances — one per input spec,
            in the same order as *report_specs*.
        """

        results: list[FetchResult] = []

        tocache_specs: list[tuple[str, str]] = []
        tocache_targets: dict[tuple[str, str], Path] = {}

        for report_type, period in report_specs:
            target = self.target_path(
                self.data_dir, year, bank.ticker_safe, report_type, period
            )
            if not force and target.exists():
                logger.info("Already downloaded, skipping: %s", target.name)
                result = FetchResult(
                    bank=bank,
                    report_type=report_type,
                    period=period,
                    report=Report(
                        bank_ticker=bank.ticker,
                        report_type=report_type,
                        year=year,
                        period=period,
                        local_path=target,
                        downloaded_from="(cached)",
                        file_size=target.stat().st_size,
                    ),
                )
                results.append(result)
            else:
                tocache_specs.append((report_type, period))
                tocache_targets[(report_type, period)] = target

        if not tocache_specs:
            return results

        urls = await self.discover_urls(bank, year, tocache_specs)
        for (report_type, period), url in zip(tocache_specs, urls):
            target = tocache_targets[(report_type, period)]
            result = FetchResult(bank=bank, report_type=report_type, period=period)

            if url is None:
                result.error = (
                    f"No PDF URL found for {bank.ticker} {report_type} {year}"
                )
                results.append(result)
                continue

            try:
                report = await self._download(
                    url, target, bank, report_type, year, period
                )
                result.report = report
            except Exception as exc:
                logger.warning("Download failed for %s: %s", url, exc)
                result.error = f"{url}: {exc}"

            results.append(result)

        return results

    # ── Abstract interface ──────────────────────────────────────────────

    @abstractmethod
    async def discover_urls(
        self,
        bank: BankSpec,
        year: int,
        report_specs: list[tuple[str, str]],
    ) -> list[Optional[str]]:
        """Discover PDF URLs for a batch of report specs.

        Accepts a list of ``(report_type, period)`` tuples and returns a
        URL (or ``None``) for each, in the same order.  Implementations
        **may** batch-optimise (e.g. crawl the bank's related site once
        and match links to all specs in a single pass).

        Args:
            bank: Bank specification.
            year: Target fiscal year.
            report_specs: List of ``(report_type, period)`` tuples.

        Returns:
            A list of URLs or ``None`` values — one per input spec,
            in the same order as *report_specs*.
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
