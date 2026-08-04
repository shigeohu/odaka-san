# MAXONE Metrics-Mediated Adaptive Stimulation Project

## 1. Project purpose

Develop a deterministic Python application that controls a MaxWell Biosystems MaxOne experiment through the vendor-supplied MaxLab Live API.

The intended cycle is:

1. Start with a baseline cycle at 0 V.
2. Wait for MaxLab Live to export a completed metrics workbook to the configured directory.
3. Select the recording that belongs to the active cycle, validate it, and read the mean firing frequency.
4. Compare the result with the configured reference threshold.
5. If the mean firing frequency is below the threshold, increase stimulation amplitude by exactly one configured step.
6. Record again and repeat.
7. If the threshold is reached or exceeded, stop normally.
8. Stop or fault when any safety, data-quality, timing, or cumulative limit is reached.

The required response time is minutes. Implement a metrics-file-mediated stepwise loop. Do not introduce the C++ real-time closed-loop interface unless the requirement changes.

## 2. Input file format

The analysis input is the MaxLab Live **metrics export workbook**, not a CSV.

- Format: Excel `.xlsx`
- Filename pattern: `metrics_data_<YYYYMMDD>_<HHMMSS>.xlsx`
- The filename timestamp is the **export time**, not a recording time. It is later than every recording in the file and must never be used as a recording timestamp.
- Reference file in this repository: `metrics_data_20260409_150425.xlsx`

### 2.1 Sheets

| Sheet | Granularity | Instances present |
| --- | --- | --- |
| `Meta Data` | one row per analysis instance | all |
| `Analysis Parameters` | one row per analysis instance | all |
| `Activity - Well Level` | one row per recording | Activity Analysis only |
| `Network - Well Level` | one row per recording | ISI-N Burst Detector only |
| `Network - Burst Level` | one row per detected burst | ISI-N Burst Detector only |

### 2.2 The `Instance` key

`Instance` is the join key across all sheets.

One recording produces **two** analysis instances: an `Activity Analysis` instance and an `ISI-N Burst Detector` instance. Both appear in `Meta Data` and `Analysis Parameters` with identical recording metadata.

In the reference file the Activity instances happen to be odd and the burst instances even. **This parity is an observed coincidence, not a documented guarantee. Never select an instance by parity.** Resolve the analysis type by joining `Instance` to `Analysis Parameters.Analysis Type`.

Pair the two instances of one recording using the recording identity in `Meta Data` (see §2.3).

### 2.3 Recording identity

A recording is identified by `Meta Data.Folder Path`, which is unique per run, for example:

```text
/home/mxwbio/Data/Odaka/CO_260206_260318/260409/P005163/Network/000030
```

`Wellplate ID` + `Assay Run ID` + `Well Number` is the equivalent structured key. `Assay Run ID` alone is not guaranteed unique across wellplates and must not be used by itself.

### 2.4 A workbook contains multiple recordings

**One exported workbook may contain many unrelated recordings.** The reference file contains seven different chips (`P005163`, `P005157`, `P005124`, `P005232`, `P005211`, `P005169`, `P005190`), each recorded for ~300 s. It is a batch export, not a time series of one chip.

Selecting the wrong row silently applies another chip's firing rate to this experiment's stimulation decision. Recording selection is therefore a safety gate, not a convenience — see §9.3.

### 2.5 Format quirks that must be handled explicitly

These are observed in the reference file and must be covered by fixture tests:

