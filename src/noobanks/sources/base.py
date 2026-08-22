"""Abstract base classes for report source adapters."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
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

from bs4 import BeautifulSoup

from noobanks.config.models import BankSpec
from noobanks.sources.extraction import (
    extract_nav_links,
    extract_pdf_links,
)
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
    url: str
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
    - :meth:`verify_url` — HEAD-based PDF validation
    - :meth:`_validate_ir_url` — GET-based page validation (detects JS shells)
    - :meth:`_rate_limit` — per-domain request throttling
    - :meth:`_download` — reliable PDF download with retry
    - :meth:`_find_pdf_links` — scrape or crawl pages for PDF links
    """

    def __init__(
        self,
        data_dir: str | Path = DEFAULT_DATA_DIR,
        *,
        timeout: int = 30,
        user_agent: str = DEFAULT_USER_AGENT,
        rate_limit_delay: float = 3.0,
        max_concurrent: int = 4,
    ):
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.user_agent = user_agent
        self.rate_limit_delay = rate_limit_delay
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._domain_timers: dict[str, float] = {}

    # ── HTTP session helpers ────────────────────────────────────────────

    def _get_session(self) -> aiohttp.ClientSession:
        """Create a new aiohttp client session with configured user-agent."""
        return aiohttp.ClientSession(
            headers={"User-Agent": self.user_agent},
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    # ── Unified HEAD verification ───────────────────────────────────────

    async def verify_url(
        self, url: str, session: Optional[aiohttp.ClientSession] = None,
    ) -> Optional[dict]:
        """HEAD-check a URL to verify it points to a valid PDF.

        Args:
            url: Candidate URL to check.
            session: Optional shared session; if None a temporary
                session is created and closed automatically.

        Returns:
            Dict with ``status``, ``content_type``, ``content_length``
            if the URL points to a valid PDF, or ``None`` otherwise.
        """
        owns_session = session is None
        if owns_session:
            session = self._get_session()
        try:
            async with session.head(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
                max_redirects=5,
            ) as resp:
                content_type = resp.headers.get("Content-Type", "")
                content_length = resp.headers.get("Content-Length")
                size = int(content_length) if content_length else 0

                if resp.status != 200:
                    logger.debug("HEAD %s → HTTP %d", url, resp.status)
                    return None

                if "pdf" not in content_type.lower() and "octet-stream" not in content_type.lower():
                    if not url.lower().endswith(".pdf"):
                        logger.debug("HEAD %s → not PDF (%s)", url, content_type)
                        return None

                if size > 0 and size < 50_000:
                    logger.debug("HEAD %s → too small (%d bytes)", url, size)
                    return None

                return {
                    "status": resp.status,
                    "content_type": content_type,
                    "content_length": size,
                }
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug("HEAD %s → error: %s", url, exc)
            return None
        finally:
            if owns_session:
                await session.close()

    # ── Shared rate-limiting ────────────────────────────────────────────

    async def _rate_limit(self, domain: str) -> None:
        """Throttle requests to *domain* based on configured delay."""
        now = time.monotonic()
        last = self._domain_timers.get(domain, 0)
        wait = self.rate_limit_delay - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)
        self._domain_timers[domain] = time.monotonic()

    # ── Shared IR page validation ───────────────────────────────────────

    async def _validate_ir_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
    ) -> dict[str, bool | str]:
        """GET-check a page to verify it returns real HTML (not a JS shell).

        Returns ``{"valid": True}`` or ``{"valid": False, "error": "..."}``.
        """
        domain = urlparse(url).netloc
        await self._rate_limit(domain)

        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=3
            ) as resp:
                if resp.status != 200:
                    return {
                        "valid": False,
                        "error": f"IR URL returned HTTP {resp.status}: {url}",
                    }

                html = await resp.text(errors="replace")
                html_size = len(html)

                soup = BeautifulSoup(html, "lxml")
                link_count = len(soup.find_all("a", href=True))

                if html_size < 1000 and link_count == 0:
                    return {
                        "valid": False,
                        "error": (
                            f"IR URL appears to be a JS-rendered shell "
                            f"({html_size} bytes, {link_count} links): {url}"
                        ),
                    }

                logger.debug(
                    "IR URL validated: %s (%d bytes, %d links)",
                    url, html_size, link_count,
                )
                return {"valid": True}

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return {
                "valid": False,
                "error": f"IR URL connection failed: {url} — {exc}",
            }

    # ── Shared PDF discovery (single-page scrape or multi-page crawl) ───

    async def _find_pdf_links(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        report_type: str,
        year_str: str,
        max_depth: int = 0,
    ) -> list[tuple[str, str]]:
        """Fetch pages and extract PDF links.

        When *max_depth* is 0 (default) only *base_url* is scraped.
        When *max_depth* > 0 the page is crawled BFS up to that depth,
        following navigation links to discover further PDF pages.
        """
        domain = urlparse(base_url).netloc
        visited: set[str] = set()
        all_pdf_links: list[tuple[str, str]] = []
        seen_urls: set[str] = set()

        queue: deque[tuple[str, int]] = deque([(base_url, 0)])

        while queue:
            url, depth = queue.popleft()

            if url in visited:
                continue
            visited.add(url)

            await self._rate_limit(domain)
            try:
                async with session.get(
                    url, allow_redirects=True, max_redirects=3
                ) as resp:
                    if resp.status != 200:
                        logger.debug("Skip %s → HTTP %d", url, resp.status)
                        continue
                    html = await resp.text(errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.debug("Skip %s → %s", url, exc)
                continue

            pdf_links = extract_pdf_links(html, url, report_type, year_str)
            for link, text in pdf_links:
                if link not in seen_urls:
                    seen_urls.add(link)
                    all_pdf_links.append((link, text))

            if pdf_links and depth > 0:
                continue

            if depth < max_depth:
                nav_links = extract_nav_links(html, url)
                for nav_url in nav_links:
                    nav_domain = urlparse(nav_url).netloc
                    if nav_domain == domain and nav_url not in visited:
                        queue.append((nav_url, depth + 1))

        logger.debug(
            "Find PDFs from %s: visited %d pages, found %d PDFs (max depth %d)",
            base_url, len(visited), len(all_pdf_links), max_depth,
        )
        return all_pdf_links

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

        async with self._semaphore:
            async with self._get_session() as session:
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
            url=url,
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
        2. Call :meth:`discover_urls` for candidate URLs
        3. HEAD-verify each candidate
        4. Download the first verified PDF
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
                url="(cached)",
                file_size=target.stat().st_size,
            )
            return result

        urls = await self.discover_urls(bank, report_type, year, period)
        if not urls:
            result.error = (
                f"No PDF URLs found for {bank.ticker} {report_type} {year}"
            )
            return result

        for url in urls:
            verified = await self.verify_url(url)
            if verified is None:
                continue

            try:
                report = await self._download(
                    url, target, bank, report_type, year, period
                )
                result.report = report
                return result
            except Exception as exc:
                logger.warning("Download failed for %s: %s", url, exc)
                result.error = f"{url}: {exc}"

        if result.report is None:
            result.error = (
                f"All {len(urls)} candidate URLs failed for {bank.ticker}"
            )
        return result

    # ── Abstract interface ──────────────────────────────────────────────

    @abstractmethod
    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int,
        period: str = "FY",
    ) -> list[str]:
        """Discover candidate PDF URLs for the given bank/report/year.

        Args:
            bank: Bank specification.
            report_type: Type of report to find.
            year: Target fiscal year.
            period: Target period (FY, Q1-Q4, H1, H2).

        Returns:
            List of candidate PDF URLs (unverified).
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