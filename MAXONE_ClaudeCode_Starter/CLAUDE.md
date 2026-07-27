# MAXONE CSV-Mediated Adaptive Stimulation Project

## 1. Project purpose

Develop a deterministic Python application that controls a MaxWell Biosystems MaxOne experiment through the vendor-supplied MaxLab Live API.

The intended cycle is:

1. Start with a baseline cycle at 0 V.
2. Wait for MaxLab Live to export a completed CSV file to the configured directory.
3. Validate the CSV and calculate the mean firing frequency.
4. Compare the result with the configured reference threshold.
5. If the mean firing frequency is below the threshold, increase stimulation amplitude by exactly one configured step.
6. Record again and repeat.
7. If the threshold is reached or exceeded, stop normally.
8. Stop or fault when any safety, data-quality, timing, or cumulative limit is reached.

The required response time is minutes. Implement a CSV-mediated stepwise loop. Do not introduce the C++ real-time closed-loop interface unless the requirement changes.

## 2. Role of Claude Code

Claude Code is used to design, implement, inspect, and test the automation software.

Claude Code must not be part of the live experimental decision loop. During an experiment, a deterministic Python program must parse the CSV and apply a version-controlled decision rule. Never send CSV contents to an LLM and use free-form model output to choose a stimulation parameter.

## 3. Sources of truth

Read these files before implementing or changing behavior:

- `config/experiment.yaml`: experiment mode, paths, timing, stop conditions, and adaptive policy
- `config/csv_schema.yaml`: CSV interpretation and mean firing-frequency calculation
- `config/stimulation_protocols.yaml`: waveform, electrodes, amplitude step, and hard limits
- local MaxLab examples under the installed MaxLab directory
- the vendor API documentation matching the installed MaxLab/API versions
- tests and recorded fixtures

Do not infer undocumented API behavior, CSV semantics, biological thresholds, electrode IDs, or safe stimulation limits.

## 4. Non-negotiable safety rules

- Default to `dry_run`; never default to live hardware operation.
- Never execute an armed experiment from Claude Code.
- Never execute live stimulation merely to test code.
- Never use or recommend `--dangerously-skip-permissions` for this repository.
- Fail closed: an absent, null, malformed, stale, duplicated, ambiguous, or out-of-range value must prevent stimulation.
- Never automatically retry a stimulation when delivery status is uncertain.
- Never continue after a MaxLab API error without entering `FAULT`.
- Never overwrite raw CSV or MaxLab recording files.
- Never process one CSV hash more than once.
- Only one process may own the device-control lock.
- Live operation requires an explicit environment gate, an armed configuration, complete safety settings, a hardware lock, and a successful preflight.
- The 0 V baseline is a recording-only cycle. Do not send a stimulation waveform for the baseline merely to represent 0 V.
- A hardware adapter must return DAC outputs to the documented neutral state during normal completion and exception cleanup.
- Do not convert volts to DAC bits until the installed hardware/API calibration has been verified.

## 5. Operating modes

### `simulate`

Use synthetic CSV data. Do not import or connect to the vendor API.

### `dry_run`

Read real CSV files and calculate the next decision. Replace every hardware write with a structured dry-run receipt.

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

## 6. Adaptive policy

Implement exactly this first policy:

```text
cycle 0:
    record baseline at 0 V without sending a stimulation sequence

for each completed cycle:
    validate CSV
    calculate mean_firing_frequency_hz

    if validation or QC fails:
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
- The same CSV hash and configuration version must produce the same decision.
- Do not ask an LLM to interpret borderline values.
- Threshold comparison is inclusive: reaching the threshold means normal completion.
- A null threshold, step, maximum amplitude, or firing-frequency result must block armed operation.
- Do not increase by more than one step per accepted CSV.
- Do not decrease amplitude or introduce adaptive pulse-shape changes unless the policy is explicitly revised and tested.

## 7. State machine

Use a persisted state machine:

```text
IDLE
-> PREFLIGHT
-> BASELINE_RECORDING
-> WAITING_FOR_CSV
-> VALIDATING
-> ANALYZING
-> DECIDING
-> SAFETY_CHECK
-> COOLDOWN
-> STIMULATING
-> RECORDING
-> WAITING_FOR_CSV
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
- CSV path and SHA-256, when applicable
- applied amplitude
- protocol ID
- metrics
- QC flags
- decision and reason code

