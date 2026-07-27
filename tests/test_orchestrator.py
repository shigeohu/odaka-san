"""End-to-end orchestration tests (CLAUDE.md section 14, "Runtime")."""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from maxone_loop.adapters.base import AdapterError, StimReceipt
from maxone_loop.adapters.real import RealMaxOneAdapter
from maxone_loop.adapters.simulated import SimulatedMaxOneAdapter
from maxone_loop.ledger import Ledger
from maxone_loop.orchestrator import Orchestrator, OrchestratorError, build_adapter
from maxone_loop.reason_codes import (
    ADAPTER_UNAVAILABLE,
    ARMED_ENVIRONMENT_GATE_CLOSED,
    AMBIGUOUS_STIM_RECEIPT,
    API_ERROR,
    BELOW_THRESHOLD_STEP_UP,
    PREFLIGHT_FAILED,
    RECORDING_ALREADY_CONSUMED,
    SINGLE_ELECTRODE_MEAN,
    THRESHOLD_REACHED,
)
from maxone_loop.state import State
from maxone_loop.synthetic import SyntheticRecording, write_workbook


@pytest.fixture
def cycle_config(runnable_config):
    """Selection scoped to the synthetic namespace, cooldown disabled."""

    def build(**extra):
        overrides = {
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
            "experiment.adaptive_policy.minimum_cooldown_seconds": 0,
        }
        overrides.update(extra)
        return runnable_config(**overrides)

    return build


def workbook_for(tmp_path: Path, name: str, recording: SyntheticRecording) -> Path:
    return write_workbook(tmp_path / "wb" / name, [recording])


def make(config, adapter=None):
    return Orchestrator(config, adapter=adapter or SimulatedMaxOneAdapter())


# --- adapter selection -----------------------------------------------------


def test_mode_selects_the_adapter_and_never_defaults_to_hardware(runnable_config):
    assert isinstance(build_adapter(runnable_config()), object)
    assert type(build_adapter(runnable_config(**{"experiment.experiment.mode": "simulate"}))).__name__ == (
        "SimulatedMaxOneAdapter"
    )
    assert type(build_adapter(runnable_config(**{"experiment.experiment.mode": "dry_run"}))).__name__ == (
        "DryRunMaxOneAdapter"
    )
    assert type(build_adapter(runnable_config(**{"experiment.experiment.mode": "armed"}))).__name__ == (
        "RealMaxOneAdapter"
    )


def test_real_adapter_fails_closed():
    adapter = RealMaxOneAdapter()
    assert adapter.preflight().ok is False

    for call in ("initialize", "stop_recording"):
        with pytest.raises(AdapterError) as exc:
            getattr(adapter, call)()
        assert exc.value.reason_code == ADAPTER_UNAVAILABLE

    # safe_shutdown must never raise: it runs during exception cleanup.
    assert adapter.safe_shutdown() is None


def test_armed_run_stops_at_preflight(cycle_config, tmp_path, monkeypatch):
    """Even with a complete config and the gate open, the real adapter refuses."""
    monkeypatch.setenv("MAXONE_HARDWARE_ENABLED", "1")
    config = cycle_config(
        **{
            "experiment.experiment.mode": "armed",
            "experiment.paths.metrics_watch_directory": str(tmp_path),
        }
    )
    orchestrator = Orchestrator(config, adapter=RealMaxOneAdapter())
    summary = orchestrator.run(workbooks=[])

    assert summary.final_state is State.FAULT
    assert summary.reason_code == PREFLIGHT_FAILED


def test_armed_run_without_the_environment_gate_faults(cycle_config, tmp_path, monkeypatch):
    """A refused armed run is recorded, not raised past the caller."""
    monkeypatch.delenv("MAXONE_HARDWARE_ENABLED", raising=False)
    config = cycle_config(
        **{
            "experiment.experiment.mode": "armed",
            "experiment.paths.metrics_watch_directory": str(tmp_path),
        }
    )
    ledger = Ledger(config.experiment.paths.state_directory)
    summary = Orchestrator(config, adapter=RealMaxOneAdapter(), ledger=ledger).run(workbooks=[])

    assert summary.final_state is State.FAULT
    assert summary.reason_code == ARMED_ENVIRONMENT_GATE_CLOSED
    rows = ledger.transitions(config.experiment.experiment.experiment_id)
    assert rows[-1]["next_state"] == "FAULT"


# --- baseline --------------------------------------------------------------


def test_baseline_performs_no_stimulation(cycle_config, tmp_path):
    config = cycle_config()
    adapter = SimulatedMaxOneAdapter()
    orchestrator = Orchestrator(config, adapter=adapter)
    orchestrator.run(workbooks=[])

    assert adapter.stimulation_count() == 0
    assert "start_recording" in adapter.call_names
    assert adapter.call_names[-1] == "safe_shutdown"


def test_baseline_decision_is_record_not_stimulate(cycle_config):
    config = cycle_config()
    summary = make(config).run(workbooks=[])
    assert summary.cycles[0].decision.action == "RECORD"


