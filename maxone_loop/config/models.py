"""Typed configuration models.

Every model forbids unknown keys. A typo in a YAML file is a configuration
error, not a silently ignored setting -- CLAUDE.md section 5 requires
fail-closed behaviour and a misspelled safety limit that reads as "absent"
is exactly the failure mode being guarded against.

Amplitudes are Decimal, never float. CLAUDE.md section 7 requires
decimal-safe amplitude arithmetic so that repeated single-step increments
do not drift away from the configured maximum.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator


def _to_decimal(value: Any) -> Any:
    """Convert via str so 0.1 stays 0.1 rather than becoming its binary echo."""
    if value is None or isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    return value


Amplitude = Annotated[Decimal, BeforeValidator(_to_decimal)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# experiment.yaml
# ---------------------------------------------------------------------------

Mode = Literal["simulate", "dry_run", "armed"]


class ExperimentSection(Strict):
    experiment_id: str
    mode: Mode
    timezone: str


class PathsSection(Strict):
    metrics_watch_directory: str | None
    accepted_archive_directory: str
    quarantine_directory: str
    state_directory: str
    log_directory: str


class MetricsWatcherSection(Strict):
    filename_glob: str
    poll_interval_seconds: float = Field(gt=0)
    stable_for_seconds: float = Field(ge=0)
    metrics_timeout_minutes: float = Field(gt=0)
    hash_algorithm: Literal["sha256"]
    ignore_filename_prefixes: tuple[str, ...]
    deduplicate_on: Literal["recording_identity"]
    reject_already_consumed_recording: bool
    allow_repeated_workbook_hash: bool
    staleness_source: Literal["recording_start_stop_time"]
    reject_recordings_older_than_cycle_start: bool


class AdaptivePolicySection(Strict):
    baseline_amplitude_mV: Amplitude
    reference_threshold_hz: float | None
    amplitude_step_mV: Amplitude | None
    maximum_amplitude_mV: Amplitude | None
    threshold_comparison: Literal["greater_than_or_equal"]
    action_at_or_above_threshold: Literal["complete"]
    action_when_next_step_exceeds_maximum: Literal["complete", "fault"]
    maximum_cycles: int | None = Field(default=None, ge=1)
    minimum_cooldown_seconds: float = Field(ge=0)
    maximum_total_experiment_minutes: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _baseline_is_zero(self) -> AdaptivePolicySection:
        if self.baseline_amplitude_mV != Decimal("0"):
            raise ValueError(
                "baseline_amplitude_mV must be 0; cycle 0 is a recording-only cycle"
            )
        return self

    @model_validator(mode="after")
    def _step_is_positive(self) -> AdaptivePolicySection:
        if self.amplitude_step_mV is not None and self.amplitude_step_mV <= 0:
            raise ValueError("amplitude_step_mV must be positive; the policy never decreases amplitude")
        return self

    @model_validator(mode="after")
    def _maximum_is_reachable(self) -> AdaptivePolicySection:
        if self.maximum_amplitude_mV is not None and self.maximum_amplitude_mV < 0:
            raise ValueError("maximum_amplitude_mV must not be negative")
        return self

    @model_validator(mode="after")
    def _threshold_is_non_negative(self) -> AdaptivePolicySection:
        if self.reference_threshold_hz is not None and self.reference_threshold_hz < 0:
            raise ValueError("reference_threshold_hz must not be negative")
        return self


class RunQualityControlSection(Strict):
    fail_on_ambiguous_recording_selection: bool
    fail_on_missing_recording: bool
    fail_on_unknown_units: bool
    fail_on_qc_flag: bool


class RuntimeSection(Strict):
    require_hardware_environment_gate: bool
    hardware_environment_variable: str
    hardware_environment_required_value: str
    require_exclusive_device_lock: bool
    resume_after_restart: bool
    structured_log_format: Literal["jsonl"]
    ledger_backend: Literal["sqlite"]


class ExperimentConfig(Strict):
    schema_version: int
    experiment: ExperimentSection
    paths: PathsSection
    metrics_watcher: MetricsWatcherSection
    adaptive_policy: AdaptivePolicySection
    quality_control: RunQualityControlSection
    runtime: RuntimeSection
    required_before_armed: tuple[str, ...]


# ---------------------------------------------------------------------------
# metrics_schema.yaml
# ---------------------------------------------------------------------------


class FileFormatSection(Strict):
    type: Literal["xlsx"]
    filename_glob: str
    header_row: int = Field(ge=0)
    missing_markers: tuple[str, ...]
    numeric_coercion: Literal["reject"]
    date_format: str
    time_format: str
    filename_timestamp_is_export_time: bool

    @model_validator(mode="after")
    def _na_is_a_missing_marker(self) -> FileFormatSection:
        if "N/A" not in self.missing_markers:
            raise ValueError(
                '"N/A" must be a missing marker; it is the marker MaxLab Live '
                "writes inside numeric columns"
            )
        return self


class SheetsSection(Strict):
    meta_data: str
    analysis_parameters: str
    activity_well_level: str
    network_well_level: str
    network_burst_level: str
    require_exact_sheet_set: bool

    def names(self) -> dict[str, str]:
        return {k: v for k, v in self if k != "require_exact_sheet_set"}


class InstanceJoinSection(Strict):
    instance_column: str
    analysis_type_column: str
    activity_analysis_type: str
    burst_analysis_type: str
    allow_parity_based_selection: bool

    @model_validator(mode="after")
    def _parity_stays_disabled(self) -> InstanceJoinSection:
        if self.allow_parity_based_selection:
            raise ValueError(
                "allow_parity_based_selection must stay false; Instance parity is an "
                "observed coincidence in the reference export, not a guarantee"
            )
        return self


class RecordingIdentitySection(Strict):
    primary_key_column: str
    wellplate_id_column: str
    assay_run_id_column: str
    well_number_column: str
    assay_tag_column: str
    allow_assay_run_id_alone: bool

    @model_validator(mode="after")
    def _run_id_alone_stays_disabled(self) -> RecordingIdentitySection:
        if self.allow_assay_run_id_alone:
            raise ValueError(
                "allow_assay_run_id_alone must stay false; Assay Run ID is not unique "
                "across wellplates"
            )
        return self


SelectionStrategy = Literal["folder_path_exact", "folder_path_prefix", "wellplate_and_run_id"]


class RecordingSelectionSection(Strict):
    strategy: SelectionStrategy | None
    folder_path: str | None
    wellplate_id: str | None
    well_number: int | None
    assay_run_id: str | None
    require_exactly_one_match: bool
    on_zero_matches: Literal["fault"]
    on_multiple_matches: Literal["fault"]
    allow_fallback_to_single_row: bool
    allow_fallback_to_latest: bool

    @model_validator(mode="after")
    def _no_fallbacks(self) -> RecordingSelectionSection:
        if self.allow_fallback_to_single_row or self.allow_fallback_to_latest:
            raise ValueError(
                "recording selection fallbacks must stay disabled; a workbook may hold "
                "several unrelated recordings and guessing applies another chip's rate"
            )
        if not self.require_exactly_one_match:
            raise ValueError("require_exactly_one_match must stay true")
        return self

    @model_validator(mode="after")
    def _strategy_has_its_keys(self) -> RecordingSelectionSection:
        if self.strategy in ("folder_path_exact", "folder_path_prefix"):
            if not self.folder_path:
                raise ValueError(f"strategy {self.strategy} requires folder_path")
        elif self.strategy == "wellplate_and_run_id":
            missing = [
                name
                for name, value in (
                    ("wellplate_id", self.wellplate_id),
                    ("well_number", self.well_number),
                    ("assay_run_id", self.assay_run_id),
                )
                if value is None
            ]
            if missing:
                raise ValueError(
                    "strategy wellplate_and_run_id requires " + ", ".join(missing)
                )
        return self


class DecisionMetricSection(Strict):
    name: Literal["mean_firing_frequency_hz"]
    unit: Literal["Hz"]
    sheet: Literal["activity_well_level"]
    column: str
    select_instance_of_analysis_type: Literal["activity_analysis_type"]
    denominator_is_all_recorded_electrodes: bool
    allow_substitute_columns: bool

    @model_validator(mode="after")
    def _no_substitutes(self) -> DecisionMetricSection:
        if self.allow_substitute_columns:
            raise ValueError(
                "allow_substitute_columns must stay false; substituting the median or a "
                "percentile changes the policy and requires an explicit revision"
            )
        if self.denominator_is_all_recorded_electrodes:
            raise ValueError(
                "denominator_is_all_recorded_electrodes must stay false; Mean Firing "
                "Rate averages only electrodes that passed the detection thresholds"
            )
        return self


class ActivityColumnsSection(Strict):
    active_area_percent: str
    firing_rate_std: str
    firing_rate_cv: str
    median_firing_rate: str


class MetaDataColumnsSection(Strict):
    duration_seconds: str
    number_of_configurations: str
    sampling_frequency_hz: str
    gain: str
    lsb_uv: str
    hpf_hz: str
    start_time: str
    stop_time: str
    date: str


class SupportingColumnsSection(Strict):
    activity_well_level: ActivityColumnsSection
    meta_data: MetaDataColumnsSection


class ExpectedAcquisitionSection(Strict):
    sampling_frequency_hz: int
    gain: int
    lsb_uv: float
    hpf_hz: float
    number_of_configurations: int
    duration_seconds_min: float | None = Field(default=None, gt=0)
    duration_seconds_max: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> ExpectedAcquisitionSection:
        lo, hi = self.duration_seconds_min, self.duration_seconds_max
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("duration_seconds_min must not exceed duration_seconds_max")
        return self


class ActivityAnalysisParameters(Strict):
    firing_rate_threshold_hz: float
    amplitude_threshold_uv: float
    isi_threshold_ms: float


class BurstDetectorParameters(Strict):
    n: int


class ExpectedAnalysisParametersSection(Strict):
    activity_analysis: ActivityAnalysisParameters
    isi_n_burst_detector: BurstDetectorParameters
    fail_on_mismatch: bool


class MetricsQualityControlSection(Strict):
    minimum_active_area_percent: float | None = Field(default=None, ge=0)
    minimum_active_electrodes: int | None = Field(default=None, ge=1)
    active_area_denominator_electrodes: int | None = Field(default=None, ge=1)
    fail_when_dispersion_missing: bool
    reject_negative_rate: bool
    reject_non_finite: bool
    fail_on_unknown_units: bool
    fail_on_acquisition_mismatch: bool
    fail_on_analysis_parameter_mismatch: bool
    require_recording_window_within_cycle: bool

    @model_validator(mode="after")
    def _electrode_minimum_needs_a_denominator(self) -> MetricsQualityControlSection:
        if (
            self.minimum_active_electrodes is not None
            and self.active_area_denominator_electrodes is None
        ):
            raise ValueError(
                "minimum_active_electrodes requires active_area_denominator_electrodes; "
                "the electrode count is derived from Active Area [%] and the denominator "
                "is not stated in the workbook"
            )
        return self


class BurstLevelQuirks(Strict):
    has_unnamed_index_column: bool
    metadata_split_across_rows: bool
    use_as_metadata_source: bool
    emits_placeholder_rows_when_no_bursts: bool
    per_electrode_denominator_documented: bool

    @model_validator(mode="after")
    def _not_a_metadata_source(self) -> BurstLevelQuirks:
        if self.use_as_metadata_source:
            raise ValueError(
                "Network - Burst Level must not be used as a metadata source; it "
                "populates group columns only on each instance block's second row"
            )
        return self


class KnownQuirksSection(Strict):
    network_burst_level: BurstLevelQuirks
    read_columns_by_header_name_only: bool

    @model_validator(mode="after")
    def _headers_only(self) -> KnownQuirksSection:
        if not self.read_columns_by_header_name_only:
            raise ValueError(
                "read_columns_by_header_name_only must stay true; Network - Burst Level "
                "carries an unnamed index column that shifts every position"
            )
        return self


class MetricsSchema(Strict):
    schema_version: int
    file_format: FileFormatSection
    sheets: SheetsSection
    instance_join: InstanceJoinSection
    recording_identity: RecordingIdentitySection
    recording_selection: RecordingSelectionSection
    decision_metric: DecisionMetricSection
    supporting_columns: SupportingColumnsSection
    expected_acquisition: ExpectedAcquisitionSection
    expected_analysis_parameters: ExpectedAnalysisParametersSection
    quality_control: MetricsQualityControlSection
    known_quirks: KnownQuirksSection


# ---------------------------------------------------------------------------
# stimulation_protocols.yaml
# ---------------------------------------------------------------------------


class HardwareSection(Strict):
    device_model: str
    maxlab_live_version: str | None
    api_version: str | None
    mxwserver_required: bool
    dac_neutral_code: int
    nominal_mV_per_dac_bit: float
    voltage_amplifier_inverts_polarity: bool
    emergency_stop_procedure: str | None


class WaveformSection(Strict):
    type: Literal["biphasic"]
    charge_balanced: bool
    phase_order: Literal["cathodic_then_anodic", "anodic_then_cathodic"]
    phase_duration_samples: int | None = Field(default=None, gt=0)
    inter_phase_interval_samples: int = Field(ge=0)
    pulses_per_train: int | None = Field(default=None, gt=0)
    inter_pulse_interval_samples: int | None = Field(default=None, ge=0)
    trains_per_cycle: int = Field(ge=1)
    inter_train_interval_seconds: float | None = Field(default=None, ge=0)
    return_to_neutral_after_each_pulse: bool

    @model_validator(mode="after")
    def _charge_balance_required(self) -> WaveformSection:
        if not self.charge_balanced:
            raise ValueError("charge_balanced must stay true for this protocol")
        if not self.return_to_neutral_after_each_pulse:
            raise ValueError("return_to_neutral_after_each_pulse must stay true")
        return self


class HardLimitsSection(Strict):
    maximum_absolute_amplitude_mV: Amplitude | None
    maximum_phase_duration_samples: int | None = Field(default=None, gt=0)
    maximum_pulses_per_train: int | None = Field(default=None, gt=0)
    maximum_trains_per_cycle: int = Field(ge=1)
    minimum_inter_pulse_interval_samples: int | None = Field(default=None, ge=0)
    minimum_inter_train_interval_seconds: float | None = Field(default=None, ge=0)
    maximum_cumulative_stimulations: int | None = Field(default=None, ge=0)
    maximum_cumulative_charge_metric: float | None = Field(default=None, ge=0)


class StimulationSection(Strict):
    protocol_id: str
    mode: Literal["voltage"]
    stimulation_electrodes: tuple[int, ...]
    recording_electrodes: tuple[int, ...]
    amplitude_source: Literal["adaptive_policy"]
    baseline_behavior: Literal["record_without_stimulation"]
    waveform: WaveformSection
    hard_limits: HardLimitsSection

    @model_validator(mode="after")
    def _electrodes_are_unique(self) -> StimulationSection:
        if len(set(self.stimulation_electrodes)) != len(self.stimulation_electrodes):
            raise ValueError("stimulation_electrodes must not repeat an electrode")
        return self


class RecordingSection(Strict):
    record_before_stimulation_seconds: float | None = Field(default=None, ge=0)
    record_after_stimulation_seconds: float | None = Field(default=None, ge=0)
    save_detected_spikes: bool
    save_raw_voltage: bool


class RoutingSection(Strict):
    require_unique_stimulation_unit_per_electrode: bool
    require_array_download: bool
    require_offset_compensation: bool

    @model_validator(mode="after")
    def _routing_checks_stay_on(self) -> RoutingSection:
        for name in (
            "require_unique_stimulation_unit_per_electrode",
            "require_array_download",
            "require_offset_compensation",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must stay true")
        return self


class StimulationProtocols(Strict):
    schema_version: int
    hardware: HardwareSection
    stimulation: StimulationSection
    recording: RecordingSection
    routing: RoutingSection


# ---------------------------------------------------------------------------
# Merged view
# ---------------------------------------------------------------------------

#: Which file owns each top-level key used by ``required_before_armed``.
_NAMESPACE_OWNER = {
    "experiment": "experiment",
    "paths": "experiment",
    "metrics_watcher": "experiment",
    "adaptive_policy": "experiment",
    "quality_control": "experiment",
    "runtime": "experiment",
    "metrics_schema": "metrics_schema",
    "hardware": "stimulation_protocols",
    "stimulation": "stimulation_protocols",
    "recording": "stimulation_protocols",
    "routing": "stimulation_protocols",
}


class AppConfig(Strict):
    """The three configuration files as one validated object."""

    experiment: ExperimentConfig
    metrics_schema: MetricsSchema
    stimulation_protocols: StimulationProtocols

    #: SHA-256 of each source file, for the provenance ledger.
    source_hashes: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", frozen=True)

    def resolve(self, dotted: str) -> Any:
        """Resolve a ``required_before_armed`` path across the three files.

        Raises KeyError when the path does not exist. An unresolvable entry is a
        configuration error, never a silently satisfied requirement.
        """
        head, _, rest = dotted.partition(".")
        owner = _NAMESPACE_OWNER.get(head)
        if owner is None:
            raise KeyError(f"unknown configuration namespace: {head!r} (in {dotted!r})")

        if head == "metrics_schema":
            node: Any = self.metrics_schema
            parts = rest.split(".") if rest else []
        else:
            node = getattr(self, owner)
            parts = dotted.split(".")

        for part in parts:
            if not isinstance(node, BaseModel) or part not in type(node).model_fields:
                raise KeyError(f"unresolvable configuration path: {dotted!r}")
            node = getattr(node, part)
        return node

    def missing_for_armed(self) -> list[str]:
        """Paths in ``required_before_armed`` that are still null or empty."""
        missing: list[str] = []
        for path in self.experiment.required_before_armed:
            value = self.resolve(path)
            if value is None or (isinstance(value, (str, tuple, list, dict)) and len(value) == 0):
                missing.append(path)
        return missing
