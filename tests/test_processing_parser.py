"""Tests for noobanks.processing.parser — PDF per-page text extraction."""

from pathlib import Path

import pytest

from noobanks.processing.parser import DocumentParser, PageText


class TestDocumentParser:
    def test_parse_returns_pages(self, tmp_pdf: Path):
        parser = DocumentParser()
        pages = parser.parse(tmp_pdf)
        assert isinstance(pages, list)
        assert len(pages) >= 1
        assert all(isinstance(p, PageText) for p in pages)

    def test_page_numbers_are_sequential(self, tmp_pdf: Path):
        parser = DocumentParser()
        pages = parser.parse(tmp_pdf)
        assert [p.page_no for p in pages] == list(range(1, len(pages) + 1))

    def test_parse_missing_file_raises(self, tmp_path: Path):
        parser = DocumentParser()
        with pytest.raises(ValueError, match="not found"):
            parser.parse(tmp_path / "does_not_exist.pdf")

    def test_parse_non_pdf_raises(self, tmp_path: Path):
        parser = DocumentParser()
        bad = tmp_path / "not_a_pdf.pdf"
        bad.write_bytes(b"this is not a pdf at all")
        with pytest.raises(ValueError, match="PDF"):
            parser.parse(bad)


from noobanks.processing.parser import (
    markdown_to_pages,
    parse_to_markdown,
)


class TestMarkdownRoundTrip:
    def test_markdown_to_pages_splits_on_markers(self):
        md = (
            "<!-- page 1 -->\n\nNet interest margin 3.63%\n\n"
            "<!-- page 2 -->\n\nTotal assets £1,544.2bn"
        )
        pages = markdown_to_pages(md)
        assert [p.page_no for p in pages] == [1, 2]
        assert "Net interest margin" in pages[0].text
        assert "Total assets" in pages[1].text

    def test_empty_pages_are_skipped(self):
        md = "<!-- page 1 -->\n\n\n\n<!-- page 3 -->\n\nContent on three"
        pages = markdown_to_pages(md)
        assert [p.page_no for p in pages] == [3]

    def test_parse_to_markdown_uses_page_number_metadata(self, mocker, tmp_path):
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4 fake")

        mock_doc = mocker.MagicMock()
        mocker.patch(
            "noobanks.processing.parser.pymupdf.open", return_value=mock_doc
        )
        mocker.patch(
            "pymupdf4llm.to_markdown",
            return_value=[
                {"metadata": {"page_number": 1}, "text": "Page one text"},
                {"metadata": {"page_number": 2}, "text": "Page two text"},
            ],
        )

        md = parse_to_markdown(pdf)
        assert "<!-- page 1 -->" in md
        assert "Page one text" in md
        assert "<!-- page 2 -->" in md
        assert "Page two text" in md


from noobanks.processing.parser import count_tokens


class TestCountTokens:
    def test_returns_positive_int(self):
        n = count_tokens("Net interest margin was 3.63%.")
        assert isinstance(n, int)
        assert n > 0

    def test_monotonic_with_length(self):
        assert count_tokens("a" * 500) > count_tokens("a" * 5)