1. **`"N/A"` is the missing marker.** It is the literal string `N/A`, not an empty cell. It appears inside otherwise-numeric columns, so every metric column is mixed-type on read. Never coerce it to `0`, `NaN`, or an empty string without an explicit, tested rule.
2. **Dates and times are strings**, not Excel date serials: `'2026-04-09'`, `'13:56:25'`, `'14:01:25'`.
3. **`Network - Burst Level` column A has no header.** It is a `0..N-1` integer index left over from a DataFrame write. Reading by column position shifts every subsequent column by one. Read by header name.
4. **`Network - Burst Level` splits metadata across rows.** Within each instance block, `Well Group Name`, `Well Group Color`, `Control`, and `Assay Tag` are populated only on the block's **second** row; every other row carries `"N/A"` for them. `Date`, `Wellplate ID`, `Well Number`, and `Assay Run ID` are populated on every row **except** that second row. Do not read recording metadata from this sheet — read it from `Meta Data` by `Instance`.
5. **A recording with zero bursts still emits rows.** `P005163` has `Burst Frequency = 0` with every other burst metric `"N/A"`, and two placeholder rows in `Network - Burst Level` despite having no bursts. Row count in that sheet is not a burst count.
6. **Recording duration is not exactly 300 s.** `Duration per Configuration [s]` ranges from `300.018` to `300.084` across the reference file. Always read the per-recording value; never hard-code 300.

## 3. How the metrics workbook is produced

**The workbook is not written automatically when a recording finishes.** A
recording produces only the raw `.h5`. The workbook exists only after an
operator runs an analysis on that recording and then exports it. Every cycle of
the adaptive loop therefore contains a manual step, and the loop's
`WAITING_FOR_METRICS` state is waiting on a person, not on the instrument.

Per cycle, in MaxLab Live (Manual v25.1 §8 *Analysis Workflow*):

1. Record the assay. This writes the raw `.h5` under the assay's folder.
2. Open the **Analysis** tab and select that assay recording in the assay list.
3. Select the analysis module. **The decision metric comes from `Activity Analysis`.** A Network Assay defaults to `Network Analysis`, so Activity Analysis must be chosen explicitly — the manual is clear that "an Activity Analysis can also be performed on a Network Assay". Running only Network Analysis produces no `Activity - Well Level` sheet and the cycle cannot proceed.
4. Confirm the analysis parameters match `expected_analysis_parameters` (`0.1 Hz` / `20 µV` / `200 ms`). A mismatch is a QC failure (§9.4), not a warning.
5. Click **Start Analysis** and wait for the trial's status icon to read *Completed*.
6. Export:
   - **Export Metrics** on the single trial, or
   - toggle **Select for joint export** on each trial and click **Export Selected**.
7. In the export dialog choose the save path, tick the `.xls` format, and choose either metric set (see §3.2).

### 3.1 Where the file lands

"The exported metrics will be saved in a folder labeled with the date and time
at which the export was carried out." The workbook therefore appears **inside a
new timestamped subdirectory** of the chosen save path, not directly in it.
`metrics_watcher.recursive` must stay `true`, and
`paths.metrics_watch_directory` must be the *parent* the operator exports into.

Exporting `.xls` with all metrics from a highly active culture "may take
several minutes", so the stable-file window is doing real work here.

### 3.2 Which sheets exist depends on the export choice

| Export choice | Sheets produced |
| --- | --- |
| Export summary metrics | well-level sheets only |
| Export all metrics | plus electrode/spike-level and burst-level sheets |

Only `Meta Data`, `Analysis Parameters`, and `Activity - Well Level` are on the
decision path, so those are the only **required** sheets. The `Network - *`
sheets are optional: they are absent from a summary-metrics export, and absent
entirely when no Network Analysis trial was included. Requiring them would
reject a perfectly good export. An *unknown* sheet is still a fault — that means
the export format changed.

### 3.3 Consequences the software must respect

