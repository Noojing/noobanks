# NooBanks

Automated pipeline for **fetching**, **parsing**, and **AI-extracting** structured financial metrics from global bank reports.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the full system design, data flow, package structure, and future trading-engine integration.

## Quick Start

```bash
# Install dependencies
uv sync

# Run the CLI
noobanks --help
```

## CLI Commands

### `noobanks fetch` — Download reports

```bash
# Fetch one bank's annual report
noobanks fetch bank BARC.L --year 2025 --type annual_report

# Fetch all configured banks
noobanks fetch all --year 2025 --type annual_report

# Filter by market
noobanks fetch all --market US --year 2025
```

### `noobanks parse` — Convert PDFs to markdown

```bash
# Parse one bank's downloaded PDF
noobanks parse bank BARC.L --year 2025

# Parse all downloaded reports (parallel)
noobanks parse all --year 2025 --max-workers 4
```

### `noobanks extract` — AI-powered metric extraction

```bash
# Extract metrics for one bank
noobanks extract bank BARC.L --year 2025

# Extract for all banks
noobanks extract all --year 2025 --market US
```

### `noobanks list` — Inspect configured banks and downloaded data

```bash
# List all configured banks
noobanks list banks

# List downloaded reports
noobanks list reports --year 2025
```

## debug via vscode

```bash
# uv run python -m debugpy --wait-for-client --listen 0.0.0.0:5678 -m noobanks [command]
```

## Configuration

| File | Purpose |
|------|---------|
| `config/banks.yaml` | Bank registry (ticker, CIK, IR URLs, filing types) |
| `config/metrics.yaml` | Metric extraction specs (keywords, descriptions) |
| `config/pipeline.yaml` | LLM backend selection (provider, model, API key env var) |

## Supported Banks

24+ banks across 4 markets:

- **US** — JPM, BAC, C, WFC, GS, MS (NYSE, SEC EDGAR)
- **CN** — ICBC, CCB, ABC, BOC, BoCom, PSBC, CMB, Ping An (SSE/SZSE)
- **HK** — HSBC, BOCHK, Hang Seng, StanC, BEA, Dah Sing (HKEX)
- **UK** — HSBA.L, BARC.L, LLOY.L, NWG.L, STAN.L (LSE)

## Output

Extracted metrics are stored as JSONL files under `~/.noobanks/data/output/`:

```
output/metrics-2025.jsonl   # One record per line, all banks for a year
```

Each record contains the bank identity and a `metrics` dict keyed by metric name.

## Testing

```bash
uv run pytest tests/ -q --cov=noobanks
```

CI runs on Python 3.14 via GitHub Actions (`.github/workflows/ci.yml`).

## TODO
- parallelise search of multiple reports if needed for the same bank
- validate doc by reading first few pages
- disable ddgs by default
- gs: js page nav
- loyds: failed in fetching contents

### Metrics:
- Allowance Coverage Ratio: Loan Loss Reserves / Non-Performing Loans × 100%)
- Distributed Dividend Rate: Distributed Dividend / Net Income × 100%