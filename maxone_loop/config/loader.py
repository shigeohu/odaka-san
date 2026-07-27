"""Load and validate the three configuration files.

Machine-specific paths and electrode IDs belong in ``*.local.yaml`` overrides,
which are gitignored (CLAUDE.md section 12). An override may only replace
values that already exist in the tracked file -- it may not introduce new keys,
because a key that exists nowhere else is far more likely to be a typo than an
intentional setting.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import AppConfig, ExperimentConfig, MetricsSchema, StimulationProtocols

DEFAULT_CONFIG_DIR = Path("config")

_FILES = {
    "experiment": ("experiment.yaml", ExperimentConfig),
    "metrics_schema": ("metrics_schema.yaml", MetricsSchema),
    "stimulation_protocols": ("stimulation_protocols.yaml", StimulationProtocols),
}


class ConfigError(Exception):
    """Configuration could not be loaded or did not validate."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deep_merge(base: dict[str, Any], override: dict[str, Any], where: str) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key not in base:
            raise ConfigError(f"{where}: override introduces unknown key {key!r}")
        if isinstance(value, dict) and isinstance(base[key], dict):
            merged[key] = _deep_merge(base[key], value, f"{where}.{key}")
        else:
            merged[key] = value
    return merged


def _load_one(directory: Path, filename: str) -> tuple[dict[str, Any], str]:
    path = directory / filename
    if not path.is_file():
        raise ConfigError(f"missing configuration file: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    local = path.with_name(path.stem + ".local.yaml")
    if local.is_file():
        override = yaml.safe_load(local.read_text(encoding="utf-8")) or {}
        if not isinstance(override, dict):
            raise ConfigError(f"{local}: expected a mapping at the top level")
        data = _deep_merge(data, override, local.name)

    return data, sha256_file(path)


def load_config(config_path: str | Path = DEFAULT_CONFIG_DIR / "experiment.yaml") -> AppConfig:
    """Load all three files. ``config_path`` names experiment.yaml or its directory."""
    path = Path(config_path)
    directory = path if path.is_dir() else path.parent

    sections: dict[str, Any] = {}
    hashes: dict[str, str] = {}
    for key, (filename, model) in _FILES.items():
        raw, digest = _load_one(directory, filename)
        try:
            sections[key] = model.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"{directory / filename} failed validation:\n{exc}") from exc
        hashes[filename] = digest

    return AppConfig(**sections, source_hashes=hashes)


def config_fingerprint(config: AppConfig) -> str:
    """Stable hash over all three files, for the provenance ledger."""
    joined = "\n".join(f"{name}:{digest}" for name, digest in sorted(config.source_hashes.items()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