- **One recording can carry several Activity Analysis trials.** `Instance` is assigned per (Well Plate ID + Well Number + Assay Run ID + **Analysis Trial**), so re-running Activity Analysis with different parameters as a *new trial* yields two Activity instances for one `Folder Path`. That is `ACTIVITY_INSTANCE_AMBIGUOUS` and must fault (§9.3 step 5) — the software cannot know which trial the operator meant.
- **Re-running the same trial overwrites its results.** "Rerun Analysis … current results will be overwritten." A re-export after a re-run therefore carries the same `Folder Path` with different numbers. The recording identity is already consumed, so the cycle is rejected — correct, and the operator must start a new cycle rather than re-analyse an old recording.
- **A joint export mixes recordings.** This is how the reference file came to hold seven chips. It is normal operation, not misuse, which is why recording selection is a safety gate (§2.4).
- **Editing Well Editor fields requires re-running the analysis** for the change to reach the export.
- Do not rename `.h5` files, and do not put spaces in folder names — MaxLab Live may fail to resolve the path or export.

## 4. Role of Claude Code

Claude Code is used to design, implement, inspect, and test the automation software.

Claude Code must not be part of the live experimental decision loop. During an experiment, a deterministic Python program must read the metrics workbook and apply a version-controlled decision rule. Never send workbook contents to an LLM and use free-form model output to choose a stimulation parameter.

## 5. Sources of truth

Read these files before implementing or changing behavior:

- `config/experiment.yaml`: experiment mode, paths, timing, stop conditions, and adaptive policy
- `config/metrics_schema.yaml`: workbook interpretation, recording selection, and the metric to read
- `config/stimulation_protocols.yaml`: waveform, electrodes, amplitude step, and hard limits
- `metrics_data_20260409_150425.xlsx`: the reference metrics export
- **MaxLab Live Manual v25.1** (`mxw-maxlab-live-manual-v25.1.pdf`): the vendor GUI manual. It defines the exported metrics (manual §9.1.2, §9.2.3), the export procedure (§8.1, §8.3), the `Instance` key and Meta Data fields (§8.3.4), and the stimulation parameter ranges (§4 Sidebar, §7.5). Manual section numbers are cited as `manual §x`; bare `§x` always means a section of this file. It contains **no** Python API documentation.
- local MaxLab examples under the installed MaxLab directory
- the vendor API documentation matching the installed MaxLab/API versions
- tests and recorded fixtures

Do not infer undocumented API behavior, workbook semantics, biological thresholds, electrode IDs, or safe stimulation limits.

### 4.1 Definitions confirmed by the vendor manual

Quote the manual, do not re-derive these.

- **`Active Area [%]`** — "Percentage of active electrodes with respect to the total number of recorded electrodes." An electrode counts as active when its firing rate exceeds the `Firing Rate Threshold [Hz]` **and** its 90th-percentile spike amplitude exceeds the `Amplitude Threshold [µV]`; both are the Activity Analysis parameters recorded in the workbook (`0.1 Hz` / `20 µV` in the reference export).
- **The denominator is per recording**: the number of electrodes routed for that recording, **at most 1020** in a single configuration. It is *not* exported in the metrics workbook, so it must be configured from the assay that produced the recording. (`1024` appears in the manual only as the 10-bit ADC resolution behind `LSB [µV]`; it is not an electrode count.)
- **Firing Rate summary metrics** — "Only active electrodes are considered." The mean is over active electrodes, never over all recorded electrodes.
- **`Instance`** — "For each unique combination (Well Plate ID + Well Number + Assay Run ID + Analysis Trial), a unique Instance index is assigned." Parity is not part of the definition.
- **`Number of Configurations`** — "Number of configurations in a single recording file (for example, 7 configurations for a 7X Sparse ActivityScan Assay)." A Network assay is `1`. When it is greater than `1`, `Duration per Configuration [s]` is per configuration and not the total recording time.
- **`Well Number`** — "always 1 for the MaxOne System."
- **`DIV [days]`** — automatically calculated from the plating date entered in the Well Editor. `"N/A"` means no plating date was entered; it is not an error.
- **Stimulation** — 32 stimulation channels may be connected simultaneously; pulses are voltage, biphasic, **positive phase first**. See §5.3.

### 4.2 Still unresolved

Do not guess a value or a definition for any of them.

