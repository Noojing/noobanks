"""DocumentParser — PDF → markdown conversion and page chunking.

The parser runs as a standalone process: it converts raw PDFs into
markdown files (with `<!-- page N -->` markers) stored under the
processed/ tree. The extractor later consumes those markdown files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Marker emitted between pages in processed markdown files.
PAGE_MARKER_RE = re.compile(r"<!-- page (\d+) -->")

# Deterministic cap on characters sent per page to the LLM — keeps token
# usage bounded regardless of page size (bank reports can have huge
# appendix pages). Truncation happens at a paragraph boundary when
# possible.
MAX_CHARS_PER_PAGE = 10_000


@dataclass
class PageText:
    """Text content of a single PDF page."""

    page_no: int  # 1-based page number
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def parse_to_markdown(pdf_path: str | Path) -> str:
    """Convert a PDF into page-marked markdown.

    Uses pymupdf4llm.to_markdown per page (header/footer stripped) so the
    output preserves table structure as markdown, with an
    `<!-- page N -->` marker before each page's content.

    Args:
        pdf_path: Path to the raw PDF.

    Returns:
        Markdown string with page markers.

    Raises:
        ValueError: If the file is missing or is not a valid PDF.
    """
    import pymupdf4llm

    path = Path(pdf_path)
    if not path.exists():
        raise ValueError(f"PDF not found: {path}")

    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        raise ValueError(f"Not a valid PDF: {path}") from exc

    try:
        chunks = pymupdf4llm.to_markdown(
            doc,
            page_chunks=True,
            header=False,
            footer=False,
            show_progress=False,
        )
    finally:
        doc.close()

    parts: list[str] = []
    for chunk in chunks:
        # pymupdf4llm returns per-page dicts with metadata.page_number (1-based)
        page_no = chunk.get("metadata", {}).get("page_number")
        text = (chunk.get("text") or "").strip()
        if page_no is None:
            parts.append(text)
        else:
            parts.append(f"<!-- page {page_no} -->\n\n{text}")
    return "\n\n".join(parts).strip()


def count_tokens(text: str, encoding: str = "cl100k_base") -> int:
    """Count tokens in text using tiktoken (BPE, cl100k_base by default).

    Used to report processed-document size; an approximation for the LLM
    backends this pipeline supports.
    """
    import tiktoken

    enc = tiktoken.get_encoding(encoding)
    return len(enc.encode(text))


def markdown_to_pages(markdown: str) -> list[PageText]:
    """Split page-marked markdown back into per-page PageText chunks.

    Args:
        markdown: Markdown produced by parse_to_markdown.

    Returns:
        List of PageText in document order.
    """
    pages: list[PageText] = []
    parts = PAGE_MARKER_RE.split(markdown)

    # parts alternates: [leading, page_no, text, page_no, text, ...]
    for i in range(1, len(parts), 2):
        page_no = int(parts[i])
        text = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if text:
            pages.append(PageText(page_no=page_no, text=text))
    return pages


class DocumentParser:
    """Extract per-page text from PDF documents using pymupdf.

    Used for quick raw-text access (e.g. tests and page counting). For
    markdown conversion use parse_to_markdown.
    """

    def parse(self, pdf_path: str | Path) -> list[PageText]:
        """Parse a PDF into per-page text.

        Args:
            pdf_path: Path to a PDF file.

        Returns:
            List of PageText, one per page, in document order.

        Raises:
            ValueError: If the file is missing or is not a valid PDF.
        """
        path = Path(pdf_path)
        if not path.exists():
            raise ValueError(f"PDF not found: {path}")

        try:
            doc = pymupdf.open(str(path))
        except Exception as exc:
            raise ValueError(f"Not a valid PDF: {path}") from exc

        pages: list[PageText] = []
        try:
            for i, page in enumerate(doc):
                text = page.get_text().strip()
                pages.append(PageText(page_no=i + 1, text=text))
        finally:
            doc.close()

        return pages
