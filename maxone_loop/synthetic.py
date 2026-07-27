"""Generate synthetic metrics workbooks.

Used by ``simulate`` mode and by the tests. The generator reproduces the
export's quirks on purpose -- ``"N/A"`` markers, the unnamed index column in
``Network - Burst Level``, the metadata split across rows, and placeholder rows
for zero-burst recordings -- so that a test passing against synthetic input is
evidence about the real input rather than about a tidied-up imitation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook

NA = "N/A"

_META_HEADERS = [
    "Instance", "Well Number", "Folder Path", "Wellplate ID", "Assay Run ID", "Assay Tag",
    "Start Time", "Stop Time", "Date", "Control", "Gain", "LSB [µV]",
    "Sampling Frequency [Hz]", "HPF [Hz]", "Number of Configurations",
    "Duration per Configuration [s]", "Well Group Name", "Well Group Color",
]

_PARAM_HEADERS = [
    "Instance", "Analysis Type", "Firing Rate Threshold [Hz]", "Amplitude Threshold [µV]",
    "ISI Threshold [ms]", "N",
]

_ACTIVITY_HEADERS = [
    "Instance", "Active Area [%]", "Mean Firing Rate [Hz]", "Firing Rate std [Hz]",
    "Firing Rate CV", "Median Firing Rate [Hz]", "Firing Rate 10th percentile [Hz]",
    "Firing Rate 90th percentile [Hz]", "Mean Spike Amplitude [µV]", "Spike Amplitude std [µV]",
    "Spike Amplitude CV", "Median Spike Amplitude [µV]", "Spike Amplitude 10th percentile [µV]",
    "Spike Amplitude 90th percentile [µV]", "Mean ISI [ms]", "ISI std [ms]", "ISI CV",
    "Median ISI [ms]", "ISI 10th percentile [ms]", "ISI 90th percentile [ms]",
    "Date", "Wellplate ID", "Well Number", "Assay Run ID", "DIV [days]",
    "Well Group Name", "Well Group Color", "Control", "Assay Tag",
]

_NETWORK_HEADERS = [
    "Instance", "Burst Frequency [Hz]", "Spikes within Bursts [%]", "Mean Spikes per Burst",
    "Date", "Wellplate ID", "Well Number", "Assay Run ID", "DIV [days]",
    "Well Group Name", "Well Group Color", "Control", "Assay Tag",
]

_BURST_HEADERS = [
    None, "Instance", "Spikes per Burst", "Spikes per Burst per Electrode",
    "Burst Duration [s]", "Burst Peak Firing Rate [Hz]", "IBI [s]", "Mean Burst ISI [ms]",
    "Burst ISI CV", "Date", "Wellplate ID", "Well Number", "Assay Run ID", "DIV [days]",
    "Well Group Name", "Well Group Color", "Control", "Assay Tag", "burstTime",
]


@dataclass
class SyntheticRecording:
    """One recording to place in a synthetic workbook."""

    wellplate_id: str
    assay_run_id: str
    mean_firing_rate_hz: float | None
    active_area_percent: float = 1.0
    #: ``None`` reproduces the single-electrode case: a mean with no dispersion.
    firing_rate_std_hz: float | None = 0.3
    firing_rate_cv: float | None = 0.5
    well_number: int = 1
    duration_seconds: float = 300.02
    number_of_configurations: int = 1
    sampling_frequency_hz: int = 20000
    gain: int = 512
    lsb_uv: float = 6.294
    hpf_hz: int = 1
    start: datetime = field(default_factory=lambda: datetime(2026, 4, 9, 13, 0, 0))
    burst_count: int = 3
    folder_path: str | None = None

    def path(self) -> str:
        if self.folder_path:
            return self.folder_path
        day = self.start.strftime("%y%m%d")
        return f"/home/mxwbio/Data/Synthetic/{day}/{self.wellplate_id}/Network/{self.assay_run_id}"


def _blank(headers: Sequence[Any]) -> dict[Any, Any]:
    return {h: NA for h in headers if h is not None}


def write_workbook(path: str | Path, recordings: Sequence[SyntheticRecording]) -> Path:
    """Write a metrics workbook containing ``recordings``."""
    workbook = Workbook()
    meta = workbook.active
    meta.title = "Meta Data"
    params = workbook.create_sheet("Analysis Parameters")
    activity = workbook.create_sheet("Activity - Well Level")
    network = workbook.create_sheet("Network - Well Level")
    bursts = workbook.create_sheet("Network - Burst Level")

    meta.append(_META_HEADERS)
    params.append(_PARAM_HEADERS)
    activity.append(_ACTIVITY_HEADERS)
    network.append(_NETWORK_HEADERS)
    bursts.append(_BURST_HEADERS)

    instance = 1
    burst_index = 0

    for recording in recordings:
        activity_instance = instance
        burst_instance = instance + 1
        instance += 2

        stop = recording.start + timedelta(seconds=recording.duration_seconds)
        for current in (activity_instance, burst_instance):
            meta.append([
                current,
                recording.well_number,
                recording.path(),
                recording.wellplate_id,
                recording.assay_run_id,
                f"Run #{recording.assay_run_id}",
                recording.start.strftime("%H:%M:%S"),
                stop.strftime("%H:%M:%S"),
                recording.start.strftime("%Y-%m-%d"),
                0,
                recording.gain,
                recording.lsb_uv,
                recording.sampling_frequency_hz,
                recording.hpf_hz,
                recording.number_of_configurations,
                recording.duration_seconds,
                "Default Group",
                "#999999",
            ])

        params.append([activity_instance, "Activity Analysis", 0.1, 20, 200, NA])
        params.append([burst_instance, "ISI-N Burst Detector", NA, NA, NA, 80])

        row = _blank(_ACTIVITY_HEADERS)
        row.update({
            "Instance": activity_instance,
            "Active Area [%]": recording.active_area_percent,
            "Mean Firing Rate [Hz]": NA if recording.mean_firing_rate_hz is None else recording.mean_firing_rate_hz,
            "Firing Rate std [Hz]": NA if recording.firing_rate_std_hz is None else recording.firing_rate_std_hz,
            "Firing Rate CV": NA if recording.firing_rate_cv is None else recording.firing_rate_cv,
            "Date": recording.start.strftime("%Y-%m-%d"),
            "Wellplate ID": recording.wellplate_id,
            "Well Number": recording.well_number,
            "Assay Run ID": recording.assay_run_id,
            "Well Group Name": "Default Group",
            "Well Group Color": "#999999",
            "Control": 0,
            "Assay Tag": f"Run #{recording.assay_run_id}",
        })
        activity.append([row[h] for h in _ACTIVITY_HEADERS])

        row = _blank(_NETWORK_HEADERS)
        row.update({
            "Instance": burst_instance,
            "Burst Frequency [Hz]": round(recording.burst_count / recording.duration_seconds, 4)
            if recording.burst_count
            else 0,
            "Date": recording.start.strftime("%Y-%m-%d"),
            "Wellplate ID": recording.wellplate_id,
            "Well Number": recording.well_number,
            "Assay Run ID": recording.assay_run_id,
            "Well Group Name": "Default Group",
            "Well Group Color": "#999999",
            "Control": 0,
            "Assay Tag": f"Run #{recording.assay_run_id}",
        })
        network.append([row[h] for h in _NETWORK_HEADERS])

        # Burst rows reproduce the export's split metadata: the group columns
        # appear only on the block's second row, and the identity columns
        # appear on every row except that one. A zero-burst recording still
        # emits two placeholder rows.
        block = max(recording.burst_count, 2)
        for position in range(block):
            row = _blank(_BURST_HEADERS)
            row["Instance"] = burst_instance if position == 0 or recording.burst_count else NA
            if position == 1:
                row.update({
                    "Well Group Name": "Default Group",
                    "Well Group Color": "#999999",
                    "Control": 0,
                    "Assay Tag": f"Run #{recording.assay_run_id}",
                })
            else:
                row.update({
                    "Date": recording.start.strftime("%Y-%m-%d"),
                    "Wellplate ID": recording.wellplate_id,
                    "Well Number": recording.well_number,
                    "Assay Run ID": recording.assay_run_id,
                })
            if recording.burst_count:
                row["Instance"] = burst_instance
                row["Spikes per Burst"] = 100 + position * 10
                row["burstTime"] = round(10.0 + position * 20.0, 2)
                if position == 1:
                    row.update({
                        "Date": recording.start.strftime("%Y-%m-%d"),
                        "Wellplate ID": recording.wellplate_id,
                        "Well Number": recording.well_number,
                        "Assay Run ID": recording.assay_run_id,
                    })
            values = [burst_index] + [row[h] for h in _BURST_HEADERS[1:]]
            bursts.append(values)
            burst_index += 1

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(destination)
    return destination


def export_filename(when: datetime | None = None) -> str:
    """The export naming convention: the timestamp is the export time."""
    moment = when or datetime.now()
    return f"metrics_data_{moment.strftime('%Y%m%d_%H%M%S')}.xlsx"