- **The vendor Python API.** The manual documents the GUI only. The `maxlab` module, `mxwserver`, DAC codes, and the volts-to-bits calibration remain undocumented here. This is what blocks `adapters/real.py`.
- **Charge injection capacity and the safe-amplitude rationale.** The manual defers to MaxWell's separate *Electrical Stimulation Guide* (`mxw.bio/MxW_Doc_Electrical_Stimulation_Guide`), which is behind an authenticated file share and has not been obtained.
- **`Spikes per Burst per Electrode`.** The manual says "normalized by the total number of recorded electrodes", but in the reference export `Spikes per Burst` divided by it yields ~166.8 (`P005157`), ~247.8 (`P005169`), and ~190.2 (`P005190`) — not the ~1020 that `Active Area [%]` implies for the same recordings. The stated definition does not reproduce the data. This field is not on the decision path; do not use it.
- Whether an adaptive run produces one new workbook per cycle, or appends recordings to an existing workbook. Both must be tolerated; see §9.

### 4.3 Documented stimulation limits

From the manual. These are device and vendor-recommendation limits; the experiment's own limits are separate and must still be set explicitly.

| Parameter | Range | Default |
| --- | --- | --- |
| Stimulation Amplitude | `0`–`1000` mV (assay setup: `3`–`1000`) | `0` (Sidebar), `200` (assay) |
| Stimulation Pulse Phase | `100`–`1000` µs, step `50` | `200` |
| Number of Pulses per Burst | `0`–`10000` | `1` |
| Interpulse Interval | `0`–`10000` ms | `300` |
| Number of Bursts | `0`–`99` | `1` |
| Interburst Interval | `0.01`–`600` s | `1.00` |
| Simultaneous stimulation channels | `32` | — |

> "For MaxOne+ Chips with PEDOT electrodes, the recommended maximum Stimulation Amplitude is **600 mV**. Exceeding this value may compromise electrode integrity and experiment reproducibility."

Treat 600 mV as a hard ceiling unless the Electrical Stimulation Guide is obtained and an operator explicitly authorises more. The configured `maximum_absolute_amplitude_mV` must never exceed it.

## 6. Non-negotiable safety rules

- Default to `dry_run`; never default to live hardware operation.
- Never execute an armed experiment from Claude Code.
- Never execute live stimulation merely to test code.
- Never use or recommend `--dangerously-skip-permissions` for this repository.
- Fail closed: an absent, null, malformed, stale, duplicated, ambiguous, or out-of-range value must prevent stimulation.
- Ambiguous recording selection must prevent stimulation. Zero matches and more than one match are both faults.
- Never automatically retry a stimulation when delivery status is uncertain.
- Never continue after a MaxLab API error without entering `FAULT`.
- Never overwrite raw metrics workbooks or MaxLab recording files.
- Never process one `(workbook hash, recording identity)` pair more than once.
- Only one process may own the device-control lock.
- Live operation requires an explicit environment gate, an armed configuration, complete safety settings, a hardware lock, and a successful preflight.
- The 0 V baseline is a recording-only cycle. Do not send a stimulation waveform for the baseline merely to represent 0 V.
- A hardware adapter must return DAC outputs to the documented neutral state during normal completion and exception cleanup.
- Do not convert volts to DAC bits until the installed hardware/API calibration has been verified.

## 7. Operating modes

### `simulate`

Use synthetic metrics workbooks. Do not import or connect to the vendor API.

### `dry_run`

Read real metrics workbooks and calculate the next decision. Replace every hardware write with a structured dry-run receipt.

### `armed`

Permit hardware calls only when all conditions below are true:

- `MAXONE_HARDWARE_ENABLED=1`
- `mode: armed`
- all fields listed under `required_before_armed` are non-null
- the configuration passes schema validation
- the MaxLab/API versions are supported
- `mxwserver` is available
- the exclusive device lock is acquired
- electrode routing and stimulation-unit assignment are verified
- the requested amplitude and waveform pass all safety gates

