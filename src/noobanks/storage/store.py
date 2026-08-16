"""ReportStore — manages file paths and I/O for raw, processed, and output data."""

from __future__ import annotations

from pathlib import Path

# Default data directory: ~/.noobanks/data — platform-agnostic via Path.home()
DEFAULT_DATA_DIR: Path = Path.home() / ".noobanks" / "data"


class ReportStore:
    """Manages file paths and I/O for raw, processed, and output data.

    Directory layout:
        {base_dir}/
        ├── raw/{year}/{ticker}_{type}_{period}.pdf
        ├── processed/{year}/{ticker}_{type}_{period}.md
        └── output/{bank_ticker}/{period}/metrics.json
    """

    def __init__(self, base_dir: str | Path = DEFAULT_DATA_DIR):
        self.base_dir = Path(base_dir)
        self.raw_dir = self.base_dir / "raw"
        self.processed_dir = self.base_dir / "processed"
        self.output_dir = self.base_dir / "output"

    def raw_path(
        self, year: int, ticker_safe: str, report_type: str, period: str
    ) -> Path:
        """Path for a raw downloaded report PDF."""
        return self.raw_dir / str(year) / f"{ticker_safe}_{report_type}_{period}.pdf"

    def processed_path(
        self, year: int, ticker_safe: str, report_type: str, period: str
    ) -> Path:
        """Path for a processed markdown file."""
        return (
            self.processed_dir
            / str(year)
            / f"{ticker_safe}_{report_type}_{period}.md"
        )

    def output_path(self, ticker_safe: str, period: str) -> Path:
        """Path for structured metrics output JSON."""
        return self.output_dir / ticker_safe / period / "metrics.json"

    def output_jsonl_path(self, year: int) -> Path:
        """Path for the per-year metrics JSONL (one record per line,
        across all banks)."""
        return self.output_dir / f"metrics-{year}.jsonl"

    def raw_exists(
        self, year: int, ticker_safe: str, report_type: str, period: str
    ) -> bool:
        """Check if a raw report has already been downloaded."""
        return self.raw_path(year, ticker_safe, report_type, period).exists()

    def ensure_dirs(self) -> None:
        """Create all top-level data directories."""
        for d in [self.raw_dir, self.processed_dir, self.output_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def list_raw_reports(self) -> list[Path]:
        """List all downloaded raw report PDFs."""
        if not self.raw_dir.exists():
            return []
        return sorted(self.raw_dir.rglob("*.pdf"))

    def list_raw_reports_for_year(self, year: int) -> list[Path]:
        """List raw reports for a specific year."""
        year_dir = self.raw_dir / str(year)
        if not year_dir.exists():
            return []
        return sorted(year_dir.glob("*.pdf"))

    def raw_size_summary(self) -> dict[str, int]:
        """Return count and total size of raw reports per year."""
        summary: dict[str, int] = {}
        if not self.raw_dir.exists():
            return summary
        for year_dir in sorted(self.raw_dir.iterdir()):
            if year_dir.is_dir():
                files = list(year_dir.glob("*.pdf"))
                total_bytes = sum(f.stat().st_size for f in files)
                summary[year_dir.name] = total_bytes
        return summary
