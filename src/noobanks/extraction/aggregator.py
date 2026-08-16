"""ResultAggregator — merge per-metric extractions into one output file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from noobanks.config.models import MetricSpec
from noobanks.extraction.extractor import MetricExtractor
from noobanks.processing.parser import PageText

logger = logging.getLogger(__name__)


class ResultAggregator:
    """Run all metric specs against one report and merge results."""

    def __init__(self, extractor: MetricExtractor):
        self.extractor = extractor

    async def extract_all(
        self,
        specs: list[MetricSpec],
        pages: list[PageText],
        year: int,
    ) -> dict[str, dict[str, Any]]:
        """Extract every metric, keyed by metric name.

        A metric that fails (LLM error, no matching pages) records
        {"metric": name, "error": "<reason>"} instead of aborting the run.
        """
        results: dict[str, dict[str, Any]] = {}
        for spec in specs:
            try:
                result = await self.extractor.extract(spec, pages, year)
                # An in-band "error" from the LLM is preserved (value+unit
                # and error are exclusive by schema, so it is one or the
                # other).
                results[spec.name] = result
                logger.info("Extracted %s: %s", spec.name, result.get("value"))
            except Exception as exc:
                logger.warning("Metric %s failed: %s", spec.name, exc)
                # Error-only record: no "value" field, so consumers can
                # branch on presence of "error" vs "value" exclusively.
                results[spec.name] = {"error": str(exc)}
        return results

    def save(self, data: dict[str, Any], path: Path) -> None:
        """Write aggregated results as pretty JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Saved metrics to %s", path)

    def records_for_bank(
        self,
        bank_ticker: str,
        bank_name: str,
        year: int,
        results: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Wrap per-metric results into a single JSONL record for one bank.

        One record per bank per report: bank identity plus a `metrics`
        dict keyed by metric name. The per-year JSONL thus holds one line
        per bank.
        """
        return [
            {
                "bank": bank_ticker,
                "bank_name": bank_name,
                "year": year,
                "metrics": results,
            }
        ]

    def has_bank_record(self, path: Path, bank: str, year: int) -> bool:
        """Check whether a (bank, year) record already exists in the JSONL."""
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("bank") == bank and record.get("year") == year:
                return True
        return False

    def append_jsonl(
        self,
        records: list[dict[str, Any]],
        path: Path,
        *,
        replace_bank: str | None = None,
        replace_year: int | None = None,
    ) -> None:
        """Append records to a JSONL file.

        When replace_bank/replace_year are given, existing lines for that
        (bank, year) pair are dropped first, so re-extracting a bank
        updates its records instead of duplicating them.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        existing: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    existing.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("Skipping corrupt JSONL line in %s", path)

        if replace_bank is not None and replace_year is not None:
            existing = [
                r
                for r in existing
                if not (r.get("bank") == replace_bank and r.get("year") == replace_year)
            ]

        with path.open("w", encoding="utf-8") as f:
            for record in existing + records:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

        logger.info("Wrote %d records to %s", len(records), path)