Never add a bypass, force, unsafe, debug-live, or auto-arm mode.

## 8. Adaptive policy

Implement exactly this first policy:

```text
cycle 0:
    record baseline at 0 V without sending a stimulation sequence

for each completed cycle:
    accept the workbook
    select exactly one recording for this cycle
    validate the recording
    read mean_firing_frequency_hz

    if selection, validation, or QC fails:
        FAULT

    if mean_firing_frequency_hz >= reference_threshold_hz:
        COMPLETE with reason THRESHOLD_REACHED

    else:
        candidate_amplitude =
            previous_amplitude_mV + amplitude_step_mV

        if candidate_amplitude > maximum_amplitude_mV:
            COMPLETE or FAULT according to configured max-amplitude behavior
        else:
            wait for configured cooldown
            stimulate once using the allowlisted protocol at candidate_amplitude
            record the next cycle
```

Requirements:

- Use decimal-safe or explicitly rounded amplitude arithmetic.
- Generate a stable `reason_code` for every branch.
- The same workbook hash, recording identity, and configuration version must produce the same decision.
- Do not ask an LLM to interpret borderline values.
- Threshold comparison is inclusive: reaching the threshold means normal completion.
- A null threshold, step, maximum amplitude, or firing-frequency result must block armed operation.
- Do not increase by more than one step per accepted recording.
- Do not decrease amplitude or introduce adaptive pulse-shape changes unless the policy is explicitly revised and tested.

## 9. Metrics watcher, selection, and validation

### 8.1 Watcher

The watcher must:

- monitor only `paths.metrics_watch_directory`
- accept only the configured filename glob (`metrics_data_*.xlsx`)
- ignore hidden, temporary, lock, and partial files, including Excel `~$` lock files
- require stable file size and modification time for the configured interval
- calculate SHA-256 of the whole workbook before parsing
- use a timeout and enter `FAULT` or `COMPLETE` according to configuration
- move or copy accepted and rejected files only according to the retention configuration

Because a workbook can be re-exported with additional recordings appended, the workbook hash alone is not a sufficient idempotency key. Deduplicate on `(workbook_sha256, folder_path)`, and additionally reject any `folder_path` already consumed by a previous cycle of this experiment. A previously seen workbook hash is not by itself an error; a previously consumed recording identity always is.

Do not reject a workbook merely because its filename timestamp precedes the cycle start — that timestamp is the export time. Apply staleness checks to `Meta Data.Start Time` and `Stop Time` of the selected recording instead.

### 8.2 Reader

The reader must:

- follow `config/metrics_schema.yaml`
- open the workbook read-only and never write back to it
- require every configured sheet to be present, and fail closed on an unexpected sheet set
- resolve columns by header name, never by position
- treat the configured missing markers, including `"N/A"`, as missing rather than as values
- reject numeric coercion failures instead of silently dropping rows
- preserve unknown columns
- reject ambiguous column mappings
- produce a typed validation report
- quarantine invalid input without modifying the original

### 8.3 Recording selection

Selection runs before any metric is read.

1. Build the recording table from `Meta Data`, keyed by `Folder Path`.
2. Join `Instance` to `Analysis Parameters` and resolve each instance's `Analysis Type`.
3. Apply the configured selection rule from `config/metrics_schema.yaml`.
4. Require **exactly one** surviving recording.
   - zero matches -> `FAULT` with `RECORDING_NOT_FOUND`
   - more than one match -> `FAULT` with `RECORDING_AMBIGUOUS`
5. Require that recording to have exactly one `Activity Analysis` instance. Zero or several -> `FAULT`.
6. Require the recording identity to be absent from the processed-recording ledger.

Never fall back to "the only row", "the first row", or "the most recent row" when the configured rule fails to match. Silence is a fault, not a default.

