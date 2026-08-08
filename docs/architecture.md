# NooBanks — Architecture

## System Overview

```mermaid
flowchart TB
    subgraph CONFIG["⚙️ Configuration"]
        direction LR
        banks["🏦 bank-defs.yaml\n(ticker, CIK, sources, filings)"]
        metrics["📏 metric-defs.yaml\n(name, prompt, schema, filters)"]
        pipeline_cfg["🔧 pipeline.yaml\n(schedule, retry, storage)"]
        prompts["💬 prompts/*.md\n(extraction templates)"]
    end

    subgraph SOURCE["📡 Data Sourcing (Layer 1)"]
        direction TB
        orch["Pipeline Orchestrator\n(scheduling, state tracking)"]
        fetcher["Report Fetcher\n(rate-limit, cache, retry)"]
        edgar["SEC EDGAR\nAdapter"]
        rss["RSS / IR Page\nAdapter"]
        generic["Custom URL\nAdapter"]
        orch --> fetcher
        fetcher --> edgar
        fetcher --> rss
        fetcher --> generic
    end

    subgraph PROCESS["📄 Document Processing (Layer 2)"]
        direction TB
        router["Format Router\n(detect: pdf / html / xbrl)"]
        pdf["PDF → Markdown\n(pymupdf / pdfplumber)"]
        html["HTML → Markdown\n(beautifulsoup / readability)"]
        xbrl["XBRL → Tables\n(xbrl parser)"]
        cleaner["Text Normalizer\n(dedup, trim, structure)"]
        chunker["Chunker\n(split for LLM context windows)"]
        router --> pdf
        router --> html
        router --> xbrl
        pdf --> cleaner
        html --> cleaner
        xbrl --> cleaner
        cleaner --> chunker
    end

    subgraph EXTRACT["🤖 AI Extraction (Layer 3)"]
        direction TB
        llm_abs["LLM Abstraction\n(provider-agnostic adapter)"]
        dispatcher["Metric Dispatcher\n(per-metric extraction jobs)"]
        validator["Schema Validator\n(json-schema / pydantic)"]
        aggregator["Result Aggregator\n(merge per-report metrics)"]
        dispatcher --> llm_abs
        llm_abs --> validator
        validator --> aggregator
    end

    subgraph STORE["💾 Storage (Layer 4)"]
        direction TB
        raw_fs["raw/\n(original PDFs, HTML)"]
        processed_fs["processed/\n(markdown, clean text)"]
        metrics_fs["output/\n(structured JSON)"]
    end

    subgraph FUTURE["📈 Trading Engine (Future)"]
        direction TB
        signal["Signal Generator"]
        backtest["Backtester"]
        risk["Risk Manager"]
    end

    CONFIG --> SOURCE
    SOURCE -->|"report files"| PROCESS
    PROCESS -->|"chunked markdown"| EXTRACT
    EXTRACT -->|"validated JSON"| STORE
    CONFIG --> EXTRACT
    STORE -.->|"historical metrics"| FUTURE

    style CONFIG fill:#f9f0ff,stroke:#531dab
    style SOURCE fill:#e6f7ff,stroke:#1890ff
    style PROCESS fill:#fff7e6,stroke:#fa8c16
    style EXTRACT fill:#f6ffed,stroke:#52c41a
    style STORE fill:#fff1f0,stroke:#cf1322
    style FUTURE fill:#f0f0f0,stroke:#8c8c8c,stroke-dasharray: 5 5
```

## Data Flow (per bank, per filing period)

```mermaid
sequenceDiagram
    actor User
    participant Orch as Orchestrator
    participant Fetch as Report Fetcher
    participant Source as EDGAR / IR Site
    participant Parser as Document Parser
    participant LLM as LLM (Claude/GPT/etc.)
    participant Store as Storage

    User->>Orch: run(bank="JPM", period="Q4-2025")
    Orch->>Fetch: fetch_reports(bank, period)
    Fetch->>Source: GET 10-K filing
    Source-->>Fetch: PDF binary
    Fetch-->>Orch: report.pdf (local path)

    Orch->>Parser: process(report.pdf)
    Parser->>Parser: detect format → PDF
    Parser->>Parser: extract text + tables
    Parser->>Parser: normalize → markdown
    Parser->>Parser: chunk for LLM context
    Parser-->>Orch: processed/ chunks

    Orch->>Store: save processed chunks

    loop For each configured metric
        Orch->>LLM: extract(chunks, metric_prompt, schema)
        LLM-->>Orch: structured JSON per metric
        Orch->>Orch: validate against schema
    end

    Orch->>Store: save output/bank/period/metrics.json
    Orch-->>User: ✅ metrics.json
```

