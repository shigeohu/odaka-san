"""Extract the decision metric from a selected recording.

MaxLab Live has already aggregated the metric to the well level, so there is
no aggregation and no spike counting here -- one scalar is read from one cell
(CLAUDE.md section 9). Everything else this module collects exists to make the
resulting decision auditable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from .. import ANALYSIS_VERSION
from ..config.models import MetricsSchema
from ..reason_codes import COLUMN_MISSING
from .selection import Selection
from .workbook import MetricsWorkbook, RecordingIdentity, WorkbookError


@dataclass(frozen=True)
class ElectrodeEstimate:
    """Electrode count derived from ``Active Area [%]``.

    The denominator is not stated in the workbook, so this is an inference and
    is labelled as one everywhere it is stored. See CLAUDE.md section 4.1.
    """

    count: float | None
    denominator: int | None
    verified: bool = False


@dataclass(frozen=True)
class AnalysisResult:
    """The full provenance record behind one firing-rate reading."""

    mean_firing_frequency_hz: float | None

    source_sheet: str
    source_column: str
    source_instance: int

    identity: RecordingIdentity

    active_area_percent: float | None
    electrodes: ElectrodeEstimate
    firing_rate_std_hz: float | None
    firing_rate_cv: float | None

    duration_seconds: float | None
    number_of_configurations: int | None
    sampling_frequency_hz: float | None
    gain: float | None
    lsb_uv: float | None
    hpf_hz: float | None

    recording_start: datetime | None
    recording_stop: datetime | None

    analysis_parameters: dict[str, Any]

    workbook_filename: str
    workbook_sha256: str
    schema_version: int
    analysis_version: int = ANALYSIS_VERSION

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["identity"] = self.identity.as_dict()
        record["electrodes"] = asdict(self.electrodes)
        record["recording_start"] = self.recording_start.isoformat() if self.recording_start else None
        record["recording_stop"] = self.recording_stop.isoformat() if self.recording_stop else None
        return record


def extract(
    workbook: MetricsWorkbook,
    schema: MetricsSchema,
    selection: Selection,
    *,
    workbook_sha256: str,
) -> AnalysisResult:
    """Read the decision metric and its supporting context."""
    metric = schema.decision_metric
    activity_columns = schema.supporting_columns.activity_well_level
    sheet = workbook.sheet(metric.sheet)
    sheet.require(
        metric.column,
        activity_columns.active_area_percent,
        activity_columns.firing_rate_std,
        activity_columns.firing_rate_cv,
    )

    instance = selection.activity_instance
    row = workbook.activity_row(instance)
    if row is None:
        raise WorkbookError(
            COLUMN_MISSING,
            f"sheet {sheet.name!r} has no row for Instance {instance}",
        )

    where = f"{sheet.name}!Instance={instance}"
    rate = workbook.as_float(row[metric.column], where=f"{where}.{metric.column}")
    active_area = workbook.as_float(
        row[activity_columns.active_area_percent], where=f"{where}.active_area"
    )
    std = workbook.as_float(row[activity_columns.firing_rate_std], where=f"{where}.std")
    cv = workbook.as_float(row[activity_columns.firing_rate_cv], where=f"{where}.cv")

    denominator = schema.quality_control.active_area_denominator_electrodes
    electrodes = ElectrodeEstimate(
        count=None if (active_area is None or denominator is None) else active_area / 100.0 * denominator,
        denominator=denominator,
        verified=False,
    )

    recording = selection.recording
    acquisition = recording.acquisition

    return AnalysisResult(
        mean_firing_frequency_hz=rate,
        source_sheet=sheet.name,
        source_column=metric.column,
        source_instance=instance,
        identity=recording.identity,
        active_area_percent=active_area,
        electrodes=electrodes,
        firing_rate_std_hz=std,
        firing_rate_cv=cv,
        duration_seconds=acquisition.duration_seconds,
        number_of_configurations=acquisition.number_of_configurations,
        sampling_frequency_hz=acquisition.sampling_frequency_hz,
        gain=acquisition.gain,
        lsb_uv=acquisition.lsb_uv,
        hpf_hz=acquisition.hpf_hz,
        recording_start=recording.start,
        recording_stop=recording.stop,
        analysis_parameters=_analysis_parameters(workbook, instance),
        workbook_filename=workbook.path.name,
        workbook_sha256=workbook_sha256,
        schema_version=schema.schema_version,
    )


def _analysis_parameters(workbook: MetricsWorkbook, instance: int) -> dict[str, Any]:
    """The Analysis Parameters row as applied, for provenance and QC."""
    from .workbook import MISSING

    row = workbook.analysis_parameter_row(instance)
    if row is None:
        return {}
    return {key: (None if value is MISSING else value) for key, value in row.items()}