### 8.4 Quality control

Reject the recording, and therefore block stimulation, when any of these hold:

- the resolved `Mean Firing Rate [Hz]` is missing, `"N/A"`, non-numeric, negative, or non-finite
- `Active Area [%]` is missing or below `minimum_active_area_percent`
- the number of contributing electrodes is below `minimum_active_electrodes`, derived as `Active Area [%] / 100 * routed_electrode_count` (§5.1: the routed count is not in the workbook and must be configured)
- `Duration per Configuration [s]` is missing, or outside the configured expected range
- `Number of Configurations` is not `1`
- `Sampling Frequency [Hz]`, `Gain`, or `LSB [µV]` differ from the configured expected values
- the recording `Start Time`/`Stop Time` are inconsistent with the active cycle window
- `Analysis Parameters` for the selected instance differ from the configured expected analysis parameters

The single-electrode case is why §9.4 exists. In the reference file `P005211` reports `Mean Firing Rate 0.19 Hz` with `Active Area 0.10 %` and `"N/A"` for standard deviation, CV, and every percentile — the mean is over exactly one electrode. That number is a valid float and would pass a naive numeric check while being meaningless as a well-level rate. A missing dispersion statistic alongside a present mean is a positive signal that the electrode count is one; treat it as such.

Do not finalize the reader until representative workbooks have been inspected.

## 10. Mean firing-frequency extraction

The metric is already aggregated to the well level by MaxLab Live. There is no aggregation to perform and no spike-event counting to implement.

Read exactly one scalar:

- sheet `Activity - Well Level`
- column `Mean Firing Rate [Hz]`
- row whose `Instance` is the selected recording's `Activity Analysis` instance

That value is `mean_firing_frequency_hz`, in Hz.

`Mean Firing Rate [Hz]` is the mean over *active* electrodes only — the manual is explicit: "Only active electrodes are considered." An electrode is active when its firing rate exceeds `Firing Rate Threshold [Hz]` and its 90th-percentile spike amplitude exceeds `Amplitude Threshold [µV]` (`0.1 Hz` / `20 µV` in the reference file). It is **not** a mean over all recorded electrodes. The denominator therefore changes with culture activity, which is why `Active Area [%]` must be read and QC-checked alongside it (§9.4).

Do not substitute `Median Firing Rate [Hz]`, any percentile column, or any `Network - Well Level` metric unless the policy is explicitly revised and tested. Do not compute a firing rate from `Network - Burst Level`.

The analysis output record must include:

- `mean_firing_frequency_hz`
- the source sheet, column, and `Instance`
- recording identity (`Folder Path`, `Wellplate ID`, `Assay Run ID`, `Well Number`)
- `Active Area [%]` and the derived electrode count, with the derivation marked as unverified
- `Duration per Configuration [s]` as reported
- recording `Start Time` and `Stop Time`
- the applied Activity Analysis parameters
- QC flags
- source workbook SHA-256 and filename
- schema version
- analysis version

## 11. State machine

Use a persisted state machine:

```text
IDLE
-> PREFLIGHT
-> BASELINE_RECORDING
-> WAITING_FOR_METRICS
-> SELECTING_RECORDING
-> VALIDATING
-> ANALYZING
-> DECIDING
-> SAFETY_CHECK
-> COOLDOWN
-> STIMULATING
-> RECORDING
-> WAITING_FOR_METRICS
-> COMPLETE

Any unrecoverable error -> FAULT
Operator stop -> STOPPING -> COMPLETE
Restart during an active experiment -> RECOVERY_REQUIRED
```

Persist every transition atomically with:

- UTC timestamp
- experiment ID
- cycle ID
- previous and next state
- workbook path and SHA-256, when applicable
- selected recording identity, when applicable
- applied amplitude
- protocol ID
- metrics
- QC flags
- decision and reason code

Never automatically leave `FAULT` or `RECOVERY_REQUIRED`.

