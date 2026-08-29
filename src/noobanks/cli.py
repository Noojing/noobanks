"""CLI entry point for NooBanks — typer-based command-line interface."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from noobanks.config.loader import load_bank_registry, load_metric_specs
from noobanks.config.models import BankSpec
from noobanks.extraction.aggregator import ResultAggregator
from noobanks.extraction.extractor import MetricExtractor
from noobanks.processing.parser import (
    count_tokens,
    markdown_to_pages,
    parse_to_markdown,
)
from noobanks.sources import CompositeAdapter, DdgsAdapter, IrAdapter
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
        0,
        "--verbose",
        "-v",
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


@contextmanager
def _timed(description: str):
    """Context manager that logs elapsed wall-clock time on exit."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        console.print(f"  [dim]⏱ {description}: {elapsed:.1f}s[/dim]")


def _period_label(report_type: str, period: str) -> str:
    """Human-readable label for a report type + period combo."""
    if period == "FY":
        return f"{report_type} (Full Year)"
    return f"{report_type} ({period})"


def _run_async(coro):
    """Run an async coroutine from a sync typer callback."""
    return asyncio.run(coro)


def _resolve_filings(
    bank: BankSpec,
    year: Optional[int],
    report_type: Optional[str],
    period: Optional[str],
) -> tuple[int, list[tuple[str, str]]]:
    """Resolve user input into concrete (year, report_type, period) specs.

    Resolution rules:
    - *year* not specified → last year (current year − 1)
    - *report_type* not specified → all report types defined on the bank
    - *period* not specified → all applicable periods for each type
    """
    resolved_year = year if year is not None else datetime.datetime.now().year - 1
    specs = bank.filing_specs(report_type=report_type, period=period)
    return resolved_year, specs


# ── shared CLI option definitions ──────────────────────────────────────────


def _opt_year(default=None, help="Fiscal year. If omitted, defaults to last year."):
    return typer.Option(default, "--year", "-y", help=help)


def _opt_data_dir():
    return typer.Option(
        DEFAULT_DATA_DIR,
        "--data-dir",
        "-d",
        help="Base data directory (default: ~/.noobanks/data)",
    )


def _opt_report_type(help):
    return typer.Option(None, "--type", "-t", help=help)


def _opt_period(help):
    return typer.Option(None, "--period", "-p", help=help)


def _opt_force(help):
    return typer.Option(False, "--force", "-f", help=help)


def _opt_market():
    return typer.Option(None, "--market", "-m", help="Filter by market: US, CN, HK, UK")


def _arg_ticker(help="Bank ticker (e.g. BARC.L, JPM, 601398.SH)"):
    return typer.Argument(..., help=help)


def _opt_ddgs_fallback():
    return typer.Option(
        False,
        "--ddgs-fallback",
        help="Use DuckDuckGo web search as a fallback when IR adapter fails",
    )


def _opt_max_concurrent(help, default=5, flag="--concurrency", short="-c"):
    return typer.Option(default, flag, short, help=help)


# ── fetch ──────────────────────────────────────────────────────────────────


@fetch_app.command(name="bank")
def fetch_bank(
    ticker: str = _arg_ticker(),
    year: Optional[int] = _opt_year(),
    report_type: Optional[str] = _opt_report_type(
        "Report type (e.g. annual_report, 10-K, 10-Q). If omitted, fetches all types.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type (e.g. FY, Q1-Q4, H1). If omitted, fetches all applicable periods.",
    ),
    force: bool = _opt_force("Re-download even if exists"),
    data_dir: Path = _opt_data_dir(),
    ddgs_fallback: bool = _opt_ddgs_fallback(),
) -> None:
    """Download financial reports for a specific bank.

    Resolution logic:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, fetches all report types defined for the bank.
    • If --period is omitted, fetches all applicable periods for each type.
    """
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        console.print(f"Use [bold]noobanks list banks[/bold] to see available tickers.")
        raise typer.Exit(1)

    resolved_year, specs = _resolve_filings(bank, year, report_type, period)
    if not specs:
        console.print(f"[yellow]No filings configured for {bank.ticker}[/yellow]")
        console.print(f"Use [bold]noobanks list banks[/bold] to see available types.")
        raise typer.Exit(1)

    console.print(
        f"Fetching [bold]{bank.name}[/bold] ({bank.ticker}) "
        f"for {len(specs)} report(s) in {resolved_year}:"
    )
    for rtype, p in specs:
        console.print(f"  • {_period_label(rtype, p)} {resolved_year}")

    ReportStore(data_dir).ensure_dirs()

    async def _fetch_all_specs():
        _adapters = [IrAdapter(data_dir=data_dir)]
        if ddgs_fallback:
            _adapters.append(DdgsAdapter(data_dir=data_dir))
        succeeded, failed = [], []
        async with CompositeAdapter(
            adapters=_adapters,
            data_dir=data_dir,
        ) as adapter:
            with _timed(f"Fetch {bank.ticker} ({len(specs)} report(s))"):
                results = await adapter.fetch(
                    bank,
                    resolved_year,
                    specs,
                    force=force,
                )
            for result in results:
                if result.report:
                    succeeded.append(result)
                    console.print(
                        f"  [green]✓[/green] {result.report.filename} "
                        f"({result.report.size_mb:.1f} MB) "
                        f"from [dim]{result.report.downloaded_from}[/dim] "
                        f"→ {result.report.local_path}"
                    )
                if result.error:
                    failed.append(result)
                    console.print(
                        f"  [red]✗[/red] {result.report_type} {result.period}: {result.error}"
                    )
        return succeeded, failed

    succeeded, failed = _run_async(_fetch_all_specs())
    if len(specs) > 1:
        console.print(
            f"\n[bold]Summary:[/bold] {len(succeeded)} succeeded, {len(failed)} failed"
        )


