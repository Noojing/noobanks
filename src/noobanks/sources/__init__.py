"""Data sourcing layer — adapters for fetching bank financial reports."""

from noobanks.sources.base_adapter import FetchResult, Report, SourceAdapter
from noobanks.sources.composite_adapter import CompositeAdapter
from noobanks.sources.ddgs_adapter import DdgsAdapter
from noobanks.sources.ir_adapter import IrAdapter

__all__ = [
    "SourceAdapter",
    "Report",
    "FetchResult",
    "CompositeAdapter",
    "DdgsAdapter",
    "IrAdapter",
]
