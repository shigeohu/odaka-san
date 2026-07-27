from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from maxone_loop.config.loader import load_config
from maxone_loop.config.models import AppConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_WORKBOOK = FIXTURES / "metrics_data_20260409_150425.xlsx"


def _deep_set(data: dict[str, Any], dotted: str, value: Any) -> None:
    node = data
    *parents, leaf = dotted.split(".")
    for part in parents:
        node = node[part]
    node[leaf] = value


@pytest.fixture
def reference_workbook() -> Path:
    return REFERENCE_WORKBOOK


@pytest.fixture
def config_factory(tmp_path: Path):
    """Build an AppConfig from the tracked config files with overrides applied.

    Overrides are dotted paths, e.g.
    ``{"experiment.adaptive_policy.reference_threshold_hz": 1.0}``.
    """

    def build(**overrides: Any) -> AppConfig:
        directory = tmp_path / "config"
        directory.mkdir(exist_ok=True)
        files = {
            "experiment": "experiment.yaml",
            "metrics_schema": "metrics_schema.yaml",
            "stimulation_protocols": "stimulation_protocols.yaml",
        }
        loaded = {
            key: yaml.safe_load((REPO_ROOT / "config" / name).read_text(encoding="utf-8"))
            for key, name in files.items()
        }
        for dotted, value in overrides.items():
            head, _, rest = dotted.partition(".")
            _deep_set(loaded[head], rest, value)
        for key, name in files.items():
            (directory / name).write_text(yaml.safe_dump(loaded[key], sort_keys=False))
        return load_config(directory)

    return build


@pytest.fixture
def runnable_config(config_factory, tmp_path: Path):
    """A configuration complete enough to make decisions.

    Numbers here are test values, not experimental ones. The tracked config
    keeps them null on purpose.
    """

    def build(**extra: Any) -> AppConfig:
        runtime = tmp_path / "runtime"
        overrides: dict[str, Any] = {
            "experiment.paths.state_directory": str(runtime / "state"),
            "experiment.paths.log_directory": str(runtime / "logs"),
            "experiment.paths.accepted_archive_directory": str(runtime / "archive"),
            "experiment.paths.quarantine_directory": str(runtime / "quarantine"),
            "experiment.experiment.experiment_id": "TEST-EXPERIMENT",
            "experiment.adaptive_policy.reference_threshold_hz": 1.0,
            "experiment.adaptive_policy.amplitude_step_mV": 100.0,
            "experiment.adaptive_policy.maximum_amplitude_mV": 500.0,
            "experiment.adaptive_policy.maximum_cycles": 20,
            "experiment.adaptive_policy.maximum_total_experiment_minutes": 600.0,
            "experiment.adaptive_policy.minimum_cooldown_seconds": 0,
            "metrics_schema.recording_selection.strategy": "wellplate_and_run_id",
            "metrics_schema.recording_selection.wellplate_id": "P005157",
            "metrics_schema.recording_selection.well_number": 1,
            "metrics_schema.recording_selection.assay_run_id": "000029",
            "metrics_schema.expected_acquisition.duration_seconds_min": 290.0,
            "metrics_schema.expected_acquisition.duration_seconds_max": 310.0,
            "metrics_schema.quality_control.minimum_active_area_percent": 0.2,
            "metrics_schema.quality_control.minimum_active_electrodes": 3,
            "metrics_schema.quality_control.active_area_denominator_electrodes": 1024,
            "stimulation_protocols.stimulation.stimulation_electrodes": [1000, 1001],
            "stimulation_protocols.stimulation.waveform.phase_duration_samples": 4,
            "stimulation_protocols.stimulation.waveform.pulses_per_train": 10,
            "stimulation_protocols.stimulation.waveform.inter_pulse_interval_samples": 200,
            "stimulation_protocols.stimulation.hard_limits.maximum_absolute_amplitude_mV": 800.0,
            "stimulation_protocols.stimulation.hard_limits.maximum_cumulative_stimulations": 50,
            "stimulation_protocols.hardware.maxlab_live_version": "TEST",
            "stimulation_protocols.hardware.api_version": "TEST",
            "stimulation_protocols.hardware.emergency_stop_procedure": "documented elsewhere",
        }
        overrides.update(extra)
        return config_factory(**overrides)

    return build


@pytest.fixture
def watch_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "watch"
    directory.mkdir()
    return directory


@pytest.fixture
def copied_reference(watch_dir: Path) -> Path:
    destination = watch_dir / REFERENCE_WORKBOOK.name
    shutil.copy(REFERENCE_WORKBOOK, destination)
    return destination


def amount(value: str) -> Decimal:
    return Decimal(value)