@fetch_app.command(name="all")
def fetch_all(
    year: Optional[int] = _opt_year(),
    report_type: Optional[str] = _opt_report_type(
        "Report type to download. If omitted, fetches all types for each bank.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type. If omitted, fetches all applicable periods.",
    ),
    market: Optional[str] = _opt_market(),
    force: bool = _opt_force("Re-download existing"),
    data_dir: Path = _opt_data_dir(),
    max_concurrent: int = _opt_max_concurrent("Max concurrent bank fetches"),
    ddgs_fallback: bool = _opt_ddgs_fallback(),
) -> None:
    """Download reports for all configured banks.

    Resolution logic is applied per bank:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, fetches all report types defined for each bank.
    • If --period is omitted, fetches all applicable periods for each type.
    """
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)
    resolved_year = year if year is not None else datetime.datetime.now().year - 1

    console.print(
        f"Fetching reports for {len(banks)} banks in {resolved_year} "
        f"(up to {max_concurrent} concurrent)..."
    )
    ReportStore(data_dir).ensure_dirs()

    async def _fetch_all():
        _adapters = [IrAdapter(data_dir=data_dir)]
        if ddgs_fallback:
            _adapters.append(DdgsAdapter(data_dir=data_dir))
        async with CompositeAdapter(
            adapters=_adapters,
            data_dir=data_dir,
        ) as adapter:
            sem = asyncio.Semaphore(max_concurrent)
            succeeded, failed = [], []

            async def _fetch_one(bank: BankSpec):
                async with sem:
                    _, specs = _resolve_filings(bank, year, report_type, period)
                    bank_results = await adapter.fetch(
                        bank,
                        resolved_year,
                        specs,
                        force=force,
                    )
                    bank_ok = [r for r in bank_results if r.report]
                    bank_fail = [r for r in bank_results if r.error]
                    return bank, bank_ok, bank_fail

            tasks = [_fetch_one(bank) for bank in banks]
            completed = 0
            total = len(banks)

            for coro in asyncio.as_completed(tasks):
                bank, bank_ok, bank_fail = await coro
                completed += 1
                succeeded.extend(bank_ok)
                failed.extend(bank_fail)
                prefix = f"  [{completed}/{total}] {bank.ticker} ({bank.name})"
                if bank_ok:
                    console.print(
                        f"{prefix} [green]✓[/green] "
                        f"{len(bank_ok)} report(s) downloaded"
                    )
                if bank_fail:
                    console.print(
                        f"{prefix} [red]✗[/red] " f"{len(bank_fail)} report(s) failed"
                    )
                if not bank_ok and not bank_fail:
                    console.print(f"{prefix} [dim]no filings configured[/dim]")

        return succeeded, failed

    with _timed(f"Fetch all {len(banks)} banks"):
        succeeded, failed = _run_async(_fetch_all())
    console.print(
        f"\n[bold]Summary:[/bold] {len(succeeded)} succeeded, {len(failed)} failed"
    )


# ── parse ──────────────────────────────────────────────────────────────────

parse_app = typer.Typer(help="Convert raw PDFs into processed markdown.")
app.add_typer(parse_app, name="parse")


