"""Cycle orchestration.

Ties the watcher, reader, selection, QC, policy, adapter, and ledger together.
The ordering constraints here are safety properties, not style:

* the recording identity is claimed in the ledger *before* any stimulation, so
  a crash between claim and stimulation loses a cycle rather than repeating one;
* recording starts before a nonzero waveform is transmitted;
* ``safe_shutdown`` runs in ``finally``, including on the exception path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .adapters.base import (
    AdapterError,
    CycleContext,
    MaxOneAdapter,
    StimProtocol,
    StimReceipt,
)
from .adapters.dry_run import DryRunMaxOneAdapter
from .adapters.real import RealMaxOneAdapter
from .adapters.simulated import SimulatedMaxOneAdapter
from .config.loader import config_fingerprint, sha256_file
from .config.models import AppConfig
from .ledger import DeviceLock, Ledger, RecordingAlreadyConsumed, Transition
from .metrics.extract import AnalysisResult, extract
from .metrics.qc import QCReport, evaluate
from .metrics.selection import SelectionError, select_recording
from .metrics.workbook import MetricsWorkbook, WorkbookError
from .policy import CycleState, Decision, baseline_decision, decide
from .reason_codes import (
    AMBIGUOUS_STIM_RECEIPT,
    ARMED_ENVIRONMENT_GATE_CLOSED,
    ARMED_REQUIREMENT_MISSING,
    METRICS_TIMEOUT,
    PREFLIGHT_FAILED,
    RECORDING_ALREADY_CONSUMED,
)
from .state import State, check_transition
from .watcher import MetricsWatcher


class OrchestratorError(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


@dataclass
class CycleOutcome:
    cycle_index: int
    decision: Decision
    analysis: AnalysisResult | None = None
    qc: QCReport | None = None
    workbook: Path | None = None
    receipt: StimReceipt | None = None


@dataclass
class RunSummary:
    experiment_id: str
    mode: str
    final_state: State
    reason_code: str
    cycles: list[CycleOutcome] = field(default_factory=list)
    #: Failures raised by stop_recording/safe_shutdown during cleanup. Recorded
    #: rather than raised so they cannot mask the fault that triggered them.
    cleanup_errors: list[str] = field(default_factory=list)

    @property
    def applied_amplitudes(self) -> list[str]:
        return [
            str(c.decision.candidate_amplitude_mV)
            for c in self.cycles
            if c.decision.candidate_amplitude_mV is not None
        ]


def build_adapter(config: AppConfig, *, log_directory: Path | None = None) -> MaxOneAdapter:
    """Pick the adapter for the configured mode. Never defaults to hardware."""
    mode = config.experiment.experiment.mode
    if mode == "simulate":
        return SimulatedMaxOneAdapter()
    if mode == "dry_run":
        log_path = None if log_directory is None else log_directory / "dry_run_receipts.jsonl"
        return DryRunMaxOneAdapter(log_path=log_path)
    if mode == "armed":
        return RealMaxOneAdapter()
    raise OrchestratorError("CONFIG_INVALID", f"unknown mode {mode!r}")


def check_armed_gate(config: AppConfig, environ: dict[str, str] | None = None) -> None:
    """Every armed precondition that can be checked without hardware.

    Called for armed mode only. It raises rather than returning a flag, because
    a caller that forgets to inspect a boolean would proceed to hardware.
    """
    env = os.environ if environ is None else environ
    runtime = config.experiment.runtime

    if runtime.require_hardware_environment_gate:
        value = env.get(runtime.hardware_environment_variable)
        if value != runtime.hardware_environment_required_value:
            raise OrchestratorError(
                ARMED_ENVIRONMENT_GATE_CLOSED,
                f"{runtime.hardware_environment_variable} must be "
                f"{runtime.hardware_environment_required_value!r} for armed mode "
                f"(currently {value!r})",
            )

    missing = config.missing_for_armed()
    if missing:
        raise OrchestratorError(
            ARMED_REQUIREMENT_MISSING,
            "armed mode requires these settings to be non-null: " + ", ".join(missing),
        )


class Orchestrator:
    """Runs the adaptive loop for one experiment."""

    def __init__(
        self,
        config: AppConfig,
        *,
        adapter: MaxOneAdapter | None = None,
        ledger: Ledger | None = None,
        watcher: MetricsWatcher | None = None,
        cooldown: Callable[[float], None] | None = None,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.experiment_id = config.experiment.experiment.experiment_id
        self.mode = config.experiment.experiment.mode

        paths = config.experiment.paths
        self.state_directory = Path(paths.state_directory)
        self.log_directory = Path(paths.log_directory)

        self.ledger = ledger or Ledger(self.state_directory)
        self.adapter = adapter or build_adapter(config, log_directory=self.log_directory)
        self.watcher = watcher
        self._cooldown = cooldown or (lambda _seconds: None)
        self._now = now

        self.state = State.IDLE
        self._started_at: datetime | None = None

    # -- plumbing -----------------------------------------------------------

    def _transition(self, nxt: State, **fields: Any) -> None:
        check_transition(self.state, nxt)
        self.ledger.record_transition(
            self.experiment_id, Transition(previous_state=self.state, next_state=nxt, **fields)
        )
        self.state = nxt

    def _elapsed_minutes(self) -> float:
        if self._started_at is None:
            return 0.0
        return (self._now() - self._started_at).total_seconds() / 60.0

    def _stim_protocol(self, amplitude: Decimal) -> StimProtocol:
        stimulation = self.config.stimulation_protocols.stimulation
        return StimProtocol(
            protocol_id=stimulation.protocol_id,
            amplitude_mV=amplitude,
            electrodes=stimulation.stimulation_electrodes,
            waveform=stimulation.waveform.model_dump(),
        )

    # -- one cycle ----------------------------------------------------------

    def process_workbook(self, path: Path, cycle_index: int) -> CycleOutcome:
        """Read, select, validate, and decide for one workbook.

        Does not touch hardware. Raises ``OrchestratorError`` on any fault so
        that the caller transitions to FAULT with a stable reason code.
        """
        schema = self.config.metrics_schema
        digest = sha256_file(path)

        self._transition(
            State.SELECTING_RECORDING,
            cycle_index=cycle_index,
            workbook_path=str(path),
            workbook_sha256=digest,
        )

        try:
            workbook = MetricsWorkbook(path, schema)
            selection = select_recording(
                workbook,
                schema,
                consumed_folder_paths=self.ledger.consumed_folder_paths(self.experiment_id),
            )
        except (WorkbookError, SelectionError) as exc:
            raise OrchestratorError(exc.reason_code, exc.message) from exc

        folder_path = selection.recording.identity.folder_path

        self._transition(
            State.VALIDATING,
            cycle_index=cycle_index,
            workbook_path=str(path),
            workbook_sha256=digest,
            folder_path=folder_path,
        )

        try:
            analysis = extract(workbook, schema, selection, workbook_sha256=digest)
        except WorkbookError as exc:
            raise OrchestratorError(exc.reason_code, exc.message) from exc

        qc = evaluate(analysis, schema)

        self._transition(
            State.ANALYZING,
            cycle_index=cycle_index,
            workbook_path=str(path),
            workbook_sha256=digest,
            folder_path=folder_path,
            metrics=analysis.to_record(),
            qc=qc.as_records(),
        )

        # Claim the recording before deciding. A crash after this point costs a
        # cycle; a crash before it costs nothing. Repeating a stimulation is the
        # outcome that must be impossible.
        try:
            self.ledger.consume_recording(
                experiment_id=self.experiment_id,
                folder_path=folder_path,
                workbook_sha256=digest,
                workbook_filename=path.name,
                cycle_index=cycle_index,
            )
        except RecordingAlreadyConsumed as exc:
            raise OrchestratorError(RECORDING_ALREADY_CONSUMED, str(exc)) from exc

        state = CycleState(
            cycle_index=cycle_index,
            previous_amplitude_mV=self._last_amplitude(),
            cumulative_stimulations=self.ledger.cumulative_stimulations(self.experiment_id),
            elapsed_minutes=self._elapsed_minutes(),
        )
        decision = decide(self.config, analysis, qc, state)

        self._transition(
            State.DECIDING,
            cycle_index=cycle_index,
            workbook_path=str(path),
            workbook_sha256=digest,
            folder_path=folder_path,
            metrics=analysis.to_record(),
            qc=qc.as_records(),
            decision=decision.action,
            reason_code=decision.reason_code,
        )

        return CycleOutcome(
            cycle_index=cycle_index, decision=decision, analysis=analysis, qc=qc, workbook=path
        )

    def _last_amplitude(self) -> Decimal:
        rows = self.ledger.stimulations(self.experiment_id)
        if not rows:
            return self.config.experiment.adaptive_policy.baseline_amplitude_mV
        return Decimal(rows[-1]["amplitude_mV"])

    def apply_decision(self, outcome: CycleOutcome) -> StimReceipt | None:
        """Carry out a STIMULATE decision. Never called for RECORD."""
        decision = outcome.decision
        if decision.action != "STIMULATE" or decision.candidate_amplitude_mV is None:
            return None

        amplitude = decision.candidate_amplitude_mV
        protocol = self._stim_protocol(amplitude)

        self._transition(
            State.SAFETY_CHECK,
            cycle_index=outcome.cycle_index,
            applied_amplitude_mV=amplitude,
            protocol_id=protocol.protocol_id,
            reason_code=decision.reason_code,
        )
        self._transition(State.COOLDOWN, cycle_index=outcome.cycle_index)
        self._cooldown(self.config.experiment.adaptive_policy.minimum_cooldown_seconds)

        self._transition(
            State.STIMULATING,
            cycle_index=outcome.cycle_index,
            applied_amplitude_mV=amplitude,
            protocol_id=protocol.protocol_id,
        )

        context = CycleContext(
            experiment_id=self.experiment_id,
            cycle_index=outcome.cycle_index + 1,
            amplitude_mV=amplitude,
        )
        # Recording must be running before a nonzero waveform is transmitted.
        self.adapter.configure_array(protocol)
        self.adapter.start_recording(context)
        try:
            receipt = self.adapter.send_stimulation(protocol)
        except AdapterError as exc:
            self.ledger.record_stimulation(
                experiment_id=self.experiment_id,
                cycle_index=outcome.cycle_index,
                amplitude_mV=amplitude,
                protocol_id=protocol.protocol_id,
                status="AMBIGUOUS",
                receipt={"error": exc.message, "reason_code": exc.reason_code},
            )
            raise OrchestratorError(exc.reason_code, exc.message) from exc

        self.ledger.record_stimulation(
            experiment_id=self.experiment_id,
            cycle_index=outcome.cycle_index,
            amplitude_mV=amplitude,
            protocol_id=protocol.protocol_id,
            status=receipt.status,
            receipt=receipt.as_dict(),
        )

        if not receipt.certain:
            # Never retried automatically. The operator resolves it.
            raise OrchestratorError(
                AMBIGUOUS_STIM_RECEIPT,
                f"stimulation receipt status is {receipt.status}; delivery is not "
                "certain and must not be retried automatically",
            )

        self._transition(State.RECORDING, cycle_index=outcome.cycle_index)
        outcome.receipt = receipt
        return receipt

    # -- whole run ----------------------------------------------------------

    def run(self, workbooks: list[Path] | None = None) -> RunSummary:
        """Run the loop.

        ``workbooks`` replays a fixed sequence (used by ``replay`` and by the
        tests). When omitted, the configured watcher supplies them.
        """
        config = self.config
        self._started_at = self._now()

        self.ledger.start_run(
            experiment_id=self.experiment_id,
            mode=self.mode,
            config_fingerprint=config_fingerprint(config),
            source_hashes=config.source_hashes,
            protocol_id=config.stimulation_protocols.stimulation.protocol_id,
            maxlab_live_version=config.stimulation_protocols.hardware.maxlab_live_version,
            api_version=config.stimulation_protocols.hardware.api_version,
        )

        summary = RunSummary(
            experiment_id=self.experiment_id,
            mode=self.mode,
            final_state=State.IDLE,
            reason_code="",
        )

        lock = DeviceLock(self.state_directory) if config.experiment.runtime.require_exclusive_device_lock else None
        if lock is not None:
            lock.acquire()

        try:
            # Inside the try so that a refused armed run is recorded as a FAULT
            # transition rather than vanishing as an uncaught exception.
            if self.mode == "armed":
                check_armed_gate(config)

            self._transition(State.PREFLIGHT)
            preflight = self.adapter.preflight()
            if not preflight.ok:
                raise OrchestratorError(
                    PREFLIGHT_FAILED,
                    "preflight failed: " + ", ".join(preflight.failures),
                )
            self.adapter.initialize()

            # Cycle 0: baseline. Recording only -- no stimulation sequence.
            self._transition(State.BASELINE_RECORDING, cycle_index=0)
            baseline = baseline_decision()
            self.adapter.start_recording(
                CycleContext(
                    experiment_id=self.experiment_id,
                    cycle_index=0,
                    amplitude_mV=config.experiment.adaptive_policy.baseline_amplitude_mV,
                )
            )
            summary.cycles.append(CycleOutcome(cycle_index=0, decision=baseline))

            queue = list(workbooks) if workbooks is not None else None
            cycle_index = 0

            while True:
                self._transition(State.WAITING_FOR_METRICS, cycle_index=cycle_index)

                if queue is not None:
                    if not queue:
                        summary.final_state = State.COMPLETE
                        summary.reason_code = METRICS_TIMEOUT
                        self._transition(State.COMPLETE, cycle_index=cycle_index)
                        break
                    path = queue.pop(0)
                else:
                    if self.watcher is None:
                        raise OrchestratorError(
                            "CONFIG_INVALID", "no watcher configured and no workbooks supplied"
                        )
                    found = self.watcher.wait_for_workbook()
                    if found is None:
                        summary.final_state = State.COMPLETE
                        summary.reason_code = METRICS_TIMEOUT
                        self._transition(State.COMPLETE, cycle_index=cycle_index)
                        break
                    path = found

                outcome = self.process_workbook(path, cycle_index)
                summary.cycles.append(outcome)

                if outcome.decision.action == "FAULT":
                    raise OrchestratorError(
                        outcome.decision.reason_code, outcome.decision.message
                    )
                if outcome.decision.action == "COMPLETE":
                    self._transition(
                        State.COMPLETE,
                        cycle_index=cycle_index,
                        reason_code=outcome.decision.reason_code,
                    )
                    summary.final_state = State.COMPLETE
                    summary.reason_code = outcome.decision.reason_code
                    break

                self.apply_decision(outcome)
                cycle_index += 1

        except OrchestratorError as exc:
            self._transition(State.FAULT, reason_code=exc.reason_code)
            summary.final_state = State.FAULT
            summary.reason_code = exc.reason_code
        finally:
            # Cleanup is best-effort and must never replace the run's outcome:
            # a failing stop_recording would otherwise mask the fault that
            # caused it. Failures are recorded on the summary instead.
            for step in (self.adapter.stop_recording, self.adapter.safe_shutdown):
                try:
                    step()
                except Exception as exc:  # noqa: BLE001 - cleanup must not raise
                    summary.cleanup_errors.append(f"{step.__name__}: {exc}")
            if lock is not None:
                lock.release()

        return summary