## Package Structure

```mermaid
graph LR
    subgraph pkg["noobanks/"]
        direction TB
        cfg["config/\n(loader, models)"]
        sources["sources/\n(edgar, rss, generic)"]
        processing["processing/\n(parsers, cleaner, chunker)"]
        extraction["extraction/\n(llm, prompts, validators)"]
        storage["storage/\n(fs, models)"]
        cli["cli.py\n(entry point)"]
    end

    cli --> cfg
    cli --> sources
    cli --> processing
    cli --> extraction
    processing --> storage
    extraction --> storage
    extraction --> cfg
    sources --> cfg
```

## Key Abstractions

| Module | Class / Interface | Responsibility |
|--------|------------------|----------------|
| `config` | `BankRegistry` | Loads & validates bank definitions from YAML |
| `config` | `MetricSpec` | Pydantic model for a metric: name, prompt template, output schema |
| `sources` | `SourceAdapter` | Abstract base: `async fetch(bank, period) → list[Report]` |
| `sources` | `EdgarAdapter` | SEC EDGAR — CIK lookup, filing search, XBRL/HTML download |
| `sources` | `RssAdapter` | Generic RSS/Atom feed scraper for bank IR pages |
| `processing` | `DocumentParser` | Format dispatch: PDF→md, HTML→md, XBRL→tables |
| `processing` | `TextChunker` | Splits long documents for LLM context windows |
| `extraction` | `LlmClient` | Provider-agnostic: `async complete(prompt) → str` |
| `extraction` | `MetricExtractor` | Runs one metric spec against document chunks, returns validated JSON |
| `extraction` | `ResultAggregator` | Merges per-metric results into a single output file |
| `storage` | `ReportStore` | Save/load raw reports, processed text, and output JSON |

## Directory Layout

```
noobanks/
├── pyproject.toml              # Build config, dependencies
├── README.md
├── docs/
│   └── architecture.md         # ← this file
├── config/
│   ├── banks.yaml              # Bank definitions
│   ├── metrics.yaml            # Metric extraction specs
│   └── pipeline.yaml           # Runtime config
├── prompts/
│   ├── net_interest_income.md  # Extraction prompt templates
│   ├── tier1_ratio.md
│   └── ...
├── src/noobanks/
│   ├── __init__.py
│   ├── cli.py                  # typer CLI entry point
│   ├── config/
│   │   ├── __init__.py
│   │   ├── loader.py           # YAML → pydantic models
│   │   └── models.py           # BankSpec, MetricSpec, PipelineConfig
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py             # SourceAdapter ABC
│   │   ├── edgar.py            # SEC EDGAR adapter
│   │   └── generic.py          # Generic URL / RSS adapter
│   ├── processing/
│   │   ├── __init__.py
│   │   ├── parser.py           # Format dispatch & parse
│   │   ├── cleaner.py          # Text normalization
│   │   └── chunker.py          # Context-window chunking
│   ├── extraction/
│   │   ├── __init__.py
│   │   ├── llm.py              # LLM abstraction layer
│   │   ├── extractor.py        # MetricExtractor (prompt + chunks → JSON)
│   │   └── prompts.py          # Prompt loader & renderer
│   └── storage/
│       ├── __init__.py
│       └── store.py            # ReportStore (raw / processed / output)
├── data/                       # .gitignore'd runtime data
│   ├── raw/                    # Downloaded PDFs / HTML
│   ├── processed/              # Parsed markdown per report
│   └── output/                 # Structured metrics JSON
└── tests/
    ├── __init__.py
    ├── test_sources/
    ├── test_processing/
    └── test_extraction/
```

## Consumer Flow (Trading Engine — Future)

```mermaid
flowchart LR
    metrics_json["output/\n(bank/period/\nmetrics.json)"] --> loader["MetricTimeSeries\nLoader"]
    loader --> frame["pandas DataFrame\n(quarterly, per-bank)"]
    frame --> signals["Signal Engine\n(rule-based + AI)"]
    signals --> portfolio["Portfolio\nConstructor"]
    portfolio --> backtest["Backtester\n(vectorbt / zipline)"]
    backtest --> metrics["Performance\nMetrics"]

    style signals fill:#f0f0f0,stroke:#8c8c8c,stroke-dasharray: 5 5
    style portfolio fill:#f0f0f0,stroke:#8c8c8c,stroke-dasharray: 5 5
    style backtest fill:#f0f0f0,stroke:#8c8c8c,stroke-dasharray: 5 5
    style metrics fill:#f0f0f0,stroke:#8c8c8c,stroke-dasharray: 5 5
```
