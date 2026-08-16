"""Tests for noobanks.processing.scorer — keyword page ranking."""

from noobanks.processing.parser import PageText
from noobanks.processing.scorer import PageScorer


def _pages(*texts: str) -> list[PageText]:
    return [PageText(page_no=i + 1, text=t) for i, t in enumerate(texts)]


class TestPageScorer:
    def test_ranks_pages_by_keyword_hits(self):
        scorer = PageScorer()
        pages = _pages(
            "Welcome to the bank. About us section.",
            "Net interest margin was 3.63% for the year. The net interest margin improved.",
            "Contact information and branch locations.",
        )
        top = scorer.top_pages(pages, ["net interest margin"], k=2)
        assert [p.page_no for p in top] == [2]

    def test_matches_chinese_keywords(self):
        scorer = PageScorer()
        pages = _pages(
            "English only page.",
            "本行净利润率为1.5%，净资产收益率为10%。",
        )
        top = scorer.top_pages(pages, ["净利润率"], k=1)
        assert [p.page_no for p in top] == [2]

    def test_keywords_are_case_insensitive(self):
        scorer = PageScorer()
        pages = _pages("Return on tangible equity was 11.3%.")
        top = scorer.top_pages(pages, ["return on tangible equity"], k=1)
        assert [p.page_no for p in top] == [1]

    def test_returns_empty_when_no_matches(self):
        scorer = PageScorer()
        pages = _pages("Nothing relevant here.", "Also nothing.")
        top = scorer.top_pages(pages, ["total assets"], k=3)
        assert top == []

    def test_limits_to_k_pages_in_document_order(self):
        scorer = PageScorer()
        pages = _pages(
            "profit",
            "profit profit profit",
            "profit profit",
        )
        top = scorer.top_pages(pages, ["profit"], k=2)
        # scores: page1=1, page2=3, page3=2 → top-2 by score = pages 2,3, doc order
        assert [p.page_no for p in top] == [2, 3]

    def test_empty_pages_returns_empty(self):
        scorer = PageScorer()
        assert scorer.top_pages([], ["profit"]) == []
