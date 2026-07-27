"""Dry-run adapter: real inputs, no hardware writes.

Every call that would touch the device becomes a structured receipt appended to
a JSONL file, so a dry run produces the same audit trail an armed run would.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import CycleContext, PreflightResult, StimProtocol, StimReceipt


@dataclass
class DryRunMaxOneAdapter:
    log_path: Path | None = None
    receipts: list[dict[str, Any]] = field(default_factory=list)
    recording: bool = False

    def _emit(self, action: str, **detail: Any) -> dict[str, Any]:
        entry = {
            "utc": datetime.now(timezone.utc).isoformat(),
            "adapter": "dry_run",
            "action": action,
            "hardware_write": False,
            **detail,
        }
        self.receipts.append(entry)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        return entry

    def preflight(self) -> PreflightResult:
        self._emit("preflight")
        return PreflightResult(
            ok=True,
            checks={"dry_run": True},
            detail={"note": "no hardware contacted"},
        )

    def initialize(self) -> None:
        self._emit("initialize")

    def configure_array(self, protocol: StimProtocol) -> None:
        self._emit("configure_array", protocol=protocol.as_dict())

    def start_recording(self, context: CycleContext) -> None:
        self._emit(
            "start_recording",
            experiment_id=context.experiment_id,
            cycle_index=context.cycle_index,
            amplitude_mV=str(context.amplitude_mV),
        )
        self.recording = True

    def send_stimulation(self, protocol: StimProtocol) -> StimReceipt:
        self._emit("send_stimulation", protocol=protocol.as_dict())
        return StimReceipt(
            status="DRY_RUN",
            protocol_id=protocol.protocol_id,
            amplitude_mV=protocol.amplitude_mV,
            detail={"note": "no waveform transmitted"},
        )

    def stop_recording(self) -> None:
        self._emit("stop_recording")
        self.recording = False

    def safe_shutdown(self) -> None:
        self._emit("safe_shutdown", dac_returned_to_neutral=True)
        self.recording = False
