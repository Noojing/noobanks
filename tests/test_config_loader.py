"""Tests for noobanks.config.loader — YAML config loading and path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from noobanks.config.loader import (
    _ensure_config_file,
    _get_user_config_dir,
    _resolve_config_path,
    load_bank_registry,
)
from noobanks.config.models import BankRegistry


class TestResolveConfigPath:
    def test_absolute_path_returns_unchanged(self, tmp_yaml_config: Path):
        result = _resolve_config_path(tmp_yaml_config)
        assert result == tmp_yaml_config

    def test_relative_cwd_exists(self, tmp_yaml_config: Path, monkeypatch):
        monkeypatch.chdir(tmp_yaml_config.parent)
        result = _resolve_config_path(tmp_yaml_config.name)
        assert result == tmp_yaml_config

    def test_relative_path_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            _resolve_config_path("config/nonexistent_file.yaml")


class TestEnsureConfigFile:
    def test_creates_user_config_dir_on_first_call(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        target = _ensure_config_file("banks.yaml")
        assert target == fake_home / ".noobanks" / "config" / "banks.yaml"
        assert target.exists()

    def test_copies_missing_file_in_existing_dir(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        target_dir = _get_user_config_dir()
        target_dir.mkdir(parents=True)
        target = _ensure_config_file("metrics.yaml")
        assert target.exists()


class TestLoadBankRegistry:
    def test_loads_valid_yaml(self, tmp_yaml_config: Path):
        registry = load_bank_registry(tmp_yaml_config)
        assert isinstance(registry, BankRegistry)
        assert len(registry) == 1
        assert registry.banks[0].ticker == "TEST.L"

    def test_loads_default_seeded_config(self, tmp_path: Path, monkeypatch):
        fake_home = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        registry = load_bank_registry()
        assert isinstance(registry, BankRegistry)
        assert len(registry) > 0

    def test_raises_on_missing_file(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist.yaml"
        with pytest.raises(FileNotFoundError):
            load_bank_registry(missing)

    def test_raises_on_invalid_yaml(self, tmp_path: Path):
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("banks: [{bad_syntax: ]", encoding="utf-8")
        with pytest.raises(Exception):
            load_bank_registry(bad_yaml)

    def test_raises_on_valid_yaml_wrong_structure(self, tmp_path: Path):
        wrong = tmp_path / "wrong.yaml"
        wrong.write_text("foo: bar\n", encoding="utf-8")
        with pytest.raises(ValidationError):
            load_bank_registry(wrong)