# --- the loop --------------------------------------------------------------


def test_below_threshold_steps_up_then_completes(cycle_config, tmp_path):
    config = cycle_config()
    low = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))
    high = workbook_for(tmp_path, "metrics_data_20260101_000100.xlsx", SyntheticRecording("P2", "000002", 1.5))

    adapter = SimulatedMaxOneAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=[low, high])

    assert summary.final_state is State.COMPLETE
    assert summary.reason_code == THRESHOLD_REACHED
    assert adapter.stimulation_count() == 1
    assert summary.cycles[1].decision.reason_code == BELOW_THRESHOLD_STEP_UP
    assert summary.cycles[1].decision.candidate_amplitude_mV == Decimal("100")


def test_amplitude_increments_one_step_per_accepted_recording(cycle_config, tmp_path):
    config = cycle_config()
    paths = [
        workbook_for(
            tmp_path,
            f"metrics_data_2026010{i}_000000.xlsx",
            SyntheticRecording(f"P{i}", f"00000{i}", 0.2),
        )
        for i in range(1, 4)
    ]
    adapter = SimulatedMaxOneAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=paths)

    assert adapter.stimulation_count() == 3
    # Values carry the configured step's scale; Decimal keeps them exact.
    assert [Decimal(a) for a in summary.applied_amplitudes] == [
        Decimal("0"), Decimal("100"), Decimal("200"), Decimal("300")
    ]


def test_qc_rejection_faults_without_stimulating(cycle_config, tmp_path):
    """A single-electrode recording must stop the run, not step the amplitude."""
    config = cycle_config()
    path = workbook_for(
        tmp_path,
        "metrics_data_20260101_000000.xlsx",
        # Active area is comfortably above the minimum, so the only thing
        # wrong with this recording is the missing dispersion -- which is
        # exactly the single-electrode signature.
        SyntheticRecording("P1", "000001", 0.4, active_area_percent=5.0,
                           firing_rate_std_hz=None, firing_rate_cv=None),
    )
    adapter = SimulatedMaxOneAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=[path])

    assert summary.final_state is State.FAULT
    assert summary.reason_code == SINGLE_ELECTRODE_MEAN
    assert adapter.stimulation_count() == 0


# --- idempotency -----------------------------------------------------------


def test_the_same_workbook_twice_stimulates_once(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    adapter = SimulatedMaxOneAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=[path, path])

    assert adapter.stimulation_count() == 1
    assert summary.final_state is State.FAULT
    assert summary.reason_code == RECORDING_ALREADY_CONSUMED


def test_a_re_export_under_a_new_name_still_stimulates_once(cycle_config, tmp_path):
    """Same recording, new filename and new hash: still one stimulation."""
    config = cycle_config()
    recording = SyntheticRecording("P1", "000001", 0.4)
    first = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", recording)
    second = tmp_path / "wb" / "metrics_data_20260101_010000.xlsx"
    shutil.copy(first, second)
    second.write_bytes(second.read_bytes())  # same content, different path

    adapter = SimulatedMaxOneAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=[first, second])

    assert adapter.stimulation_count() == 1
    assert summary.reason_code == RECORDING_ALREADY_CONSUMED


def test_recording_is_claimed_before_stimulation(cycle_config, tmp_path):
    """A crash during stimulation must not leave the recording re-consumable."""
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    class ExplodingAdapter(SimulatedMaxOneAdapter):
        def send_stimulation(self, protocol):
            raise AdapterError(API_ERROR, "device vanished mid-write")

    ledger = Ledger(config.experiment.paths.state_directory)
    summary = Orchestrator(config, adapter=ExplodingAdapter(), ledger=ledger).run(workbooks=[path])

    assert summary.final_state is State.FAULT
    assert summary.reason_code == API_ERROR
    # The identity was claimed before the failed write, so a replay cannot
    # repeat the stimulation.
    assert ledger.consumed_folder_paths(config.experiment.experiment.experiment_id)


# --- hardware failure modes ------------------------------------------------


def test_api_error_enters_fault(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    class FailingAdapter(SimulatedMaxOneAdapter):
        def send_stimulation(self, protocol):
            raise AdapterError(API_ERROR, "maxlab raised")

    summary = Orchestrator(config, adapter=FailingAdapter()).run(workbooks=[path])
    assert summary.final_state is State.FAULT
    assert summary.reason_code == API_ERROR


def test_ambiguous_receipt_faults_and_is_never_retried(cycle_config, tmp_path):
    config = cycle_config()
    paths = [
        workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4)),
        workbook_for(tmp_path, "metrics_data_20260101_000100.xlsx", SyntheticRecording("P2", "000002", 0.4)),
    ]

    class AmbiguousAdapter(SimulatedMaxOneAdapter):
        def send_stimulation(self, protocol):
            super().send_stimulation(protocol)
            return StimReceipt(
                status="AMBIGUOUS",
                protocol_id=protocol.protocol_id,
                amplitude_mV=protocol.amplitude_mV,
            )

    adapter = AmbiguousAdapter()
    summary = Orchestrator(config, adapter=adapter).run(workbooks=paths)

    assert summary.final_state is State.FAULT
    assert summary.reason_code == AMBIGUOUS_STIM_RECEIPT
    assert adapter.stimulation_count() == 1  # not retried, and the run stopped


