"""Data sourcing layer — adapters for fetching bank financial reports."""

from noobanks.sources.base_adapter import FetchResult, Report, SourceAdapter
from noobanks.sources.composite_adapter import CompositeAdapter

__all__ = [
    "SourceAdapter",
    "Report",
    "FetchResult",
    "CompositeAdapter",
]