## 12. Hardware abstraction

All vendor calls must be behind an interface such as:

```python
class MaxOneAdapter(Protocol):
    def preflight(self) -> PreflightResult: ...
    def initialize(self) -> None: ...
    def configure_array(self, protocol: StimProtocol) -> None: ...
    def start_recording(self, context: CycleContext) -> None: ...
    def send_stimulation(self, protocol: StimProtocol) -> StimReceipt: ...
    def stop_recording(self) -> None: ...
    def safe_shutdown(self) -> None: ...
```

Provide:

- `SimulatedMaxOneAdapter`
- `DryRunMaxOneAdapter`
- `RealMaxOneAdapter`

Real adapter requirements:

- import the vendor-supplied `maxlab` module only inside the real adapter
- verify `mxwserver`
- verify installed MaxLab/API versions
- use local vendor examples as the implementation reference
- verify electrode routing and unique stimulation-unit assignment
- download the routed array before stimulation
- run required offset compensation
- begin recording before transmitting a nonzero stimulation sequence
- ensure the DAC/output returns to the documented neutral state
- stop recording and clean up in `finally`
- do not automatically retry ambiguous writes

## 13. Configuration validation

Implement typed configuration validation with Pydantic or an equivalent library.

Armed mode must be rejected when any of these are null or invalid:

- metrics watch directory
- recording selection rule
- metric sheet and column mapping
- reference firing-frequency threshold
- minimum active area and minimum active electrodes
- amplitude step
- maximum amplitude
- stimulation electrodes
- waveform and pulse timing
- maximum cycles
- cooldown
- maximum total experiment duration
- cumulative stimulation limit
- MaxLab/API version constraints
- emergency-stop behavior

Keep machine-specific paths and electrode IDs in a local, gitignored override where practical.

## 14. Idempotency and provenance

Use a transactional SQLite ledger or equivalent.

Record:

- configuration and protocol hashes
- Git commit
- software versions
- MaxLab/API versions
- state transitions
- workbook path, size, timestamps, and SHA-256
- selected recording identity and analysis instance
- validation report
- metrics and QC
- decision and reason code
- requested and applied amplitude
- stimulation receipt
- exceptions and cleanup outcome

Enforce uniqueness on the recording identity, not only on the workbook hash. Use an OS-level exclusive lock. Duplicate filesystem events must never cause duplicate stimulation.

## 15. Tests

Before any hardware review, run:

```bash
python -m pytest
python -m maxone_loop.cli validate-config --config config/experiment.yaml
python -m maxone_loop.cli inspect --config config/experiment.yaml <workbook.xlsx>
python -m maxone_loop.cli replay --config config/experiment.yaml --input tests/fixtures
python -m maxone_loop.cli run --config config/experiment.yaml --mode dry_run
```

With the tracked configuration, `replay` and `run` exit non-zero on purpose:
`recording_selection.strategy` and `paths.metrics_watch_directory` are still
null, and refusing to guess is the designed behaviour. They exit zero once
those values are set. `validate-config` and `inspect` work as-is.

Test at minimum:

Policy:

- 0 V baseline performs no hardware stimulation
- rate below threshold increments exactly one step
- rate equal to threshold completes
- rate above threshold completes
- candidate amplitude equal to maximum is allowed
- candidate amplitude above maximum is blocked
- null threshold, step, or maximum blocks armed mode

Workbook format:

- the reference file `metrics_data_20260409_150425.xlsx` parses and yields the expected seven recordings
- `"N/A"` in a numeric column is treated as missing, never as `0`
- the unnamed index column in `Network - Burst Level` does not shift the column mapping
- recording metadata is read from `Meta Data`, and the split-metadata rows in `Network - Burst Level` are never used as a metadata source
- a zero-burst recording (`P005163`) does not crash and is not counted as having bursts
- a missing sheet, a renamed sheet, a renamed column, or a changed unit suffix is rejected
- date and time strings are parsed as strings, not as Excel serials
- a per-recording duration other than exactly 300 s is accepted; a duration outside the configured range is rejected

