"""Data sourcing layer — adapters for fetching bank financial reports."""

from noobanks.sources.base import SourceAdapter, Report, FetchResult
from noobanks.sources.generic import GenericIrAdapter

__all__ = ["SourceAdapter", "Report", "FetchResult", "GenericIrAdapter"]