def _default_parse_workers() -> int:
    """Default concurrency for `parse all`: half the CPU cores (min 1)."""
    return max(1, (os.cpu_count() or 2) // 2)


def _parse_one(
    store: ReportStore,
    bank: BankSpec,
    year: int,
    report_type: str,
    period: str,
    force: bool,
) -> tuple[bool, str]:
    """Parse one bank's raw PDF into processed markdown.

    Returns (ok, message). Skips when the processed file exists unless
    force is set.
    """
    pdf_path = store.raw_path(year, bank.ticker_safe, report_type, period)
    if not pdf_path.exists():
        return False, f"report not downloaded ({pdf_path})"

    md_path = store.processed_path(year, bank.ticker_safe, report_type, period)
    if md_path.exists() and not force:
        return True, f"already parsed ({md_path})"

    markdown = parse_to_markdown(pdf_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8")
    return (
        True,
        f"{md_path} ({len(markdown):,} chars, {count_tokens(markdown):,} tokens)",
    )


@parse_app.command(name="bank")
def parse_bank(
    ticker: str = _arg_ticker(),
    year: Optional[int] = _opt_year(),
    report_type: Optional[str] = _opt_report_type(
        "Report type (e.g. annual_report, 10-K, 10-Q). If omitted, parses all types.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type (e.g. FY, Q1-Q4, H1). If omitted, parses all applicable periods.",
    ),
    data_dir: Path = _opt_data_dir(),
    force: bool = _opt_force("Re-parse even if already processed"),
) -> None:
    """Convert downloaded report PDFs into processed markdown.

    Resolution logic:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, parses all report types defined for the bank.
    • If --period is omitted, parses all applicable periods for each type.
    """
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        raise typer.Exit(1)

    resolved_year, specs = _resolve_filings(bank, year, report_type, period)
    if not specs:
        console.print(f"[yellow]No filings configured for {bank.ticker}[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"Parsing [bold]{bank.name}[/bold] ({bank.ticker}) "
        f"for {len(specs)} report(s) in {resolved_year}:"
    )
    for rtype, p in specs:
        console.print(f"  • {_period_label(rtype, p)} {resolved_year}")

    store = ReportStore(data_dir)
    parsed, skipped, failed = [], [], []

    with _timed(f"Parse {bank.ticker} ({len(specs)} report(s))"):
        for rtype, p in specs:
            ok, message = _parse_one(store, bank, resolved_year, rtype, p, force)
            if not ok:
                failed.append((rtype, p))
                console.print(f"  [red]✗[/red] {_period_label(rtype, p)}: {message}")
            elif "already parsed" in message:
                skipped.append((rtype, p))
                console.print(f"  [dim]•[/dim] {_period_label(rtype, p)}: {message}")
            else:
                parsed.append((rtype, p))
                console.print(
                    f"  [green]✓[/green] {_period_label(rtype, p)}: {message}"
                )

    if len(specs) > 1:
        console.print(
            f"\n[bold]Summary:[/bold] {len(parsed)} parsed, "
            f"{len(skipped)} skipped, {len(failed)} failed"
        )


@parse_app.command(name="all")
def parse_all(
    year: Optional[int] = _opt_year(),
    report_type: Optional[str] = _opt_report_type(
        "Report type to parse. If omitted, parses all types for each bank.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type. If omitted, parses all applicable periods.",
    ),
    data_dir: Path = _opt_data_dir(),
    market: Optional[str] = _opt_market(),
    force: bool = _opt_force("Re-parse even if already processed"),
    max_workers: Optional[int] = _opt_max_concurrent(
        "Max parallel parses (default: half of CPU cores)",
        default=None,
        flag="--max-workers",
        short="-w",
    ),
) -> None:
    """Parse every available downloaded report into processed markdown.

    Resolution logic is applied per bank:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, parses all report types defined for each bank.
    • If --period is omitted, parses all applicable periods for each type.
    """
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)
    workers = max_workers or _default_parse_workers()
    resolved_year = year if year is not None else datetime.datetime.now().year - 1

    tasks: list[tuple[BankSpec, str, str]] = []
    for bank in banks:
        _, specs = _resolve_filings(bank, year, report_type, period)
        if not specs:
            console.print(f"  [dim]{bank.ticker}: no filings configured[/dim]")
            continue
        for rtype, p in specs:
            tasks.append((bank, rtype, p))

    if not tasks:
        console.print("[yellow]No filings configured for any bank.[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"Parsing {len(tasks)} reports for {len(banks)} banks in {resolved_year} "
        f"(up to {workers} in parallel)..."
    )

    store = ReportStore(data_dir)
    parsed, skipped, failed = [], [], []

    with _timed(f"Parse all {len(banks)} banks"):
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _parse_one, store, bank, resolved_year, rtype, p, force
                ): (bank, rtype, p)
                for bank, rtype, p in tasks
            }
            for future in as_completed(futures):
                bank, rtype, p = futures[future]
                ok, message = future.result()
                label = f"{bank.ticker} {_period_label(rtype, p)}"
                if not ok:
                    failed.append(bank.ticker)
                    console.print(f"  [red]✗[/red] {label}: {message}")
                elif "already parsed" in message:
                    skipped.append(bank.ticker)
                    console.print(f"  [dim]•[/dim] {label}: {message}")
                else:
                    parsed.append(bank.ticker)
                    console.print(f"  [green]✓[/green] {label}: {message}")

    console.print(
        f"\n[bold]Summary:[/bold] {len(parsed)} parsed, "
        f"{len(skipped)} skipped, {len(failed)} failed"
    )