Recording selection:

- a workbook holding seven recordings selects exactly one
- a selection rule matching zero recordings faults with `RECORDING_NOT_FOUND`
- a selection rule matching two recordings faults with `RECORDING_AMBIGUOUS`
- an already-consumed recording identity is rejected even in a new workbook
- a re-exported workbook containing both an old and a new recording consumes only the new one
- an identical workbook delivered twice performs no second stimulation

Quality control:

- the single-electrode recording (`P005211`, `Active Area 0.10 %`, `"N/A"` dispersion) is rejected, not used as a rate
- `Number of Configurations` other than `1` is rejected
- a mismatched sampling frequency, gain, or LSB is rejected
- mismatched Activity Analysis parameters are rejected
- NaN and infinity are rejected

Runtime:

- duplicate filesystem events and duplicate hashes
- partial, truncated, and non-Excel files in the watch directory
- Excel `~$` lock files are ignored
- restart from every active state
- API failure before, during, and after stimulation
- ambiguous stimulation receipt
- safe shutdown and neutral-output restoration

Unit and CI tests must mock the vendor API. Hardware-in-the-loop tests must be separate, explicit, and disabled by default.

## 16. Repository layout

```text
maxone_loop/
  config/models.py      typed configuration; unknown keys are errors
  config/loader.py      three-file load, *.local.yaml overrides, hashes
  metrics/workbook.py   read-only workbook reader; "N/A", headers, quirks
  metrics/selection.py  exactly-one recording selection
  metrics/extract.py    the one scalar, plus its provenance record
  metrics/qc.py         quality gates, including the single-electrode case
  policy.py             pure decision function
  state.py              state machine and restart resolution
  ledger.py             SQLite provenance, recording idempotency, device lock
  watcher.py            stable-file detection
  adapters/             simulated / dry_run / real (real is a fail-closed stub)
  orchestrator.py       cycle sequencing
  synthetic.py          synthetic workbooks for simulate mode and tests
  cli.py                validate-config, inspect, replay, run
```

`adapters/real.py` is deliberately unimplemented. It requires the installed
MaxLab Live distribution and its API documentation; every method raises rather
than guessing at vendor behaviour. `safe_shutdown` is the exception -- it must
never raise, because it runs during cleanup.

The provenance and idempotency boundary is the **recording identity**, not the
workbook hash: `consumed_recordings` has a primary key on
`(experiment_id, folder_path)`, and the orchestrator claims the identity
*before* stimulating. A crash between the claim and the write costs a cycle; it
never repeats one.

## 17. Claude Code workflow

For each task:

1. Read this file and all configuration files.
2. Inspect representative metrics workbooks before changing reader behavior.
3. State any unresolved assumptions.
4. Make the smallest safe change.
5. Implement pure analysis and decision logic before hardware integration.
6. Add or update tests first.
7. Run focused tests, then the full suite.
8. Show exact diffs affecting hardware writes, configuration validation, and safety gates.
9. Never run armed mode.
10. Never weaken safety checks merely to satisfy tests.
11. Update configuration examples and setup documentation when interfaces change.

If a required value is unknown, retain `null`, produce a clear validation error, and keep the project in `dry_run`.

## 18. Initial implementation order

1. Inspect representative metrics workbooks.
2. Finalize `config/metrics_schema.yaml`.
3. Implement configuration models and validation.
4. Implement stable-file detection and the processed-recording ledger.
5. Implement the workbook reader and recording selection.
6. Implement quality control.
7. Implement the adaptive decision policy.
8. Implement simulation and replay.
9. Implement dry-run orchestration.
10. Implement and review the real MaxOne adapter.
11. Perform operator-supervised hardware-in-the-loop validation outside Claude Code.
