"""The hardware boundary.

Every vendor call goes through ``MaxOneAdapter``. Nothing above this line
imports ``maxlab``, so the policy, reader, and orchestrator are testable
without the vendor package installed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

#: A stimulation whose delivery status is not knowable. It is never retried
#: automatically (CLAUDE.md section 5).
StimStatus = Literal["DELIVERED", "AMBIGUOUS", "REJECTED", "DRY_RUN", "SIMULATED"]


@dataclass(frozen=True)
class StimProtocol:
    """One fully-resolved stimulation, ready to transmit."""

    protocol_id: str
    amplitude_mV: Decimal
    electrodes: tuple[int, ...]
    waveform: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_id": self.protocol_id,
            "amplitude_mV": str(self.amplitude_mV),
            "electrodes": list(self.electrodes),
            "waveform": dict(self.waveform),
        }


@dataclass(frozen=True)
class CycleContext:
    experiment_id: str
    cycle_index: int
    amplitude_mV: Decimal


@dataclass(frozen=True)
class StimReceipt:
    status: StimStatus
    protocol_id: str
    amplitude_mV: Decimal
    utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def certain(self) -> bool:
        return self.status in ("DELIVERED", "DRY_RUN", "SIMULATED")

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "protocol_id": self.protocol_id,
            "amplitude_mV": str(self.amplitude_mV),
            "utc": self.utc,
            "detail": dict(self.detail),
        }


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    checks: dict[str, bool]
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(name for name, passed in self.checks.items() if not passed)


@runtime_checkable
class MaxOneAdapter(Protocol):
    def preflight(self) -> PreflightResult: ...

    def initialize(self) -> None: ...

    def configure_array(self, protocol: StimProtocol) -> None: ...

    def start_recording(self, context: CycleContext) -> None: ...

    def send_stimulation(self, protocol: StimProtocol) -> StimReceipt: ...

    def stop_recording(self) -> None: ...

    def safe_shutdown(self) -> None: ...


class AdapterError(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
