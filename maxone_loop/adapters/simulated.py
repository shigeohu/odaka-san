"""Simulated adapter. Never imports or contacts the vendor API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .base import CycleContext, PreflightResult, StimProtocol, StimReceipt


@dataclass
class SimulatedMaxOneAdapter:
    """Records the call sequence so tests can assert on it."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    recording: bool = False
    dac_neutral: bool = True

    def _log(self, name: str, **detail: Any) -> None:
        self.calls.append((name, detail))

    def preflight(self) -> PreflightResult:
        self._log("preflight")
        return PreflightResult(ok=True, checks={"simulated": True})

    def initialize(self) -> None:
        self._log("initialize")

    def configure_array(self, protocol: StimProtocol) -> None:
        self._log("configure_array", protocol=protocol.as_dict())

    def start_recording(self, context: CycleContext) -> None:
        self._log("start_recording", cycle_index=context.cycle_index)
        self.recording = True

    def send_stimulation(self, protocol: StimProtocol) -> StimReceipt:
        if not self.recording:
            # Recording must already be running before a nonzero sequence is
            # transmitted, otherwise the stimulation is not captured.
            raise AssertionError("send_stimulation called while not recording")
        self._log("send_stimulation", protocol=protocol.as_dict())
        self.dac_neutral = False
        try:
            return StimReceipt(
                status="SIMULATED",
                protocol_id=protocol.protocol_id,
                amplitude_mV=protocol.amplitude_mV,
            )
        finally:
            self.dac_neutral = True

    def stop_recording(self) -> None:
        self._log("stop_recording")
        self.recording = False

    def safe_shutdown(self) -> None:
        self._log("safe_shutdown")
        self.recording = False
        self.dac_neutral = True

    # -- test helpers -------------------------------------------------------

    @property
    def call_names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def stimulation_count(self) -> int:
        return self.call_names.count("send_stimulation")