Never automatically leave `FAULT` or `RECOVERY_REQUIRED`.

## 8. CSV watcher and validation

The watcher must:

- monitor only `paths.csv_watch_directory`
- accept only the configured filename glob
- ignore hidden, temporary, lock, and partial files
- require stable file size and modification time for the configured interval
- reject files older than the active cycle start unless replay mode is explicit
- calculate SHA-256 before parsing
- reject hashes already recorded in the ledger
- use a timeout and enter `FAULT` or `COMPLETE` according to configuration
- move or copy accepted and rejected files only according to the retention configuration

The parser must:

- follow `config/csv_schema.yaml`
- validate encoding, delimiter, headers, units, data types, missingness, numeric ranges, and time ordering
- preserve unknown columns
- reject ambiguous column mappings
- never guess whether rows represent spikes, bins, channels, or summary statistics
- produce a typed validation report
- quarantine invalid input without modifying the original

Do not finalize the parser until representative CSV files have been inspected.

## 9. Mean firing-frequency calculation

Support only the calculation mode explicitly selected in `config/csv_schema.yaml`.

Initially support these two modes:

1. `precomputed_rate`
   - CSV already contains a firing-frequency column in Hz.
   - Calculate the configured aggregation across valid rows/electrodes.

2. `spike_events`
   - Each accepted row represents a spike event.
   - Calculate event count divided by configured recording duration and configured electrode denominator.

Do not silently switch between modes. Reject an incompatible CSV.

The output must include:

- `mean_firing_frequency_hz`
- number of valid rows
- number of included electrodes/channels
- recording duration used
- excluded rows and reasons
- QC flags
- source CSV hash
- schema version
- analysis version

## 10. Hardware abstraction

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

## 11. Configuration validation

Implement typed configuration validation with Pydantic or an equivalent library.

Armed mode must be rejected when any of these are null or invalid:

- CSV watch directory
- CSV calculation mode and required column mappings
- reference firing-frequency threshold
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

## 12. Idempotency and provenance

Use a transactional SQLite ledger or equivalent.

Record:

- configuration and protocol hashes
- Git commit
- software versions
- MaxLab/API versions
- state transitions
- CSV path, size, timestamps, and SHA-256
- validation report
- metrics and QC
- decision and reason code
- requested and applied amplitude
- stimulation receipt
- exceptions and cleanup outcome

Use an OS-level exclusive lock. Duplicate filesystem events must never cause duplicate stimulation.

## 13. Tests

Before any hardware review, run:

```bash
python -m pytest
python -m maxone_loop.cli validate-config --config config/experiment.yaml
python -m maxone_loop.cli replay --config config/experiment.yaml --input tests/fixtures
python -m maxone_loop.cli run --config config/experiment.yaml --mode dry_run
```

Test at minimum:

- 0 V baseline performs no hardware stimulation
- rate below threshold increments exactly one step
- rate equal to threshold completes
- rate above threshold completes
- candidate amplitude equal to maximum is allowed
- candidate amplitude above maximum is blocked
- null threshold, step, or maximum blocks armed mode
- malformed, empty, partial, stale, duplicated, or non-monotonic CSV
- changed column names or units
- NaN and infinity
- duplicate filesystem events and duplicate hashes
- restart from every active state
- API failure before, during, and after stimulation
- ambiguous stimulation receipt
- safe shutdown and neutral-output restoration

Unit and CI tests must mock the vendor API. Hardware-in-the-loop tests must be separate, explicit, and disabled by default.

## 14. Claude Code workflow

For each task:

1. Read this file and all configuration files.
2. Inspect representative CSV fixtures before changing parser behavior.
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

## 15. Initial implementation order

1. Inspect sample CSV files.
2. Finalize `csv_schema.yaml`.
3. Implement configuration models and validation.
4. Implement stable-file detection and the processed-hash ledger.
5. Implement pure mean-firing-frequency calculation.
6. Implement the adaptive decision policy.
7. Implement simulation and replay.
8. Implement dry-run orchestration.
9. Implement and review the real MaxOne adapter.
10. Perform operator-supervised hardware-in-the-loop validation outside Claude Code.
