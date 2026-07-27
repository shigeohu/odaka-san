"""Select the one recording that belongs to the active cycle.

A workbook may hold many unrelated recordings (CLAUDE.md section 2.4), so
picking the wrong row silently applies another chip's firing rate to this
experiment's stimulation decision. Selection is a safety gate: exactly one
match, no fallbacks, and every other outcome is a fault.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.models import MetricsSchema
from ..reason_codes import (
    ACTIVITY_INSTANCE_AMBIGUOUS,
    ACTIVITY_INSTANCE_NOT_FOUND,
    RECORDING_ALREADY_CONSUMED,
    RECORDING_AMBIGUOUS,
    RECORDING_NOT_FOUND,
)
from .workbook import MetricsWorkbook, Recording


class SelectionError(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass(frozen=True)
class Selection:
    recording: Recording
    activity_instance: int
    #: Every candidate considered, for the ledger's audit trail.
    considered: tuple[str, ...]


def _matches(recording: Recording, schema: MetricsSchema) -> bool:
    rule = schema.recording_selection
    identity = recording.identity

    if rule.strategy == "folder_path_exact":
        return identity.folder_path == rule.folder_path
    if rule.strategy == "folder_path_prefix":
        return identity.folder_path.startswith(rule.folder_path or "")
    if rule.strategy == "wellplate_and_run_id":
        return (
            identity.wellplate_id == rule.wellplate_id
            and identity.well_number == rule.well_number
            and identity.assay_run_id == rule.assay_run_id
        )
    raise SelectionError(
        RECORDING_NOT_FOUND,
        "recording_selection.strategy is not configured; refusing to guess which "
        "recording belongs to this cycle",
    )


def select_recording(
    workbook: MetricsWorkbook,
    schema: MetricsSchema,
    *,
    consumed_folder_paths: frozenset[str] = frozenset(),
) -> Selection:
    """Return the single recording for this cycle, or raise ``SelectionError``.

    ``consumed_folder_paths`` are recording identities already used by an
    earlier cycle of this experiment. They are rejected even when they arrive
    in a workbook whose hash has never been seen before, because a re-export
    legitimately repeats earlier recordings.
    """
    recordings = workbook.recordings()
    considered = tuple(sorted(recordings))

    matched = [r for r in recordings.values() if _matches(r, schema)]

    if not matched:
        raise SelectionError(
            RECORDING_NOT_FOUND,
            f"no recording in {workbook.path.name} matches "
            f"{schema.recording_selection.strategy}; {len(recordings)} recording(s) present",
        )
    if len(matched) > 1:
        paths = ", ".join(sorted(r.identity.folder_path for r in matched))
        raise SelectionError(
            RECORDING_AMBIGUOUS,
            f"{len(matched)} recordings in {workbook.path.name} match the selection "
            f"rule: {paths}",
        )

    recording = matched[0]

    if recording.identity.folder_path in consumed_folder_paths:
        raise SelectionError(
            RECORDING_ALREADY_CONSUMED,
            f"recording {recording.identity.folder_path} was already consumed by an "
            "earlier cycle of this experiment",
        )

    activity_type = schema.instance_join.activity_analysis_type
    instances = recording.instances.get(activity_type, ())
    if not instances:
        raise SelectionError(
            ACTIVITY_INSTANCE_NOT_FOUND,
            f"recording {recording.identity.folder_path} has no {activity_type!r} instance",
        )
    if len(instances) > 1:
        raise SelectionError(
            ACTIVITY_INSTANCE_AMBIGUOUS,
            f"recording {recording.identity.folder_path} has {len(instances)} "
            f"{activity_type!r} instances: {sorted(instances)}",
        )

    return Selection(
        recording=recording, activity_instance=instances[0], considered=considered
    )
