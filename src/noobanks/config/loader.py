"""Load and validate configuration from YAML files.

Configuration is resolved from ``~/.noobanks/config/`` by default.
If the target directory or a specific file is missing, the package's
built-in ``config/`` directory is copied as a base (only on first use).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from noobanks.config.models import BankRegistry, LlmConfig, MetricSpec

# Default user config directory under the home directory.
# Computed lazily via _get_user_config_dir() so tests can monkeypatch Path.home.
def _get_user_config_dir() -> Path:
    return Path.home() / ".noobanks" / "config"


# Package-bundled config directory (used as the seed for first-time setup).
_PACKAGE_CONFIG_DIR: Path = (
    Path(__file__).resolve().parent / "templates"
)


def _ensure_user_config_dir() -> Path:
    """Ensure ``~/.noobanks/config/`` exists, seeding it from the package
    defaults when necessary."""
    user_dir = _get_user_config_dir()
    if not user_dir.exists():
        if _PACKAGE_CONFIG_DIR.exists():
            shutil.copytree(_PACKAGE_CONFIG_DIR, user_dir)
        else:
            user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


def _ensure_config_file(filename: str) -> Path:
    """Ensure a single config file exists under the user config dir.

    If the file is missing it is copied from the package defaults.
    """
    cfg_dir = _ensure_user_config_dir()
    target = cfg_dir / filename
    if not target.exists() and _PACKAGE_CONFIG_DIR.exists():
        src = _PACKAGE_CONFIG_DIR / filename
        if src.exists():
            shutil.copy2(src, target)
    return target


def _resolve_config_path(path: str | Path) -> Path:
    """Resolve a config path.

    * Absolute paths are returned unchanged.
    * The default user config dir (``~/.noobanks/config/``) is checked first;
      missing dirs/files are seeded from the package.
    * When the user explicitly passes a relative path it is resolved from
      CWD (same as before) and the seed-fallback does **not** apply.
    """
    p = Path(path)
    if p.is_absolute():
        return p

    # Relative path — resolve from CWD (no auto-seeding)
    cwd_path = Path.cwd() / p
    if cwd_path.exists():
        return cwd_path

    raise FileNotFoundError(f"Could not resolve config path: {path}")


def load_bank_registry(path: str | Path | None = None) -> BankRegistry:
    """Load and validate the bank registry from YAML.

    Args:
        path: Optional explicit path. If ``None`` the default file
              ``banks.yaml`` under ``~/.noobanks/config/`` is used,
              auto-seeded from the package on first access.

    Returns:
        Validated BankRegistry instance.

    Raises:
        FileNotFoundError: If the config file cannot be found.
        pydantic.ValidationError: If the YAML structure is invalid.
    """
    if path is None:
        resolved = _ensure_config_file("banks.yaml")
    else:
        resolved = _resolve_config_path(path)

    if not resolved.exists():
        raise FileNotFoundError(f"Bank config not found: {resolved}")

    with open(resolved, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return BankRegistry.model_validate(raw)


def load_metric_specs(path: str | Path | None = None) -> list[MetricSpec]:
    """Load metric extraction specs from YAML.

    Returns a list of MetricSpec in file order.
    """
    if path is None:
        resolved = _ensure_config_file("metrics.yaml")
    else:
        resolved = _resolve_config_path(path)

    if not resolved.exists():
        raise FileNotFoundError(f"Metric config not found: {resolved}")

    with open(resolved, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return [MetricSpec.model_validate(m) for m in raw["metrics"]]


def load_llm_config(path: str | Path | None = None) -> LlmConfig:
    """Load the LLM backend configuration from pipeline.yaml."""
    if path is None:
        resolved = _ensure_config_file("pipeline.yaml")
    else:
        resolved = _resolve_config_path(path)

    if not resolved.exists():
        raise FileNotFoundError(f"Pipeline config not found: {resolved}")

    with open(resolved, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return LlmConfig.model_validate(raw["llm"])