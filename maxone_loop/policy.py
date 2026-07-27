"""The adaptive decision policy.

Pure: no I/O, no clock, no hardware. Given a configuration, an analysis result,
a QC report, and the cycle state, it returns exactly one decision. The same
inputs always produce the same output, which is what makes a stored decision
reproducible from the ledger (CLAUDE.md section 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from .config.models import AppConfig
from .metrics.extract import AnalysisResult
from .metrics.qc import QCReport
from .reason_codes import (
    BASELINE_RECORDED,
    BELOW_THRESHOLD_STEP_UP,
    CONFIG_INVALID,
    CUMULATIVE_STIMULATION_LIMIT_REACHED,
    MAX_AMPLITUDE_EXCEEDED,
    MAX_CYCLES_REACHED,
    MAX_DURATION_REACHED,
    RATE_MISSING,
    THRESHOLD_REACHED,
)

#: ``RECORD`` is deliberately distinct from ``STIMULATE``. The 0 V baseline is a
#: recording-only cycle, and an orchestrator must not be able to reach a
#: hardware write by treating "amplitude 0" as a stimulation of size zero.
Action = Literal["RECORD", "STIMULATE", "COMPLETE", "FAULT"]


@dataclass(frozen=True)
class CycleState:
    """Everything the policy needs to know about the run so far."""

    cycle_index: int
    previous_amplitude_mV: Decimal
    cumulative_stimulations: int
    elapsed_minutes: float


@dataclass(frozen=True)
class Decision:
    action: Action
    reason_code: str
    message: str
    candidate_amplitude_mV: Decimal | None = None

    @property
    def stimulates(self) -> bool:
        return self.action == "STIMULATE"


def decide(config: AppConfig, result: AnalysisResult, qc: QCReport, state: CycleState) -> Decision:
    """Return the single decision for this cycle."""
    policy = config.experiment.adaptive_policy
    limits = config.stimulation_protocols.stimulation.hard_limits

    # A QC failure blocks stimulation before anything else is considered.
    if not qc.passed:
        codes = ", ".join(qc.blocking_codes)
        return Decision(
            action="FAULT",
            reason_code=qc.blocking_codes[0],
            message=f"quality control rejected the recording: {codes}",
        )

    # Null policy values must block operation rather than be defaulted.
    missing = [
        name
        for name, value in (
            ("reference_threshold_hz", policy.reference_threshold_hz),
            ("amplitude_step_mV", policy.amplitude_step_mV),
            ("maximum_amplitude_mV", policy.maximum_amplitude_mV),
        )
        if value is None
    ]
    if missing:
        return Decision(
            action="FAULT",
            reason_code=CONFIG_INVALID,
            message="adaptive policy is incomplete: " + ", ".join(missing) + " is null",
        )

    rate = result.mean_firing_frequency_hz
    if rate is None:
        return Decision(
            action="FAULT",
            reason_code=RATE_MISSING,
            message="mean firing frequency is unavailable",
        )

    assert policy.reference_threshold_hz is not None  # narrowed above
    assert policy.amplitude_step_mV is not None
    assert policy.maximum_amplitude_mV is not None

    # Inclusive comparison: reaching the threshold is normal completion.
    if rate >= policy.reference_threshold_hz:
        return Decision(
            action="COMPLETE",
            reason_code=THRESHOLD_REACHED,
            message=(
                f"mean firing frequency {rate} Hz reached the reference threshold "
                f"{policy.reference_threshold_hz} Hz"
            ),
        )

    candidate = state.previous_amplitude_mV + policy.amplitude_step_mV

    if candidate > policy.maximum_amplitude_mV:
        action: Action = (
            "COMPLETE" if policy.action_when_next_step_exceeds_maximum == "complete" else "FAULT"
        )
        return Decision(
            action=action,
            reason_code=MAX_AMPLITUDE_EXCEEDED,
            message=(
                f"next step {candidate} mV would exceed the maximum "
                f"{policy.maximum_amplitude_mV} mV"
            ),
            candidate_amplitude_mV=candidate,
        )

    # The protocol's own ceiling is independent of the adaptive policy's.
    if limits.maximum_absolute_amplitude_mV is not None and candidate > limits.maximum_absolute_amplitude_mV:
        return Decision(
            action="FAULT",
            reason_code=MAX_AMPLITUDE_EXCEEDED,
            message=(
                f"next step {candidate} mV would exceed the protocol hard limit "
                f"{limits.maximum_absolute_amplitude_mV} mV"
            ),
            candidate_amplitude_mV=candidate,
        )

    if policy.maximum_cycles is not None and state.cycle_index + 1 >= policy.maximum_cycles:
        return Decision(
            action="COMPLETE",
            reason_code=MAX_CYCLES_REACHED,
            message=f"maximum_cycles {policy.maximum_cycles} reached",
        )

    if (
        policy.maximum_total_experiment_minutes is not None
        and state.elapsed_minutes >= policy.maximum_total_experiment_minutes
    ):
        return Decision(
            action="COMPLETE",
            reason_code=MAX_DURATION_REACHED,
            message=(
                f"elapsed {state.elapsed_minutes:.1f} min reached "
                f"maximum_total_experiment_minutes {policy.maximum_total_experiment_minutes}"
            ),
        )

    if (
        limits.maximum_cumulative_stimulations is not None
        and state.cumulative_stimulations + 1 > limits.maximum_cumulative_stimulations
    ):
        return Decision(
            action="COMPLETE",
            reason_code=CUMULATIVE_STIMULATION_LIMIT_REACHED,
            message=(
                f"cumulative stimulation limit {limits.maximum_cumulative_stimulations} reached"
            ),
        )

    return Decision(
        action="STIMULATE",
        reason_code=BELOW_THRESHOLD_STEP_UP,
        message=(
            f"mean firing frequency {rate} Hz is below the threshold "
            f"{policy.reference_threshold_hz} Hz; stepping to {candidate} mV"
        ),
        candidate_amplitude_mV=candidate,
    )


def baseline_decision() -> Decision:
    """Cycle 0 records at 0 V and never transmits a stimulation sequence."""
    return Decision(
        action="RECORD",
        reason_code=BASELINE_RECORDED,
        message="baseline cycle: recording only, no stimulation sequence",
        candidate_amplitude_mV=Decimal("0"),
    )
