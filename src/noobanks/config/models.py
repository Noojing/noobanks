"""Pydantic models for bank configuration."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    """Data sources for a bank."""

    edgar: bool = False
    investor_relations: str


class BankSpec(BaseModel):
    """A single bank entry from banks.yaml."""

    name: str
    ticker: str
    exchange: str
    market: str  # US, CN, HK, UK
    cik: Optional[str] = None
    sources: SourceConfig
    filings: list[str] = Field(default_factory=list)

    @property
    def ticker_safe(self) -> str:
        """Ticker with dots replaced by underscores for filesystem safety."""
        return self.ticker.replace(".", "_")

    @property
    def domain(self) -> str:
        """Extract domain from the investor_relations URL."""
        from urllib.parse import urlparse

        parsed = urlparse(self.sources.investor_relations)
        return parsed.netloc or ""


class BankRegistry(BaseModel):
    """Loaded bank definitions with lookup helpers."""

    banks: list[BankSpec]

    def find_by_ticker(self, ticker: str) -> Optional[BankSpec]:
        """Find a bank by exact or safe ticker match (case-insensitive)."""
        t = ticker.upper().replace("_", ".")
        for bank in self.banks:
            if bank.ticker.upper() == t:
                return bank
            if bank.ticker_safe.upper() == ticker.upper():
                return bank
        return None

    def find_by_name(self, name: str) -> Optional[BankSpec]:
        """Find a bank by case-insensitive name substring match."""
        n = name.lower()
        for bank in self.banks:
            if n in bank.name.lower():
                return bank
        return None

    def find(self, identifier: str) -> Optional[BankSpec]:
        """Find a bank by ticker first, then name."""
        return self.find_by_ticker(identifier) or self.find_by_name(identifier)

    def by_market(self, market: str) -> list[BankSpec]:
        """Return banks in a given market (case-insensitive)."""
        m = market.upper()
        return [b for b in self.banks if b.market.upper() == m]

    @property
    def markets(self) -> list[str]:
        """Return sorted list of unique markets."""
        return sorted({b.market for b in self.banks})

    def __len__(self) -> int:
        return len(self.banks)

    def __iter__(self):
        return iter(self.banks)
