"""Tests for noobanks.storage.store — ReportStore path management and I/O."""

from __future__ import annotations

from pathlib import Path

from noobanks.storage.store import ReportStore


class TestReportStorePaths:
    def test_raw_path(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        path = store.raw_path(2025, "BARC_L", "annual_report", "FY")
        assert path == tmp_data_dir / "raw" / "2025" / "BARC_L_annual_report_FY.pdf"

    def test_raw_path_quarterly(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        path = store.raw_path(2026, "JPM", "10-Q", "Q3")
        assert path == tmp_data_dir / "raw" / "2026" / "JPM_10-Q_Q3.pdf"

    def test_processed_path(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        path = store.processed_path(2025, "BARC_L", "annual_report", "FY")
        assert path == tmp_data_dir / "processed" / "2025" / "BARC_L_annual_report_FY.md"

    def test_output_path(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        path = store.output_path("BARC_L", "FY")
        assert path == tmp_data_dir / "output" / "BARC_L" / "FY" / "metrics.json"

    def test_default_base_dir(self):
        store = ReportStore()
        assert store.base_dir == Path("src/data")


class TestReportStoreExists:
    def test_raw_exists_true(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        year_dir = tmp_data_dir / "raw" / "2025"
        year_dir.mkdir(parents=True)
        (year_dir / "BARC_L_annual_report_FY.pdf").write_text("fake pdf")
        assert store.raw_exists(2025, "BARC_L", "annual_report", "FY") is True

    def test_raw_exists_false(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        assert store.raw_exists(2025, "BARC_L", "annual_report", "FY") is False


class TestReportStoreEnsureDirs:
    def test_creates_dirs_when_missing(self, tmp_path: Path):
        store = ReportStore(tmp_path / "data")
        assert not store.raw_dir.exists()
        store.ensure_dirs()
        assert store.raw_dir.exists()
        assert store.processed_dir.exists()
        assert store.output_dir.exists()

    def test_noop_when_dirs_exist(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        # Should not raise
        store.ensure_dirs()


class TestReportStoreList:
    def test_list_raw_reports(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)

        # Create some fake PDFs
        (tmp_data_dir / "raw" / "2025").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2025" / "JPM_10-K_FY.pdf").write_text("pdf")
        (tmp_data_dir / "raw" / "2025" / "BAC_10-K_FY.pdf").write_text("pdf")
        (tmp_data_dir / "raw" / "2026").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2026" / "C_10-K_FY.pdf").write_text("pdf")

        reports = store.list_raw_reports()
        assert len(reports) == 3
        # Should be sorted
        assert reports[0].name == "BAC_10-K_FY.pdf"

    def test_list_raw_reports_empty(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        assert store.list_raw_reports() == []

    def test_list_raw_reports_dir_missing(self, tmp_path: Path):
        store = ReportStore(tmp_path / "nonexistent")
        assert store.list_raw_reports() == []

    def test_list_raw_reports_for_year(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        (tmp_data_dir / "raw" / "2025").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2025" / "A.pdf").write_text("a")
        (tmp_data_dir / "raw" / "2025" / "B.pdf").write_text("b")

        reports = store.list_raw_reports_for_year(2025)
        assert len(reports) == 2

    def test_list_raw_reports_for_year_missing(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        assert store.list_raw_reports_for_year(2099) == []

    def test_list_ignores_non_pdf_files(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        (tmp_data_dir / "raw" / "2025").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2025" / "notes.txt").write_text("notes")
        (tmp_data_dir / "raw" / "2025" / "report.pdf").write_text("pdf")

        reports = store.list_raw_reports()
        assert len(reports) == 1
        assert reports[0].name == "report.pdf"


class TestReportStoreSizeSummary:
    def test_empty(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        assert store.raw_size_summary() == {}

    def test_with_files(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        (tmp_data_dir / "raw" / "2025").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2025" / "A.pdf").write_bytes(b"x" * 1000)

        summary = store.raw_size_summary()
        assert "2025" in summary
        assert summary["2025"] == 1000

    def test_multiple_years(self, tmp_data_dir: Path):
        store = ReportStore(tmp_data_dir)
        (tmp_data_dir / "raw" / "2025").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2025" / "A.pdf").write_bytes(b"a" * 500)
        (tmp_data_dir / "raw" / "2024").mkdir(parents=True, exist_ok=True)
        (tmp_data_dir / "raw" / "2024" / "B.pdf").write_bytes(b"b" * 300)

        summary = store.raw_size_summary()
        assert summary["2024"] == 300
        assert summary["2025"] == 500
