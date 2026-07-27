"""Quality control. Any blocking flag prevents stimulation.

The rule that motivates this module: in the reference export, P005211 reports
``Mean Firing Rate 0.19 Hz`` at ``Active Area 0.10 %`` with ``"N/A"`` for
standard deviation, CV, and every percentile -- the mean is over exactly one
electrode. It is a perfectly valid float that a naive numeric check accepts,
and it is meaningless as a well-level rate. See CLAUDE.md section 8.4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from ..config.models import MetricsSchema
from ..reason_codes import (
    ACQUISITION_MISMATCH,
    ACTIVE_AREA_BELOW_MINIMUM,
    ACTIVE_AREA_MISSING,
    ACTIVE_ELECTRODES_BELOW_MINIMUM,
    ANALYSIS_PARAMETER_MISMATCH,
    CONFIGURATION_COUNT_UNEXPECTED,
    DURATION_MISSING,
    DURATION_OUT_OF_RANGE,
    RATE_MISSING,
    RATE_NEGATIVE,
    RATE_NOT_FINITE,
    RECORDING_WINDOW_OUTSIDE_CYCLE,
    SINGLE_ELECTRODE_MEAN,
)
from .extract import AnalysisResult

#: Acquisition settings are exact integers or short decimals in the export, so
#: a tight relative tolerance is enough to absorb float round-tripping.
_TOLERANCE = 1e-9


@dataclass(frozen=True)
class QCFlag:
    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True)
class QCReport:
    flags: tuple[QCFlag, ...]

    @property
    def passed(self) -> bool:
        return not any(flag.blocking for flag in self.flags)

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        return tuple(flag.code for flag in self.flags if flag.blocking)

    def as_records(self) -> list[dict[str, object]]:
        return [
            {"code": f.code, "message": f.message, "blocking": f.blocking} for f in self.flags
        ]


def _close(actual: float | None, expected: float) -> bool:
    return actual is not None and math.isclose(actual, expected, rel_tol=_TOLERANCE, abs_tol=_TOLERANCE)


def evaluate(
    result: AnalysisResult,
    schema: MetricsSchema,
    *,
    cycle_window: tuple[datetime, datetime] | None = None,
) -> QCReport:
    """Evaluate every configured quality gate against one analysis result."""
    qc = schema.quality_control
    expected = schema.expected_acquisition
    flags: list[QCFlag] = []

    rate = result.mean_firing_frequency_hz
    if rate is None:
        flags.append(QCFlag(RATE_MISSING, "mean firing rate is missing or \"N/A\""))
    else:
        if qc.reject_non_finite and not math.isfinite(rate):
            flags.append(QCFlag(RATE_NOT_FINITE, f"mean firing rate is {rate}"))
        if qc.reject_negative_rate and rate < 0:
            flags.append(QCFlag(RATE_NEGATIVE, f"mean firing rate is negative: {rate}"))

    # --- active area and the electrode count derived from it ---------------
    area = result.active_area_percent
    if area is None:
        flags.append(QCFlag(ACTIVE_AREA_MISSING, "Active Area [%] is missing"))
    elif qc.minimum_active_area_percent is not None and area < qc.minimum_active_area_percent:
        flags.append(
            QCFlag(
                ACTIVE_AREA_BELOW_MINIMUM,
                f"Active Area {area} % is below the configured minimum "
                f"{qc.minimum_active_area_percent} %",
            )
        )

    if qc.minimum_active_electrodes is not None:
        count = result.electrodes.count
        if count is None:
            flags.append(
                QCFlag(
                    ACTIVE_ELECTRODES_BELOW_MINIMUM,
                    "electrode count could not be derived from Active Area [%]",
                )
            )
        elif count < qc.minimum_active_electrodes:
            flags.append(
                QCFlag(
                    ACTIVE_ELECTRODES_BELOW_MINIMUM,
                    f"derived electrode count {count:.1f} is below the configured "
                    f"minimum {qc.minimum_active_electrodes} (denominator "
                    f"{result.electrodes.denominator}, unverified)",
                )
            )

    # --- the single-electrode mean -----------------------------------------
    if qc.fail_when_dispersion_missing and rate is not None:
        if result.firing_rate_std_hz is None or result.firing_rate_cv is None:
            flags.append(
                QCFlag(
                    SINGLE_ELECTRODE_MEAN,
                    "a mean firing rate is present but its dispersion statistics are "
                    'missing ("N/A"), which indicates exactly one contributing '
                    "electrode; the value is not a well-level rate",
                )
            )

    # --- acquisition settings ----------------------------------------------
    duration = result.duration_seconds
    if duration is None:
        flags.append(QCFlag(DURATION_MISSING, "Duration per Configuration [s] is missing"))
    else:
        low, high = expected.duration_seconds_min, expected.duration_seconds_max
        if (low is not None and duration < low) or (high is not None and duration > high):
            flags.append(
                QCFlag(
                    DURATION_OUT_OF_RANGE,
                    f"recording duration {duration} s is outside the configured range "
                    f"[{low}, {high}]",
                )
            )

    if result.number_of_configurations != expected.number_of_configurations:
        flags.append(
            QCFlag(
                CONFIGURATION_COUNT_UNEXPECTED,
                f"Number of Configurations is {result.number_of_configurations}, expected "
                f"{expected.number_of_configurations}; multi-configuration semantics are "
                "not documented",
            )
        )

    if qc.fail_on_acquisition_mismatch:
        for label, actual, want in (
            ("sampling frequency", result.sampling_frequency_hz, float(expected.sampling_frequency_hz)),
            ("gain", result.gain, float(expected.gain)),
            ("LSB", result.lsb_uv, expected.lsb_uv),
            ("HPF", result.hpf_hz, expected.hpf_hz),
        ):
            if not _close(actual, want):
                flags.append(
                    QCFlag(
                        ACQUISITION_MISMATCH,
                        f"{label} is {actual}, expected {want}",
                    )
                )

    # --- analysis parameters -----------------------------------------------
    if qc.fail_on_analysis_parameter_mismatch and schema.expected_analysis_parameters.fail_on_mismatch:
        flags.extend(_analysis_parameter_flags(result, schema))

    # --- timing -------------------------------------------------------------
    if qc.require_recording_window_within_cycle and cycle_window is not None:
        start, stop = cycle_window
        if result.recording_start is None or result.recording_stop is None:
            flags.append(
                QCFlag(
                    RECORDING_WINDOW_OUTSIDE_CYCLE,
                    "recording Start/Stop Time is missing, so it cannot be attributed "
                    "to this cycle",
                )
            )
        elif result.recording_start < start or result.recording_stop > stop:
            flags.append(
                QCFlag(
                    RECORDING_WINDOW_OUTSIDE_CYCLE,
                    f"recording window {result.recording_start}..{result.recording_stop} "
                    f"lies outside the cycle window {start}..{stop}",
                )
            )

    return QCReport(flags=tuple(flags))


def _analysis_parameter_flags(result: AnalysisResult, schema: MetricsSchema) -> list[QCFlag]:
    expected = schema.expected_analysis_parameters.activity_analysis
    applied = result.analysis_parameters
    checks = (
        ("Firing Rate Threshold [Hz]", expected.firing_rate_threshold_hz),
        ("Amplitude Threshold [µV]", expected.amplitude_threshold_uv),
        ("ISI Threshold [ms]", expected.isi_threshold_ms),
    )
    flags: list[QCFlag] = []
    for column, want in checks:
        if column not in applied:
            flags.append(
                QCFlag(
                    ANALYSIS_PARAMETER_MISMATCH,
                    f"Analysis Parameters is missing {column!r}",
                )
            )
            continue
        value = applied[column]
        actual = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if not _close(actual, want):
            flags.append(
                QCFlag(
                    ANALYSIS_PARAMETER_MISMATCH,
                    f"{column} is {value!r}, expected {want}",
                )
            )
    return flags
