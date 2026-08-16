"""CLI entry point for NooBanks — typer-based command-line interface."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from noobanks.config.loader import load_bank_registry, load_metric_specs
from noobanks.extraction.aggregator import ResultAggregator
from noobanks.extraction.extractor import MetricExtractor
from noobanks.processing.parser import markdown_to_pages, parse_to_markdown
from noobanks.sources.generic import GenericIrAdapter
from noobanks.storage import ReportStore
from noobanks.storage.store import DEFAULT_DATA_DIR

app = typer.Typer(
    name="noobanks",
    help="Automated pipeline for fetching and analyzing global bank financial reports.",
    no_args_is_help=True,
)
console = Console()

# Log-level lookup: -v count → level
_LEVELS = [logging.WARNING, logging.INFO, logging.DEBUG]


@app.callback()
def _global(
    verbose: int = typer.Option(
        0, "--verbose", "-v",
        count=True,
        help="Increase log verbosity: -v for INFO, -vv for DEBUG",
        show_default=False,
    ),
) -> None:
    """Global options applied before any subcommand runs."""
    level = _LEVELS[min(verbose, len(_LEVELS) - 1)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        force=True,  # re-configure even if already called
    )


# Subcommand groups
fetch_app = typer.Typer(help="Download financial reports from bank IR pages.")
list_app = typer.Typer(help="List configured banks or downloaded reports.")
app.add_typer(fetch_app, name="fetch")
app.add_typer(list_app, name="list")


# ── helper ────────────────────────────────────────────────────────────────

def _period_label(report_type: str, period: str) -> str:
    """Human-readable label for a report type + period combo."""
    if period == "FY":
        return f"{report_type} (Full Year)"
    return f"{report_type} ({period})"

def _run_async(coro):
    """Run an async coroutine from a sync typer callback."""
    return asyncio.run(coro)


# ── fetch ──────────────────────────────────────────────────────────────────

@fetch_app.command(name="bank")
def fetch_bank(
    ticker: str = typer.Argument(..., help="Bank ticker (e.g. BARC.L, JPM, 601398.SH)"),
    report_type: str = typer.Option(
        "annual_report", "--type", "-t",
        help="Report type: annual_report, 10-K, 10-Q, interim_report, quarterly_report, pillar3",
    ),
    year: int = typer.Option(2025, "--year", "-y", help="Fiscal year of the report"),
    period: str = typer.Option("FY", "--period", "-p", help="Period: FY, Q1-Q4, H1, H2"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download even if exists"),
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, "--data-dir", "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    ),
) -> None:
    """Download a financial report for a specific bank."""
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        console.print(f"Use [bold]noobanks list banks[/bold] to see available tickers.")
        raise typer.Exit(1)

    console.print(f"Fetching [bold]{bank.name}[/bold] ({bank.ticker}): "
                  f"{_period_label(report_type, period)} {year}")

    ReportStore(data_dir).ensure_dirs()
    adapter = GenericIrAdapter(data_dir=data_dir)
    result = _run_async(adapter.fetch(bank, report_type, year, period, force=force))

    if result.reports:
        for r in result.reports:
            console.print(
                f"  [green]✓[/green] {r.filename} ({r.size_mb:.1f} MB) → {r.local_path}"
            )
    if result.errors:
        for e in result.errors:
            console.print(f"  [red]✗[/red] {e}")
    if not result.reports and not result.errors:
        console.print("  [yellow]No results[/yellow]")


@fetch_app.command(name="all")
def fetch_all(
    report_type: str = typer.Option(
        "annual_report", "--type", "-t",
        help="Report type to download for all banks",
    ),
    year: int = typer.Option(2025, "--year", "-y", help="Fiscal year"),
    period: str = typer.Option("FY", "--period", "-p", help="Period"),
    market: Optional[str] = typer.Option(
        None, "--market", "-m", help="Filter by market: US, CN, HK, UK"
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Re-download existing"),
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, "--data-dir", "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    ),
) -> None:
    """Download reports for all configured banks."""
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)

    console.print(f"Fetching [bold]{_period_label(report_type, period)} {year}[/bold] "
                  f"for {len(banks)} banks...")
    ReportStore(data_dir).ensure_dirs()
    adapter = GenericIrAdapter(data_dir=data_dir)

    async def _fetch_all():
        succeeded, failed = [], []
        for i, bank in enumerate(banks):
            console.print(f"  [{i+1}/{len(banks)}] {bank.ticker} ({bank.name})...")
            result = await adapter.fetch(bank, report_type, year, period, force=force)
            if result.reports:
                succeeded.append(result)
                for r in result.reports:
                    console.print(f"    [green]✓[/green] {r.filename} ({r.size_mb:.1f} MB)")
            else:
                failed.append(result)
                for e in result.errors:
                    console.print(f"    [red]✗[/red] {e}")
            if i < len(banks) - 1:
                await asyncio.sleep(2)  # inter-bank cooldown
        return succeeded, failed

    succeeded, failed = _run_async(_fetch_all())
    console.print(f"\n[bold]Summary:[/bold] {len(succeeded)} succeeded, {len(failed)} failed")


# ── parse ──────────────────────────────────────────────────────────────────

parse_app = typer.Typer(help="Convert raw PDFs into processed markdown.")
app.add_typer(parse_app, name="parse")


@parse_app.command(name="bank")
def parse_bank(
    ticker: str = typer.Argument(..., help="Bank ticker (e.g. BARC.L, 601398.SH)"),
    year: int = typer.Option(2025, "--year", "-y", help="Fiscal year"),
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, "--data-dir", "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    ),
) -> None:
    """Convert a downloaded annual-report PDF into processed markdown."""
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        raise typer.Exit(1)

    store = ReportStore(data_dir)
    pdf_path = store.raw_path(year, bank.ticker_safe, "annual_report", "FY")
    if not pdf_path.exists():
        console.print(f"[red]Report not downloaded:[/red] {pdf_path}")
        console.print(f"Run `noobanks fetch bank {ticker} --year {year}` first.")
        raise typer.Exit(1)

    console.print(f"Parsing [bold]{bank.name}[/bold] FY{year} PDF → markdown...")
    markdown = parse_to_markdown(pdf_path)
    md_path = store.processed_path(year, bank.ticker_safe, "annual_report", "FY")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    console.print(f"  ✓ {md_path} ({len(markdown):,} chars)")


# ── extract ────────────────────────────────────────────────────────────────

extract_app = typer.Typer(help="Extract financial metrics from downloaded reports.")
app.add_typer(extract_app, name="extract")


@extract_app.command(name="bank")
def extract_bank(
    ticker: str = typer.Argument(..., help="Bank ticker (e.g. BARC.L, 601398.SH)"),
    year: int = typer.Option(2025, "--year", "-y", help="Fiscal year"),
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, "--data-dir", "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    ),
    force: bool = typer.Option(
        False, "--force", "-f",
        help="Re-extract even if the bank already has a record for this year",
    ),
) -> None:
    """Extract metrics from processed markdown into the per-year JSONL."""
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        raise typer.Exit(1)

    specs = load_metric_specs()
    store = ReportStore(data_dir)
    md_path = store.processed_path(year, bank.ticker_safe, "annual_report", "FY")
    if not md_path.exists():
        console.print(f"[red]Report not parsed:[/red] {md_path}")
        console.print(f"Run `noobanks parse bank {ticker} --year {year}` first.")
        raise typer.Exit(1)

    aggregator = ResultAggregator(extractor=MetricExtractor())
    out_path = store.output_jsonl_path(year)

    # Skip unless forced: one record per bank per year.
    if not force and aggregator.has_bank_record(out_path, bank.ticker_safe, year):
        console.print(
            f"[yellow]Already extracted[/yellow] {bank.ticker} FY{year} "
            f"(use --force to re-extract)."
        )
        raise typer.Exit(0)

    console.print(f"Extracting metrics for [bold]{bank.name}[/bold] FY{year}...")
    markdown = md_path.read_text(encoding="utf-8")
    pages = markdown_to_pages(markdown)
    console.print(f"  Loaded {len(pages)} page chunks from {md_path.name}")

    async def _run():
        return await aggregator.extract_all(specs, pages, year)

    results = _run_async(_run())

    records = aggregator.records_for_bank(
        bank.ticker_safe, bank.name, year, results
    )
    aggregator.append_jsonl(
        records, out_path,
        replace_bank=bank.ticker_safe, replace_year=year,
    )

    for name, r in results.items():
        if "error" in r:
            console.print(f"  [red]✗[/red] {name}: {r['error']}")
        else:
            console.print(f"  [green]✓[/green] {name}: {r['value']} {r.get('unit') or ''}")
    console.print(f"\nWrote {len(records)} record(s) to {out_path}")


# ── list ───────────────────────────────────────────────────────────────────

@list_app.command(name="banks")
def list_banks(
    market: Optional[str] = typer.Option(
        None, "--market", "-m", help="Filter by market: US, CN, HK, UK"
    ),
) -> None:
    """List all configured banks."""
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)

    table = Table(title=f"Configured Banks ({len(banks)})")
    table.add_column("Ticker", style="cyan")
    table.add_column("Name")
    table.add_column("Market", style="dim")
    table.add_column("Exchange", style="dim")
    table.add_column("Filings", style="dim")

    for bank in banks:
        table.add_row(
            bank.ticker,
            bank.name,
            bank.market,
            bank.exchange,
            ", ".join(bank.filings),
        )

    console.print(table)


@list_app.command(name="reports")
def list_reports(
    year: Optional[int] = typer.Option(None, "--year", "-y", help="Filter by year"),
    data_dir: Path = typer.Option(
        DEFAULT_DATA_DIR, "--data-dir", "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    ),
) -> None:
    """List downloaded raw reports."""
    store = ReportStore(data_dir)
    if year:
        paths = store.list_raw_reports_for_year(year)
    else:
        paths = store.list_raw_reports()

    if not paths:
        console.print("[dim]No reports downloaded yet.[/dim]")
        return

    table = Table(title=f"Downloaded Reports ({len(paths)})")
    table.add_column("File", style="cyan")
    table.add_column("Size", style="dim")
    table.add_column("Year", style="dim")

    total_bytes = 0
    for p in paths:
        size = p.stat().st_size
        total_bytes += size
        year_str = p.parent.name
        table.add_row(p.name, f"{size / (1024*1024):.1f} MB", year_str)

    console.print(table)
    console.print(f"[dim]Total: {len(paths)} files, {total_bytes / (1024*1024):.1f} MB[/dim]")


# ── main entry point ──────────────────────────────────────────────────────

def main() -> None:
    """Entry point for the noobanks CLI."""
    app()
