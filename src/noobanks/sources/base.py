"""Abstract base classes for report source adapters."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from noobanks.config.models import BankSpec


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
    reports: list[Report] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return len(self.reports)

    @property
    def failed(self) -> int:
        return len(self.errors)

    @property
    def ok(self) -> bool:
        return len(self.reports) > 0 and len(self.errors) == 0


class SourceAdapter(ABC):
    """Abstract base for report-fetching adapters.

    Each adapter handles a specific source type:
    - GenericIrAdapter: scrapes bank investor-relations pages
    - EdgarAdapter (future): SEC EDGAR CIK-based lookup
    """

    @abstractmethod
    async def fetch(
        self,
        bank: BankSpec,
        report_type: str,
        year: int,
        period: str = "FY",
        *,
        force: bool = False,
    ) -> FetchResult:
        """Fetch reports for a bank and return the result.

        Args:
            bank: Bank specification from config.
            report_type: Type of report (annual_report, 10-K, etc.).
            year: Fiscal year of the report.
            period: Reporting period (FY, Q1-Q4, H1, H2).
            force: If True, re-download even if the file already exists.

        Returns:
            FetchResult with downloaded reports and any errors.
        """
        ...

    @abstractmethod
    async def discover_urls(
        self, bank: BankSpec, report_type: str, year: int
    ) -> list[str]:
        """Discover candidate PDF URLs for the given bank/report/year.

        Args:
            bank: Bank specification.
            report_type: Type of report to find.
            year: Target fiscal year.

        Returns:
            List of candidate PDF URLs (unverified).
        """
        ...

    @abstractmethod
    async def verify_url(self, url: str) -> Optional[dict]:
        """HEAD-check a URL to verify it points to a valid PDF.

        Args:
            url: Candidate URL to check.

        Returns:
            Dict with status, content_type, content_length if valid,
            or None if the URL is not a valid PDF.
        """
        ...

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