def test_recording_starts_before_any_stimulation(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    adapter = SimulatedMaxOneAdapter()
    Orchestrator(config, adapter=adapter).run(workbooks=[path])

    names = adapter.call_names
    assert names.index("start_recording") < names.index("send_stimulation")


def test_safe_shutdown_runs_and_restores_neutral_output(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    adapter = SimulatedMaxOneAdapter()
    Orchestrator(config, adapter=adapter).run(workbooks=[path])

    assert adapter.call_names[-1] == "safe_shutdown"
    assert adapter.dac_neutral is True
    assert adapter.recording is False


def test_safe_shutdown_runs_on_the_exception_path(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    class FailingAdapter(SimulatedMaxOneAdapter):
        def send_stimulation(self, protocol):
            raise AdapterError(API_ERROR, "boom")

    adapter = FailingAdapter()
    Orchestrator(config, adapter=adapter).run(workbooks=[path])

    assert adapter.call_names[-1] == "safe_shutdown"
    assert adapter.dac_neutral is True


# --- ledger integration ----------------------------------------------------


def test_run_writes_a_full_provenance_trail(cycle_config, tmp_path):
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 1.5))

    ledger = Ledger(config.experiment.paths.state_directory)
    Orchestrator(config, adapter=SimulatedMaxOneAdapter(), ledger=ledger).run(workbooks=[path])

    rows = ledger.transitions(config.experiment.experiment.experiment_id)
    states = [r["next_state"] for r in rows]

    assert states[:3] == ["PREFLIGHT", "BASELINE_RECORDING", "WAITING_FOR_METRICS"]
    assert states[-1] == "COMPLETE"

    analyzed = next(r for r in rows if r["next_state"] == "ANALYZING")
    assert analyzed["workbook_sha256"]
    assert analyzed["folder_path"]
    assert analyzed["metrics_json"]
    assert analyzed["qc_json"]

    decided = next(r for r in rows if r["next_state"] == "DECIDING")
    assert decided["reason_code"] == THRESHOLD_REACHED


def test_dry_run_emits_receipts_without_hardware_writes(runnable_config, tmp_path):
    config = runnable_config(
        **{
            "experiment.experiment.mode": "dry_run",
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
        }
    )
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    orchestrator = Orchestrator(config)
    orchestrator.run(workbooks=[path])

    receipts = orchestrator.adapter.receipts
    assert receipts
    assert all(entry["hardware_write"] is False for entry in receipts)
    assert any(entry["action"] == "send_stimulation" for entry in receipts)

    log = Path(config.experiment.paths.log_directory) / "dry_run_receipts.jsonl"
    assert log.is_file() and log.read_text().strip()


def test_dry_run_does_not_consume_the_cumulative_limit(runnable_config, tmp_path):
    config = runnable_config(
        **{
            "experiment.experiment.mode": "dry_run",
            "metrics_schema.recording_selection.strategy": "folder_path_prefix",
            "metrics_schema.recording_selection.folder_path": "/home/mxwbio/Data/Synthetic/",
        }
    )
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    ledger = Ledger(config.experiment.paths.state_directory)
    Orchestrator(config, ledger=ledger).run(workbooks=[path])

    assert ledger.cumulative_stimulations(config.experiment.experiment.experiment_id) == 0


def test_cleanup_failure_does_not_mask_the_real_fault(cycle_config, tmp_path):
    """A raising stop_recording must be recorded, not replace the outcome."""
    config = cycle_config()
    path = workbook_for(tmp_path, "metrics_data_20260101_000000.xlsx", SyntheticRecording("P1", "000001", 0.4))

    class BadCleanupAdapter(SimulatedMaxOneAdapter):
        def send_stimulation(self, protocol):
            raise AdapterError(API_ERROR, "device vanished")

        def stop_recording(self):
            raise RuntimeError("stop_recording also failed")

    summary = Orchestrator(config, adapter=BadCleanupAdapter()).run(workbooks=[path])

    assert summary.final_state is State.FAULT
    assert summary.reason_code == API_ERROR  # the original fault, not the cleanup one
    assert any("stop_recording" in error for error in summary.cleanup_errors)


def test_safe_shutdown_still_runs_when_stop_recording_raises(cycle_config, tmp_path):
    config = cycle_config()

    class BadCleanupAdapter(SimulatedMaxOneAdapter):
        def stop_recording(self):
            raise RuntimeError("nope")

    adapter = BadCleanupAdapter()
    Orchestrator(config, adapter=adapter).run(workbooks=[])

    assert adapter.call_names[-1] == "safe_shutdown"
    assert adapter.dac_neutral is True
