"""Adaptive policy tests (CLAUDE.md section 14, "Policy")."""

from __future__ import annotations

from decimal import Decimal

import pytest

from maxone_loop.metrics.extract import AnalysisResult, ElectrodeEstimate
from maxone_loop.metrics.qc import QCFlag, QCReport
from maxone_loop.metrics.workbook import RecordingIdentity
from maxone_loop.policy import CycleState, baseline_decision, decide
from maxone_loop.reason_codes import (
    BELOW_THRESHOLD_STEP_UP,
    CONFIG_INVALID,
    CUMULATIVE_STIMULATION_LIMIT_REACHED,
    MAX_AMPLITUDE_EXCEEDED,
    MAX_CYCLES_REACHED,
    MAX_DURATION_REACHED,
    SINGLE_ELECTRODE_MEAN,
    THRESHOLD_REACHED,
)

PASSING = QCReport(flags=())


def result(rate: float | None) -> AnalysisResult:
    return AnalysisResult(
        mean_firing_frequency_hz=rate,
        source_sheet="Activity - Well Level",
        source_column="Mean Firing Rate [Hz]",
        source_instance=3,
        identity=RecordingIdentity("/p", "P1", "000001", 1),
        active_area_percent=1.0,
        electrodes=ElectrodeEstimate(count=10.0, denominator=1024),
        firing_rate_std_hz=0.2,
        firing_rate_cv=0.4,
        duration_seconds=300.02,
        number_of_configurations=1,
        sampling_frequency_hz=20000,
        gain=512,
        lsb_uv=6.294,
        hpf_hz=1,
        recording_start=None,
        recording_stop=None,
        analysis_parameters={},
        workbook_filename="metrics_data_20260101_000000.xlsx",
        workbook_sha256="0" * 64,
        schema_version=2,
    )


def state(previous="0", cycle=1, stimulations=0, elapsed=0.0) -> CycleState:
    return CycleState(
        cycle_index=cycle,
        previous_amplitude_mV=Decimal(previous),
        cumulative_stimulations=stimulations,
        elapsed_minutes=elapsed,
    )


def test_baseline_records_without_stimulating():
    decision = baseline_decision()
    assert decision.action == "RECORD"
    assert decision.stimulates is False
    assert decision.candidate_amplitude_mV == Decimal("0")


def test_below_threshold_increments_exactly_one_step(runnable_config):
    config = runnable_config()
    decision = decide(config, result(0.5), PASSING, state(previous="100"))

    assert decision.action == "STIMULATE"
    assert decision.reason_code == BELOW_THRESHOLD_STEP_UP
    assert decision.candidate_amplitude_mV == Decimal("200")


def test_rate_equal_to_threshold_completes(runnable_config):
    config = runnable_config()
    decision = decide(config, result(1.0), PASSING, state())

    assert decision.action == "COMPLETE"
    assert decision.reason_code == THRESHOLD_REACHED


def test_rate_above_threshold_completes(runnable_config):
    config = runnable_config()
    decision = decide(config, result(2.5), PASSING, state())
    assert decision.action == "COMPLETE"
    assert decision.reason_code == THRESHOLD_REACHED


def test_candidate_equal_to_maximum_is_allowed(runnable_config):
    config = runnable_config()
    decision = decide(config, result(0.5), PASSING, state(previous="400"))

    assert decision.action == "STIMULATE"
    assert decision.candidate_amplitude_mV == Decimal("500")


def test_candidate_above_maximum_is_blocked(runnable_config):
    config = runnable_config()
    decision = decide(config, result(0.5), PASSING, state(previous="500"))

    assert decision.action == "COMPLETE"
    assert decision.reason_code == MAX_AMPLITUDE_EXCEEDED


def test_max_amplitude_can_fault_instead_of_completing(runnable_config):
    config = runnable_config(
        **{"experiment.adaptive_policy.action_when_next_step_exceeds_maximum": "fault"}
    )
    decision = decide(config, result(0.5), PASSING, state(previous="500"))

    assert decision.action == "FAULT"
    assert decision.reason_code == MAX_AMPLITUDE_EXCEEDED


