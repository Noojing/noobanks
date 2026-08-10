"""Load and validate configuration from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml

from noobanks.config.models import BankRegistry


def _resolve_config_path(path: str | Path) -> Path:
    """Resolve a config path relative to the project root."""
    p = Path(path)
    if p.is_absolute():
        return p
    # Try relative to CWD first, then relative to project root
    cwd_path = Path.cwd() / p
    if cwd_path.exists():
        return cwd_path
    # Walk up from this file's location to find project root
    this_file = Path(__file__).resolve()
    for parent in this_file.parents:
        if (parent / "config" / "banks.yaml").exists():
            return parent / p
    raise FileNotFoundError(f"Could not resolve config path: {path}")


def load_bank_registry(path: str | Path = "config/banks.yaml") -> BankRegistry:
    """Load and validate the bank registry from YAML.

    Args:
        path: Path to banks.yaml. Resolved relative to CWD or project root.

    Returns:
        Validated BankRegistry instance.

    Raises:
        FileNotFoundError: If the config file cannot be found.
        pydantic.ValidationError: If the YAML structure is invalid.
    """
    resolved = _resolve_config_path(path)
    if not resolved.exists():
        raise FileNotFoundError(f"Bank config not found: {resolved}")

    with open(resolved, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return BankRegistry.model_validate(raw)
