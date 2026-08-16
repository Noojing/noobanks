"""MetricExtractor — run one metric spec against ranked document pages."""

from __future__ import annotations

import logging
from typing import Any

from noobanks.config.models import MetricSpec
from noobanks.extraction.llm import LlmClient
from noobanks.processing.parser import MAX_CHARS_PER_PAGE, PageText
from noobanks.processing.scorer import PageScorer

logger = logging.getLogger(__name__)

# Fixed output shape for every metric extraction.
# No field is ever null: unavailable fields are simply omitted. Every
# "value" is paired with a "unit"; value+unit and "error" are mutually
# exclusive (exactly one branch present).
METRIC_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "value": {"type": "number"},
        "unit": {"type": "string"},
        "error": {"type": "string"},
        "source_page": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["confidence"],
    "oneOf": [
        {"required": ["value", "unit"], "not": {"required": ["error"]}},
        {"required": ["error"], "not": {"required": ["value", "unit"]}},
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT_TEMPLATE = """You extract a single financial metric from pages of a bank's annual report.

Metric: {label} ({name})
Description: {description}

Rules:
- If the metric is stated directly in the provided pages, report that value.
- If it is NOT stated directly, compute it from stated component figures (e.g. attributable profit divided by total income), and cite both component figures you used.
- Report the value together with its unit (e.g. "%", "x", "p", "£m", "times").
- Never use null: if a field is not available, omit the field entirely.
- Return an error string ONLY when extraction is impossible — e.g. no relevant content in any provided page. value+unit and error are mutually exclusive: never return both.
- source_page: the page number (from the [Page N] markers) where the value appears; omit it if unknown.
- confidence: "high" if the value is explicitly stated, "medium" if computed from stated components, "low" if uncertain.
- Respond with JSON only."""


class MetricExtractor:
    """Extract one metric from a parsed report via keyword page scoring + LLM."""

    def __init__(
        self,
        llm: LlmClient | None = None,
        scorer: PageScorer | None = None,
    ):
        self._llm = llm
        self._scorer = scorer or PageScorer()

    @property
    def llm(self) -> LlmClient:
        if self._llm is None:
            # Lazy resolution from config/pipeline.yaml so unit tests
            # (which pass an explicit client) never touch the network.
            from noobanks.config.loader import load_llm_config
            from noobanks.extraction.llm import create_llm_client

            self._llm = create_llm_client(load_llm_config())
        return self._llm

    async def extract(
        self,
        spec: MetricSpec,
        pages: list[PageText],
        year: int,
        k: int = 5,
        max_chars_per_page: int = MAX_CHARS_PER_PAGE,
    ) -> dict[str, Any]:
        """Extract one metric from the most relevant pages.

        Args:
            spec: MetricSpec from metrics.yaml.
            pages: All parsed pages of the report.
            year: Fiscal year of the report.
            k: Max pages to send to the LLM.
            max_chars_per_page: Cap on characters sent per page — keeps
                LLM token usage bounded on oversized appendix pages.

        Returns:
            Dict matching METRIC_JSON_SCHEMA.

        Raises:
            ValueError: If no page matches the metric's keywords.
        """
        top = self._scorer.top_pages(pages, spec.keywords, k=k)
        if not top:
            raise ValueError(
                f"No pages matched keywords for metric {spec.name}"
            )

        user_text = "\n\n".join(
            f"[Page {p.page_no}]\n{self._cap(p.text, max_chars_per_page)}"
            for p in top
        )

        system = SYSTEM_PROMPT_TEMPLATE.format(
            label=spec.label,
            name=spec.name,
            description=spec.description,
        )

        logger.debug(
            "Extracting %s from %d pages (year %d)", spec.name, len(top), year
        )
        return await self.llm.complete(system, user_text, METRIC_JSON_SCHEMA)

    @staticmethod
    def _cap(text: str, max_chars: int) -> str:
        """Truncate page text to max_chars, cutting at a line boundary."""
        if len(text) <= max_chars:
            return text
        cut = text[:max_chars]
        last_newline = cut.rfind("\n")
        if last_newline > max_chars // 2:
            cut = cut[:last_newline]
        return cut + "\n…[truncated]"
