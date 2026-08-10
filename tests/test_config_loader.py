"""Tests for noobanks.config.loader — YAML config loading and path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from noobanks.config.loader import _resolve_config_path, load_bank_registry
from noobanks.config.models import BankRegistry


class TestResolveConfigPath:
    def test_absolute_path_returns_unchanged(self, tmp_yaml_config: Path):
        result = _resolve_config_path(tmp_yaml_config)
        assert result == tmp_yaml_config

    def test_relative_cwd_exists(self, tmp_yaml_config: Path, monkeypatch):
        # Change CWD to the parent of our temp config
        monkeypatch.chdir(tmp_yaml_config.parent)
        result = _resolve_config_path(tmp_yaml_config.name)
        assert result == tmp_yaml_config

    def test_walk_up_to_project_root(self, monkeypatch):
        """Should walk up from this file to find the real project root."""
        # Move CWD to a temp dir so CWD relative fails
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir("/tmp")
            result = _resolve_config_path("config/banks.yaml")
            assert result.exists()
            assert result.name == "banks.yaml"

    def test_resolves_via_project_root_fallback(self):
        """When CWD doesn't have the config, the walk-up from __file__
        finds the project root. The resolved path may not exist, but
        resolution itself succeeds (it's up to the caller to check)."""
        result = _resolve_config_path("config/nonexistent_file.yaml")
        # Should resolve to somewhere under the project root
        assert "noobanks" in str(result)
        assert not result.exists()  # file doesn't exist, but path is resolved


class TestLoadBankRegistry:
    def test_loads_valid_yaml(self, tmp_yaml_config: Path):
        registry = load_bank_registry(tmp_yaml_config)
        assert isinstance(registry, BankRegistry)
        assert len(registry) == 1
        assert registry.banks[0].ticker == "TEST.L"

    def test_loads_project_config(self):
        """Integration test: load the real banks.yaml."""
        registry = load_bank_registry()
        assert len(registry) == 25
        assert registry.find_by_ticker("JPM") is not None

    def test_raises_on_missing_file(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.yaml"
        with pytest.raises(FileNotFoundError):
            load_bank_registry(missing)

    def test_raises_on_invalid_yaml(self, tmp_path: Path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("banks: [{bad_syntax: ]", encoding="utf-8")
        # This is a YAML parse error
        with pytest.raises(Exception):
            load_bank_registry(bad_yaml)

    def test_raises_on_valid_yaml_wrong_structure(self, tmp_path: Path):
        wrong = tmp_path / "wrong.yaml"
        wrong.write_text("foo: bar\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_bank_registry(wrong)
