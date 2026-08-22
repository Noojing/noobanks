"""Scoring functions for ranking report URL candidates."""

from __future__ import annotations

from typing import Optional

from noobanks.sources.keywords import (
    NON_REPORT_SCORE_KEYWORDS,
    PERIOD_SCORE_KEYWORDS,
    REPORT_TYPE_SCORE_KEYWORDS,
)


def _keyword_match_score(
    url_lower: str,
    text_lower: str,
    keywords: dict[str, list[str]],
    target: Optional[str],
    text_match_weight: int,
    url_match_weight: int,
    text_other_penalty: int,
    url_other_penalty: int,
) -> int:
    """Score keyword matches: target key rewarded, other keys penalized.

    Shared logic for report-type and period scoring.
    """
    score = 0
    for key, kws in keywords.items():
        hit_text = any(kw in text_lower for kw in kws)
        hit_url = any(kw in url_lower for kw in kws)
        if key == target:
            if hit_text:
                score += text_match_weight
            elif hit_url:
                score += url_match_weight
        else:
            if hit_text:
                score -= text_other_penalty
            elif hit_url:
                score -= url_other_penalty
    return score


def score_candidate(
    url: str,
    year_str: str,
    report_type: str,
    link_text: Optional[str] = None,
    period: Optional[str] = None,
) -> int:
    """Score a candidate URL for relevance (higher = better match).

    Text-aware scoring: anchor text is weighted above the URL, report-type
    and period keywords are used for fine-grained ranking, and non-report
    documents (announcements, briefings, ...) are penalized.
    """
    url_lower = url.lower()
    text_lower = (link_text or "").lower()

    score = 0

    if year_str in text_lower:
        score += 4
    elif year_str in url_lower:
        score += 3

    score += _keyword_match_score(
        url_lower, text_lower, REPORT_TYPE_SCORE_KEYWORDS, report_type,
        text_match_weight=4, url_match_weight=3,
        text_other_penalty=3, url_other_penalty=2,
    )

    if period and period in PERIOD_SCORE_KEYWORDS:
        score += _keyword_match_score(
            url_lower, text_lower, PERIOD_SCORE_KEYWORDS, period,
            text_match_weight=4, url_match_weight=3,
            text_other_penalty=3, url_other_penalty=2,
        )

    if any(kw in text_lower for kw in NON_REPORT_SCORE_KEYWORDS):
        score -= 3
    elif any(kw in url_lower for kw in NON_REPORT_SCORE_KEYWORDS):
        score -= 2

    if "report" in url_lower:
        score += 1
    if "cdn" not in url_lower and "static" not in url_lower:
        score += 1

    return score