"""HTML extraction helpers for finding PDF links and navigation URLs."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from noobanks.sources.keywords import (
    NAV_EXCLUDE_TEXT,
    NAV_KEYWORDS,
    REPORT_PATTERNS,
)


def extract_pdf_links(
    html: str,
    base_url: str,
    report_type: str,
    year_str: str,
    *,
    report_patterns: Optional[dict[str, list[str]]] = None,
) -> list[tuple[str, str]]:
    """Parse HTML and extract PDF hrefs matching the report type + year.

    A PDF link is returned when both year and report-type information
    are present, though they may appear in different signals:
    - Year may appear in the href or the <a> tag text.
    - Report-type keywords may appear in the href or the <a> tag text.

    Args:
        html: Raw HTML of the page.
        base_url: Base URL for resolving relative links.
        report_type: Target report type key (e.g. "annual_report", "10-K").
        year_str: 4-digit year as string (e.g. "2025").
        report_patterns: Dict of report_type -> pattern list. If None,
            uses the default REPORT_PATTERNS from the keywords module.

    Returns:
        List of (url, anchor_text) tuples.  Anchor text is the stripped
        text of the <a> tag, or an empty string when unavailable.
    """
    if report_patterns is None:
        report_patterns = REPORT_PATTERNS
    patterns = [p.lower() for p in report_patterns.get(report_type, [report_type.lower()])]
    year_short = year_str[2:]

    soup = BeautifulSoup(html, "lxml")
    links: list[tuple[str, str]] = []
    seen: set[str] = set()

    for a_tag in soup.find_all("a", href=True):
        href_value = a_tag.get("href")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        href_lower = href.lower()

        if not href_lower.endswith(".pdf"):
            continue

        link_text = a_tag.get_text(strip=True)

        year_present = (
            year_str in href
            or year_short in href
            or year_str in link_text
            or year_short in link_text
        )

        type_present = any(
            p in href_lower or p in link_text.lower() for p in patterns
        )

        if year_present and type_present:
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                links.append((full_url, link_text))

    return links


def extract_nav_links(html: str, base_url: str) -> list[str]:
    """Extract report-related navigation links from an webpage.

    Finds <a> tags whose text or href contains report-related keywords.
    Excludes PDF links, generic navigation, and already-seen URLs.
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    links: list[str] = []

    for a_tag in soup.find_all("a", href=True):
        href_value = a_tag.get("href")
        if not isinstance(href_value, str):
            continue
        href = href_value.strip()
        href_lower = href.lower()

        if href_lower.endswith(".pdf"):
            continue
        if href.startswith(("javascript:", "mailto:", "#")):
            continue

        link_text = a_tag.get_text(strip=True).lower()

        if link_text in NAV_EXCLUDE_TEXT:
            continue

        text_or_href = f"{href_lower} {link_text}"
        matches = any(kw in text_or_href for kw in NAV_KEYWORDS)
        is_year_link = bool(
            link_text
            and link_text.replace("年", "").replace(" ", "").strip().isdigit()
            and len(link_text.replace("年", "").replace(" ", "").strip()) == 4
        )
        if not matches and not is_year_link:
            continue

        if matches:
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)

    return links