"""PageScorer — rank document pages by bilingual keyword relevance."""

from __future__ import annotations

from noobanks.processing.parser import PageText


class PageScorer:
    """Score pages by keyword hits and return the most relevant ones."""

    def score(self, page: PageText, keywords: list[str]) -> int:
        """Count keyword occurrences on a page (case-insensitive).

        Keywords may be multi-word phrases; each occurrence counts once.
        """
        text_lower = page.text.lower()
        total = 0
        for kw in keywords:
            total += text_lower.count(kw.lower())
        return total

    def top_pages(
        self,
        pages: list[PageText],
        keywords: list[str],
        k: int = 5,
    ) -> list[PageText]:
        """Return up to k pages with the highest keyword scores.

        Result is ordered by document page number. Returns an empty list
        when no page matches any keyword.
        """
        if not pages or not keywords:
            return []

        scored = [(self.score(p, keywords), p) for p in pages]
        scored.sort(key=lambda item: item[0], reverse=True)
        top = [p for s, p in scored[:k] if s > 0]
        top.sort(key=lambda p: p.page_no)
        return top
