"""Tests for noobanks.cli — helpers (no typer invocation)."""

import os

from noobanks.cli import _default_parse_workers


class TestDefaultParseWorkers:
    def test_is_half_of_cpu_cores(self):
        assert _default_parse_workers() == max(1, (os.cpu_count() or 2) // 2)

    def test_never_below_one(self):
        assert _default_parse_workers() >= 1
