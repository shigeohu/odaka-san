"""Quality control tests (CLAUDE.md section 14, "Quality control")."""

from __future__ import annotations

from datetime import datetime

import pytest

from maxone_loop.metrics.extract import extract
from maxone_loop.metrics.qc import evaluate
from maxone_loop.metrics.selection import select_recording
from maxone_loop.metrics.workbook import MetricsWorkbook
from maxone_loop.reason_codes import (
    ACQUISITION_MISMATCH,
    ACTIVE_AREA_BELOW_MINIMUM,
    ACTIVE_ELECTRODES_BELOW_MINIMUM,
    ANALYSIS_PARAMETER_MISMATCH,
    CONFIGURATION_COUNT_UNEXPECTED,
    DURATION_OUT_OF_RANGE,
    RATE_MISSING,
    RECORDING_WINDOW_OUTSIDE_CYCLE,
    SINGLE_ELECTRODE_MEAN,
)
from maxone_loop.synthetic import SyntheticRecording, write_workbook


def analyse(path, config):
    schema = config.metrics_schema
    workbook = MetricsWorkbook(path, schema)
    selection = select_recording(workbook, schema)
    result = extract(workbook, schema, selection, workbook_sha256="0" * 64)
    return result, evaluate(result, schema)


def synthetic(tmp_path, config, recording, name="metrics_data_20260101_000000.xlsx"):
    path = write_workbook(tmp_path / name, [recording])
    return analyse(path, config)


@pytest.fixture
def synthetic_config(runnable_config):
    """Selection pointed at the synthetic namespace."""

    def build(**extra):
        overrides = {
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
        }
        overrides.update(extra)
        return runnable_config(**overrides)

    return build


def test_healthy_recording_passes(reference_workbook, runnable_config):
    """P005157: 0.54 Hz, 0.88 % active area, dispersion present."""
    result, report = analyse(reference_workbook, runnable_config())

    assert result.mean_firing_frequency_hz == 0.54
    assert report.passed, report.blocking_codes


def test_single_electrode_recording_is_rejected(reference_workbook, runnable_config):
    """P005211: a valid float that is not a well-level rate.

    This is the case the whole QC layer exists for. 0.19 Hz would pass any
    naive numeric check while being the reading of one electrode.
    """
    config = runnable_config(
        **{
            "metrics_schema.recording_selection.wellplate_id": "P005211",
            "metrics_schema.recording_selection.assay_run_id": "000031",
        }
    )
    result, report = analyse(reference_workbook, config)

    assert result.mean_firing_frequency_hz == 0.19  # numerically fine
    assert result.firing_rate_std_hz is None  # but dispersion is "N/A"
    assert not report.passed
    assert SINGLE_ELECTRODE_MEAN in report.blocking_codes
    assert ACTIVE_AREA_BELOW_MINIMUM in report.blocking_codes
    assert ACTIVE_ELECTRODES_BELOW_MINIMUM in report.blocking_codes


def test_electrode_estimate_is_marked_unverified(reference_workbook, runnable_config):
    result, _ = analyse(reference_workbook, runnable_config())

    assert result.electrodes.verified is False
    assert result.electrodes.denominator == 1020
    assert result.electrodes.count == pytest.approx(0.88 / 100 * 1020)


def test_missing_rate_is_rejected(tmp_path, synthetic_config):
    config = synthetic_config()
    _, report = synthetic(
        tmp_path, config, SyntheticRecording("P1", "000001", mean_firing_rate_hz=None)
    )
    assert RATE_MISSING in report.blocking_codes


def test_duration_out_of_range_is_rejected(tmp_path, synthetic_config):
    config = synthetic_config()
    _, report = synthetic(
        tmp_path, config, SyntheticRecording("P1", "000001", 0.5, duration_seconds=120.0)
    )
    assert DURATION_OUT_OF_RANGE in report.blocking_codes


def test_duration_other_than_exactly_300_is_accepted(tmp_path, synthetic_config):
    config = synthetic_config()
    _, report = synthetic(
        tmp_path, config, SyntheticRecording("P1", "000001", 0.5, duration_seconds=300.084)
    )
    assert report.passed, report.blocking_codes


def test_multiple_configurations_is_rejected(tmp_path, synthetic_config):
    config = synthetic_config()
    _, report = synthetic(
        tmp_path, config, SyntheticRecording("P1", "000001", 0.5, number_of_configurations=2)
    )
    assert CONFIGURATION_COUNT_UNEXPECTED in report.blocking_codes


@pytest.mark.parametrize(
    "field,value",
    [
        ("sampling_frequency_hz", 10000),
        ("gain", 1024),
        ("lsb_uv", 3.0),
        ("hpf_hz", 300),
    ],
)
def test_acquisition_mismatch_is_rejected(tmp_path, synthetic_config, field, value):
    config = synthetic_config()
    recording = SyntheticRecording("P1", "000001", 0.5, **{field: value})
    _, report = synthetic(tmp_path, config, recording)
    assert ACQUISITION_MISMATCH in report.blocking_codes


def test_analysis_parameter_mismatch_is_rejected(tmp_path, synthetic_config):
    config = synthetic_config(
        **{"metrics_schema.expected_analysis_parameters.activity_analysis.firing_rate_threshold_hz": 0.5}
    )
    _, report = synthetic(tmp_path, config, SyntheticRecording("P1", "000001", 0.5))
    assert ANALYSIS_PARAMETER_MISMATCH in report.blocking_codes


def test_recording_outside_the_cycle_window_is_rejected(reference_workbook, runnable_config):
    config = runnable_config()
    result, _ = analyse(reference_workbook, config)

    window = (datetime(2026, 4, 10, 0, 0, 0), datetime(2026, 4, 10, 1, 0, 0))
    report = evaluate(result, config.metrics_schema, cycle_window=window)
    assert RECORDING_WINDOW_OUTSIDE_CYCLE in report.blocking_codes

    inside = (datetime(2026, 4, 9, 13, 0, 0), datetime(2026, 4, 9, 15, 0, 0))
    assert evaluate(result, config.metrics_schema, cycle_window=inside).passed


def test_active_area_minimum_is_enforced(tmp_path, synthetic_config):
    config = synthetic_config(
        **{"metrics_schema.quality_control.minimum_active_area_percent": 5.0}
    )
    _, report = synthetic(
        tmp_path, config, SyntheticRecording("P1", "000001", 0.5, active_area_percent=1.0)
    )
    assert ACTIVE_AREA_BELOW_MINIMUM in report.blocking_codes