# ── extract ────────────────────────────────────────────────────────────────

extract_app = typer.Typer(help="Extract financial metrics from downloaded reports.")
app.add_typer(extract_app, name="extract")


async def _extract_one(
    store: ReportStore,
    bank: BankSpec,
    year: int,
    report_type: str,
    period: str,
    force: bool,
    show_metrics: bool = False,
) -> tuple[bool, str]:
    """Extract metrics for one bank/report from its processed markdown.

    Returns (ok, message). Skips when the bank already has a jsonl record
    for the year unless force is set. When show_metrics is True, prints a
    per-metric result line (used by the single-bank command).
    """
    md_path = store.processed_path(year, bank.ticker_safe, report_type, period)
    if not md_path.exists():
        return False, f"report not parsed ({md_path})"

    extractor = MetricExtractor()
    aggregator = ResultAggregator(extractor=extractor)
    out_path = store.output_jsonl_path(year)

    if not force and aggregator.has_bank_record(out_path, bank.ticker_safe, year):
        return True, "already extracted"

    specs = load_metric_specs()
    markdown = md_path.read_text(encoding="utf-8")
    pages = markdown_to_pages(markdown)

    results = await aggregator.extract_all(specs, pages, year)

    records = aggregator.records_for_bank(bank.ticker_safe, bank.name, year, results)
    aggregator.append_jsonl(
        records,
        out_path,
        replace_bank=bank.ticker_safe,
        replace_year=year,
    )

    if show_metrics:
        for name, r in results.items():
            if "error" in r:
                console.print(f"    [red]✗[/red] {name}: {r['error']}")
            else:
                console.print(
                    f"    [green]✓[/green] {name}: {r['value']} {r.get('unit') or ''}"
                )

    usage_note = ""
    usage = extractor.total_usage
    if usage is not None:
        usage_note = (
            f" (LLM: {usage.input_tokens:,} prompt / {usage.output_tokens:,} "
            f"completion tokens)"
        )

    return True, f"{len(records)} record(s) → {out_path}{usage_note}"


@extract_app.command(name="bank")
def extract_bank(
    ticker: str = _arg_ticker(),
    year: Optional[int] = _opt_year(
        help="Fiscal year. If omitted, defaults to last year."
    ),
    report_type: Optional[str] = _opt_report_type(
        "Report type (e.g. annual_report, 10-K, 10-Q). If omitted, extracts all types.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type (e.g. FY, Q1-Q4, H1). If omitted, extracts all applicable periods.",
    ),
    data_dir: Path = _opt_data_dir(),
    force: bool = _opt_force(
        "Re-extract even if the bank already has a record for this year"
    ),
) -> None:
    """Extract metrics from processed markdown into the per-year JSONL.

    Resolution logic:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, extracts all report types defined for the bank.
    • If --period is omitted, extracts all applicable periods for each type.
    """
    registry = load_bank_registry()
    bank = registry.find(ticker)
    if bank is None:
        console.print(f"[red]Bank not found:[/red] {ticker}")
        raise typer.Exit(1)

    resolved_year, specs = _resolve_filings(bank, year, report_type, period)
    if not specs:
        console.print(f"[yellow]No filings configured for {bank.ticker}[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"Extracting [bold]{bank.name}[/bold] ({bank.ticker}) "
        f"for {len(specs)} report(s) in {resolved_year}:"
    )
    for rtype, p in specs:
        console.print(f"  • {_period_label(rtype, p)} {resolved_year}")

    store = ReportStore(data_dir)
    extracted, skipped, failed = [], [], []

    with _timed(f"Extract {bank.ticker} ({len(specs)} report(s))"):
        for rtype, p in specs:
            ok, message = _run_async(
                _extract_one(
                    store, bank, resolved_year, rtype, p, force, show_metrics=True
                )
            )
            if not ok:
                failed.append((rtype, p))
                console.print(f"  [red]✗[/red] {_period_label(rtype, p)}: {message}")
            elif message == "already extracted":
                skipped.append((rtype, p))
                console.print(f"  [dim]•[/dim] {_period_label(rtype, p)}: {message}")
            else:
                extracted.append((rtype, p))
                console.print(
                    f"  [green]✓[/green] {_period_label(rtype, p)}: {message}"
                )

    if len(specs) > 1:
        console.print(
            f"\n[bold]Summary:[/bold] {len(extracted)} extracted, "
            f"{len(skipped)} skipped, {len(failed)} failed"
        )


