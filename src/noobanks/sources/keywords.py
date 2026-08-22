"""Keyword and pattern constants for report discovery and scoring.

All string-matching keyword sets live here so that extraction, scoring,
and the main adapter can share a single source of truth.
"""

from __future__ import annotations

# Common report-type keywords for matching PDF links.
# Used by to decide whether a <a> tag or a href is a plausible report link 
# (must match both a year and one of these patterns).
REPORT_PATTERNS: dict[str, list[str]] = {
    "annual_report": [
        "annual.report", "annual_report", "annualreport",
        "annual-report", "annual report", "annual",
        "full-year", "full_year", "fy-report",
        "年报", "年度报告",
    ],
    "interim_report": [
        "interim.report", "interim_report", "interim-report",
        "interim report", "interim",
        "half-year", "half_year", "h1", "h2", "halfyear",
        "中期报告", "半年报", "半年度报告",
    ],
    "quarterly_report": [
        "quarterly.report", "quarterly_report", "quarterly-report",
        "quarterly report", "quarterly",
        "q1", "q2", "q3", "q4",
        "季度报告", "季报", "一季度", "二季度", "三季度", "四季度",
        "第1季度", "第2季度", "第3季度", "第4季度",
    ],
    "10-K": ["10-k", "10k", "form-10-k", "form 10-k"],
    "10-Q": ["10-q", "10q", "form-10-q", "form 10-q"],
    "8-K": ["8-k", "8k", "form-8-k", "form 8-k"],
    "6-K": ["6-k", "6k", "form-6-k", "form 6-k"],
    "pillar3": ["pillar.3", "pillar-3", "pillar3", "pillar_3", "第三支柱"],
}

# Human-readable labels for search-fallback DuckDuckGo queries.
REPORT_TYPE_LABELS: dict[str, str] = {
    "annual_report": "annual report 年报 年度报告",
    "10-K": "10-K annual report 10-K 年报",
    "10-Q": "10-Q quarterly report 10-Q 季度报告",
    "8-K": "8-K current report 8-K 当期报告",
    "6-K": "6-K current report 6-K 当期报告",
    "interim_report": "interim report 中期报告 半年报",
    "quarterly_report": "quarterly report 季度报告 季报",
    "pillar3": "pillar 3 disclosures 第三支柱",
}

# Report-type keywords for candidate scoring.
REPORT_TYPE_SCORE_KEYWORDS: dict[str, list[str]] = {
    "annual_report": [
        "annual report", "annual-report", "annual_report", "annualreport",
        "full-year", "full_year",
        "年报", "年度报告",
    ],
    "interim_report": [
        "interim report", "interim-report", "interim_report", "interimreport",
        "interim results", "interim-results", "interim_results", "interimresults",
        "half-year", "half_year", "half year", "half-year results",
        "中期报告", "半年报", "半年度报告",
    ],
    "quarterly_report": [
        "quarterly report", "quarterly-report", "quarterly_report", "quarterlyreport",
        "季度报告", "季报",
    ],
    "10-K": ["10-k", "10k"],
    "10-Q": ["10-q", "10q"],
    "8-K": ["8-k", "8k"],
    "6-K": ["6-k", "6k"],
    "pillar3": ["pillar 3", "pillar-3", "pillar3", "pillar_3", "第三支柱"],
}

# Period keywords for candidate scoring.
PERIOD_SCORE_KEYWORDS: dict[str, list[str]] = {
    "FY": ["fy", "full year", "full-year", "annual", "yearly", "年报", "年度报告"],
    "Q1": ["q1", "quarter 1", "1st quarter", "first quarter", "一季度", "第1季度"],
    "Q2": ["q2", "quarter 2", "2nd quarter", "second quarter", "二季度", "第2季度"],
    "Q3": ["q3", "quarter 3", "3rd quarter", "third quarter", "三季度", "第3季度"],
    "Q4": ["q4", "quarter 4", "4th quarter", "fourth quarter", "四季度", "第4季度"],
    "H1": ["h1", "half-year 1", "first half", "上半年", "半年报", "中期报告"],
    "H2": ["h2", "half-year 2", "second half", "下半年"],
}

# Filenames that signal a non-report document. Penalized in text-aware scoring.
NON_REPORT_SCORE_KEYWORDS: list[str] = [
    "announcement", "circular", "notice", "briefing", "qarecord",
    "annual results", "annual-results", "annual_results", "annualresults",
    "公告", "通告", "通知", "通函", "新闻稿", "会议纪要", "路演", "问答",
    "简讯", "简报",
]

# Navigation-link keywords and exclusion sets.
NAV_KEYWORDS: list[str] = [
    "annual-report", "annual_reports", "annual report",
    "interim-report", "interim_reports", "interim report",
    "quarterly-report", "quarterly_reports", "quarterly report",
    "financial-report", "financial_reports", "financial report",
    "financial-results", "financial_results", "financial results",
    "performance-report", "performance_reports", "performance report",
    "results-and-reports", "results-and-announcements",
    "reports-and-events", "reports-and-presentations",
    "earnings", "filings", "sec-filings",
    "regulatory-news", "regulatory_filings",
    "pillar-3", "pillar_3", "pillar3",
    # Chinese report-related navigation keywords
    "年报", "年度报告", "年報", "年度報告",
    "中期报告", "中期報告", "半年报", "半年報",
    "季度报告", "季度報告", "季报", "季報",
    "财务报告", "財務報告", "财务报表", "財務報表",
    "业绩报告", "業績報告", "业绩公告", "業績公告",
    "投资者关系", "投資者關係", "投资者", "投資者",
    "信息披露", "信息揭露", "定期报告", "定期報告",
    "公告", "报告", "報告",
]

# Navigation-link text that should be excluded (too generic / not report-related).
NAV_EXCLUDE_TEXT: set[str] = {
    "home", "about", "about us", "contact", "contact us",
    "careers", "news", "media", "press", "search", "login",
    "share price", "stock", "corporate governance", "sustainability",
    "csr", "esg", "cookie", "privacy", "terms", "accessibility",
    "sitemap", "rss", "email alerts", "subscribe",
    # Chinese generic navigation text to exclude
    "首页", "关于", "关于我们", "关于我們",
    "联系我们", "聯繫我們", "加入我们", "加入我們",
    "招聘", "人才招聘", "职位", "職位",
    "搜索", "搜尋", "登录", "登錄", "注册", "註冊",
    "股价", "股價", "股票", "公司治理", "公司管治",
    "可持续发展", "可持續發展", "社会责任", "社會責任",
    "隐私", "隱私", "条款", "條款", "无障碍", "無障礙",
    "网站地图", "網站地圖", "邮件提醒", "郵件提醒", "订阅", "訂閱",
}