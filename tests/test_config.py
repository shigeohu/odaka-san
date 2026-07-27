"""Configuration validation tests (CLAUDE.md section 12)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from maxone_loop.config.loader import ConfigError, load_config
from maxone_loop.orchestrator import OrchestratorError, check_armed_gate
from maxone_loop.reason_codes import ARMED_ENVIRONMENT_GATE_CLOSED, ARMED_REQUIREMENT_MISSING

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_tracked_config_loads_and_defaults_to_dry_run():
    config = load_config(REPO_ROOT / "config")
    assert config.experiment.experiment.mode == "dry_run"


def test_tracked_config_blocks_armed_mode():
    """Every unset value must be reported, not defaulted."""
    config = load_config(REPO_ROOT / "config")
    missing = config.missing_for_armed()

    assert missing, "the tracked configuration must not be armed-ready"
    assert "adaptive_policy.reference_threshold_hz" in missing
    assert "metrics_schema.recording_selection.strategy" in missing
    assert "hardware.emergency_stop_procedure" in missing


def test_every_required_before_armed_path_resolves():
    """An unresolvable path would silently satisfy the armed gate."""
    config = load_config(REPO_ROOT / "config")
    for path in config.experiment.required_before_armed:
        config.resolve(path)  # raises KeyError if the path is wrong


def test_unknown_key_is_rejected(tmp_path):
    for name in ("experiment.yaml", "metrics_schema.yaml", "stimulation_protocols.yaml"):
        (tmp_path / name).write_text((REPO_ROOT / "config" / name).read_text())

    data = yaml.safe_load((tmp_path / "experiment.yaml").read_text())
    data["adaptive_policy"]["reference_threshold_hertz"] = 1.0  # typo
    (tmp_path / "experiment.yaml").write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_safety_switches_cannot_be_flipped(config_factory):
    """The switches that disable a safety gate must refuse to be set."""
    for dotted in (
        "metrics_schema.recording_selection.allow_fallback_to_single_row",
        "metrics_schema.recording_selection.allow_fallback_to_latest",
        "metrics_schema.instance_join.allow_parity_based_selection",
        "metrics_schema.decision_metric.allow_substitute_columns",
        "metrics_schema.recording_identity.allow_assay_run_id_alone",
        "metrics_schema.known_quirks.network_burst_level.use_as_metadata_source",
    ):
        with pytest.raises(ConfigError):
            config_factory(**{dotted: True})


def test_na_must_remain_a_missing_marker(config_factory):
    with pytest.raises(ConfigError):
        config_factory(**{"metrics_schema.file_format.missing_markers": ["", "NA"]})


def test_baseline_amplitude_must_be_zero(config_factory):
    with pytest.raises(ConfigError):
        config_factory(**{"experiment.adaptive_policy.baseline_amplitude_mV": 50.0})


def test_negative_amplitude_step_is_rejected(config_factory):
    with pytest.raises(ConfigError):
        config_factory(**{"experiment.adaptive_policy.amplitude_step_mV": -10.0})


def test_electrode_minimum_requires_a_denominator(config_factory):
    """The denominator is an inference, so it must be stated explicitly."""
    with pytest.raises(ConfigError):
        config_factory(
            **{
                "metrics_schema.quality_control.minimum_active_electrodes": 5,
                "metrics_schema.quality_control.active_area_denominator_electrodes": None,
            }
        )


def test_selection_strategy_requires_its_keys(config_factory):
    with pytest.raises(ConfigError):
        config_factory(
            **{
                "metrics_schema.recording_selection.strategy": "folder_path_exact",
                "metrics_schema.recording_selection.folder_path": None,
            }
        )


def test_local_override_cannot_introduce_new_keys(tmp_path):
    for name in ("experiment.yaml", "metrics_schema.yaml", "stimulation_protocols.yaml"):
        (tmp_path / name).write_text((REPO_ROOT / "config" / name).read_text())
    (tmp_path / "experiment.local.yaml").write_text(
        yaml.safe_dump({"adaptive_policy": {"brand_new_setting": 1}})
    )

    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path)
    assert "unknown key" in str(exc.value)


def test_local_override_applies(tmp_path):
    for name in ("experiment.yaml", "metrics_schema.yaml", "stimulation_protocols.yaml"):
        (tmp_path / name).write_text((REPO_ROOT / "config" / name).read_text())
    (tmp_path / "experiment.local.yaml").write_text(
        yaml.safe_dump({"paths": {"metrics_watch_directory": "/data/metrics"}})
    )

    config = load_config(tmp_path)
    assert config.experiment.paths.metrics_watch_directory == "/data/metrics"


def test_armed_gate_requires_the_environment_variable(runnable_config):
    config = runnable_config(**{"experiment.experiment.mode": "armed"})

    with pytest.raises(OrchestratorError) as exc:
        check_armed_gate(config, environ={})
    assert exc.value.reason_code == ARMED_ENVIRONMENT_GATE_CLOSED


def test_armed_gate_requires_every_setting(config_factory):
    config = config_factory(**{"experiment.experiment.mode": "armed"})

    with pytest.raises(OrchestratorError) as exc:
        check_armed_gate(config, environ={"MAXONE_HARDWARE_ENABLED": "1"})
    assert exc.value.reason_code == ARMED_REQUIREMENT_MISSING


def test_armed_gate_passes_when_complete(runnable_config):
    config = runnable_config(
        **{
            "experiment.experiment.mode": "armed",
            "experiment.paths.metrics_watch_directory": "/data/metrics",
        }
    )
    check_armed_gate(config, environ={"MAXONE_HARDWARE_ENABLED": "1"})