@extract_app.command(name="all")
def extract_all(
    year: Optional[int] = _opt_year(
        help="Fiscal year. If omitted, defaults to last year."
    ),
    report_type: Optional[str] = _opt_report_type(
        "Report type to extract. If omitted, extracts all types for each bank.",
    ),
    period: Optional[str] = _opt_period(
        "Period within type. If omitted, extracts all applicable periods.",
    ),
    data_dir: Path = _opt_data_dir(),
    market: Optional[str] = _opt_market(),
    force: bool = _opt_force(
        "Re-extract even if the bank already has a record for this year"
    ),
) -> None:
    """Extract metrics for every available parsed report into the JSONL.

    Resolution logic is applied per bank:
    \b
    • If --year is omitted, defaults to last year.
    • If --type is omitted, extracts all report types defined for each bank.
    • If --period is omitted, extracts all applicable periods for each type.
    """
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)
    store = ReportStore(data_dir)
    resolved_year = year if year is not None else datetime.datetime.now().year - 1

    tasks: list[tuple[BankSpec, str, str]] = []
    for bank in banks:
        _, specs = _resolve_filings(bank, year, report_type, period)
        if not specs:
            console.print(f"  [dim]{bank.ticker}: no filings configured[/dim]")
            continue
        for rtype, p in specs:
            tasks.append((bank, rtype, p))

    if not tasks:
        console.print("[yellow]No filings configured for any bank.[/yellow]")
        raise typer.Exit(1)

    console.print(
        f"Extracting {len(tasks)} reports for {len(banks)} banks in {resolved_year}..."
    )
    extracted, skipped, failed = [], [], []

    async def _extract_all():
        for i, (bank, rtype, p) in enumerate(tasks):
            ok, message = await _extract_one(
                store, bank, resolved_year, rtype, p, force
            )
            label = f"{bank.ticker} {_period_label(rtype, p)}"
            if not ok:
                failed.append(bank.ticker)
                console.print(f"  [red]✗[/red] {label}: {message}")
            elif message == "already extracted":
                skipped.append(bank.ticker)
                console.print(f"  [dim]•[/dim] {label}: {message}")
            else:
                extracted.append(bank.ticker)
                console.print(f"  [green]✓[/green] {label}: {message}")
            if i < len(tasks) - 1:
                await asyncio.sleep(2)  # inter-bank cooldown (LLM rate limits)
        return extracted, skipped, failed

    with _timed(f"Extract all {len(banks)} banks"):
        extracted, skipped, failed = _run_async(_extract_all())
    console.print(
        f"\n[bold]Summary:[/bold] {len(extracted)} extracted, "
        f"{len(skipped)} skipped, {len(failed)} failed"
    )


# ── list ───────────────────────────────────────────────────────────────────


@list_app.command(name="banks")
def list_banks(
    market: Optional[str] = _opt_market(),
) -> None:
    """List all configured banks."""
    registry = load_bank_registry()
    banks = registry.by_market(market) if market else list(registry.banks)

    with _timed("List banks"):
        table = Table(title=f"Configured Banks ({len(banks)})")
        table.add_column("Ticker", style="cyan")
        table.add_column("Name")
        table.add_column("Market", style="dim")
        table.add_column("Exchange", style="dim")
        table.add_column("Filings", style="dim")

        for bank in banks:
            filing_strs = [
                f"{rt}: [{', '.join(ps)}]" for rt, ps in bank.filings.items()
            ]
            table.add_row(
                bank.ticker,
                bank.name,
                bank.market,
                bank.exchange,
                "; ".join(filing_strs),
            )

    console.print(table)


@list_app.command(name="reports")
def list_reports(
    year: Optional[int] = _opt_year(help="Filter by year"),
    data_dir: Path = _opt_data_dir(),
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

    with _timed("List reports"):
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
    console.print(
        f"[dim]Total: {len(paths)} files, {total_bytes / (1024*1024):.1f} MB[/dim]"
    )


# ── main entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Entry point for the noobanks CLI."""
    app()
