# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

NooBanks — an automated pipeline for fetching, parsing, and AI-extracting structured metrics from global bank financial reports. See [docs/architecture.md](docs/architecture.md) for the full system design (data sourcing → document processing → AI extraction → storage, with a future trading engine).

The project is in **early design/implementation phase**: the architecture is defined, configs are populated, but most Python modules do not exist yet.

## Build & run

```bash
uv build              # build the package
uv run noobanks       # run the CLI entry point (currently prints "Hello from noobanks!")
uv run --with pyyaml python -c "import yaml; ..."  # ad-hoc scripts with extra deps
```

Python **3.14+** is required (see `.python-version`). All dependencies are managed with `uv`.

## Architecture

**Four-layer pipeline** (see `docs/architecture.md` diagram):

| Layer | Purpose | Key abstractions |
|-------|---------|-----------------|
| **Data Sourcing** | Fetch reports from IR pages / SEC EDGAR | `SourceAdapter` ABC → `EdgarAdapter`, `RssAdapter`, `GenericAdapter` |
| **Document Processing** | PDF/HTML/XBRL → clean markdown chunks | `DocumentParser` (format router), `TextChunker` |
| **AI Extraction** | LLM-driven metric extraction per report | `LlmClient` (provider-agnostic), `MetricExtractor`, `ResultAggregator` |
| **Storage** | Raw PDFs → processed text → structured JSON | `ReportStore`, directory hierarchy under `src/data/` |

**Package layout** (`src/noobanks/`) mirrors these layers: `config/`, `sources/`, `processing/`, `extraction/`, `storage/`, plus `cli.py` (typer entry point).

## Config files

- **`config/banks.yaml`** — 25 banks across US (6), China Mainland (8), Hong Kong (6), UK (5). Each entry: `ticker`, `exchange`, `market`, `cik` (US only), `sources` (edgar + investor_relations URL), `filings`. This is the **authoritative bank registry**.
- **`config/metrics.yaml`** and **`config/pipeline.yaml`** — not yet created (defined in architecture, pending implementation).

## Data directory

```
src/data/
├── raw/           # Downloaded PDFs organized by year: raw/{YYYY}/{TICKER}_{report_type}_{period}.pdf
│   └── 2025/
│       ├── BAC_10-K_FY.pdf
│       ├── JPM_10-K_FY.pdf
│       └── ...
├── processed/     # Parsed markdown per report (mirrors raw/ hierarchy)
└── output/        # Structured metrics JSON per bank/period
```

**Naming conventions**:
- `{year}` — 4-digit year, used as the **subfolder** name
- `{ticker}` — bank ticker, dots → underscores (e.g. `BARC_L`, `601398_SH`)
- `{report_type}` — `10-K`, `10-Q`, `annual_report`, `interim_report`, `quarterly_report`
- `{period}` — `FY` (full year), `Q1`–`Q4` (quarterly), `H1`/`H2` (half-year)

## Subagent

- **`.claude/agents/report-fetcher.md`** — a project-level agent type (`report-fetcher`) that downloads financial reports from banks' official IR pages. Uses `curl` + `grep` for zero-token URL discovery, with WebSearch as last-resort fallback. Target model: `haiku`. Available after session restart.

## Superpowers configuration

- **Plan location**: Save superpowers plans to `~/.claude/plans/superpowers/` (overrides the default `docs/superpowers/plans/`)

## Key conventions

- **Naming**: bank tickers use underscores instead of dots in filenames (e.g. `BARC_L`, `601398_SH`). Raw reports live in year subfolders: `raw/{YYYY}/{TICKER}_{report_type}_{period}.pdf`. Period is `FY` for annual, `Q1`–`Q4` for quarterly.
- **Report fetching**: always prefer banks' own IR pages over third-party sources. Use HEAD requests to verify URLs before downloading full PDFs.
- **US banks**: CIK numbers are stored in `banks.yaml` for SEC EDGAR access; IR pages are supplementary.
- **Commits**: format as `FU-{YYMMDD}:{description}` (e.g. `FU-260809: add config for list of banks`).