def test_protocol_hard_limit_is_independent(runnable_config):
    """The protocol ceiling can bite before the policy maximum does."""
    config = runnable_config(
        **{
            "experiment.adaptive_policy.maximum_amplitude_mV": 5000.0,
            "stimulation_protocols.stimulation.hard_limits.maximum_absolute_amplitude_mV": 150.0,
        }
    )
    decision = decide(config, result(0.5), PASSING, state(previous="100"))

    assert decision.action == "FAULT"
    assert decision.reason_code == MAX_AMPLITUDE_EXCEEDED
    assert "hard limit" in decision.message


@pytest.mark.parametrize(
    "field",
    [
        "experiment.adaptive_policy.reference_threshold_hz",
        "experiment.adaptive_policy.amplitude_step_mV",
        "experiment.adaptive_policy.maximum_amplitude_mV",
    ],
)
def test_null_policy_value_blocks_the_decision(runnable_config, field):
    config = runnable_config(**{field: None})
    decision = decide(config, result(0.5), PASSING, state())

    assert decision.action == "FAULT"
    assert decision.reason_code == CONFIG_INVALID


def test_qc_failure_blocks_before_anything_else(runnable_config):
    """A QC rejection wins even when the rate would have completed the run."""
    config = runnable_config()
    failing = QCReport(flags=(QCFlag(SINGLE_ELECTRODE_MEAN, "one electrode"),))
    decision = decide(config, result(99.0), failing, state())

    assert decision.action == "FAULT"
    assert decision.reason_code == SINGLE_ELECTRODE_MEAN


def test_maximum_cycles_completes(runnable_config):
    config = runnable_config(**{"experiment.adaptive_policy.maximum_cycles": 3})
    decision = decide(config, result(0.5), PASSING, state(cycle=2))

    assert decision.action == "COMPLETE"
    assert decision.reason_code == MAX_CYCLES_REACHED


def test_maximum_duration_completes(runnable_config):
    config = runnable_config(
        **{"experiment.adaptive_policy.maximum_total_experiment_minutes": 30.0}
    )
    decision = decide(config, result(0.5), PASSING, state(elapsed=31.0))

    assert decision.action == "COMPLETE"
    assert decision.reason_code == MAX_DURATION_REACHED


def test_cumulative_stimulation_limit_completes(runnable_config):
    config = runnable_config(
        **{"stimulation_protocols.stimulation.hard_limits.maximum_cumulative_stimulations": 2}
    )
    decision = decide(config, result(0.5), PASSING, state(stimulations=2))

    assert decision.action == "COMPLETE"
    assert decision.reason_code == CUMULATIVE_STIMULATION_LIMIT_REACHED


def test_amplitude_arithmetic_does_not_drift(runnable_config):
    """Ten 0.1 mV steps must land exactly on 1.0 mV, not 0.9999999999999999."""
    config = runnable_config(
        **{
            "experiment.adaptive_policy.amplitude_step_mV": 0.1,
            "experiment.adaptive_policy.maximum_amplitude_mV": 1.0,
        }
    )
    amplitude = Decimal("0")
    for _ in range(10):
        decision = decide(config, result(0.5), PASSING, state(previous=str(amplitude)))
        assert decision.action == "STIMULATE", decision.reason_code
        amplitude = decision.candidate_amplitude_mV

    assert amplitude == Decimal("1.0")
    # The eleventh step exceeds the maximum, exactly at the boundary.
    assert decide(config, result(0.5), PASSING, state(previous=str(amplitude))).reason_code == (
        MAX_AMPLITUDE_EXCEEDED
    )


def test_decision_is_reproducible(runnable_config):
    config = runnable_config()
    inputs = (config, result(0.5), PASSING, state(previous="100"))
    assert decide(*inputs) == decide(*inputs)
