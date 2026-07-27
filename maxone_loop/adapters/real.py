"""Real MaxOne adapter.

DELIBERATELY UNIMPLEMENTED.

Implementing this requires the installed MaxLab Live distribution: the vendor
``maxlab`` package, the local example scripts, and API documentation matching
the installed versions. None of those are available in this repository, and
CLAUDE.md section 4 forbids inferring undocumented API behaviour. Every value
this adapter would need -- electrode routing calls, stimulation-unit
assignment, the DAC neutral code, the volts-to-bits calibration, the offset
compensation sequence -- is exactly the kind of thing that must be read from
vendor documentation rather than guessed.

So the class exists to hold the contract and to fail closed. Each method raises
``AdapterError``. It must be implemented on the acquisition machine, against
the installed MaxLab version, and reviewed before any armed run.

The checklist below is the implementation contract from CLAUDE.md section 11.
Do not remove an item to make the class importable.
"""

from __future__ import annotations

from typing import Any, NoReturn

from ..reason_codes import ADAPTER_UNAVAILABLE
from .base import AdapterError, CycleContext, PreflightResult, StimProtocol, StimReceipt

#: Everything the real adapter must do, in order. Kept as data so preflight can
#: report it and so a reviewer can diff the implementation against it.
IMPLEMENTATION_CONTRACT: tuple[str, ...] = (
    "import the vendor maxlab module only inside this module",
    "verify mxwserver is reachable",
    "verify the installed MaxLab Live and API versions against configuration",
    "use the local vendor examples as the implementation reference",
    "verify electrode routing and unique stimulation-unit assignment",
    "download the routed array before stimulation",
    "run the required offset compensation",
    "begin recording before transmitting a nonzero stimulation sequence",
    "return the DAC to the documented neutral state after every pulse",
    "stop recording and clean up in finally",
    "never retry an ambiguous write automatically",
)

#: Values that must be verified against the installed hardware before this
#: adapter may convert a requested amplitude into a device command.
UNVERIFIED_CALIBRATION: tuple[str, ...] = (
    "dac_neutral_code",
    "nominal_mV_per_dac_bit",
    "voltage_amplifier_inverts_polarity",
)


def _unavailable(operation: str) -> NoReturn:
    raise AdapterError(
        ADAPTER_UNAVAILABLE,
        f"RealMaxOneAdapter.{operation} is not implemented. It requires the installed "
        "MaxLab Live distribution and its API documentation, which are not part of this "
        "repository. Implement and review it on the acquisition machine before any "
        "armed run.",
    )


class RealMaxOneAdapter:
    """Fail-closed placeholder for the vendor-backed adapter."""

    def __init__(self, **_: Any) -> None:
        # Constructing the adapter is allowed so that configuration validation
        # and adapter selection can be tested. Using it is not.
        self.contract = IMPLEMENTATION_CONTRACT
        self.unverified_calibration = UNVERIFIED_CALIBRATION

    def preflight(self) -> PreflightResult:
        return PreflightResult(
            ok=False,
            checks={item: False for item in IMPLEMENTATION_CONTRACT},
            detail={
                "reason_code": ADAPTER_UNAVAILABLE,
                "message": "real adapter is not implemented",
                "unverified_calibration": list(UNVERIFIED_CALIBRATION),
            },
        )

    def initialize(self) -> None:
        _unavailable("initialize")

    def configure_array(self, protocol: StimProtocol) -> None:
        _unavailable("configure_array")

    def start_recording(self, context: CycleContext) -> None:
        _unavailable("start_recording")

    def send_stimulation(self, protocol: StimProtocol) -> StimReceipt:
        _unavailable("send_stimulation")

    def stop_recording(self) -> None:
        _unavailable("stop_recording")

    def safe_shutdown(self) -> None:
        # Shutdown is the one call that must never raise: it runs in exception
        # cleanup. Nothing was ever initialized, so there is nothing to undo.
        return None
