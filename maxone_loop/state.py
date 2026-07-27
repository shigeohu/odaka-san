"""The persisted state machine (CLAUDE.md section 10)."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    PREFLIGHT = "PREFLIGHT"
    BASELINE_RECORDING = "BASELINE_RECORDING"
    WAITING_FOR_METRICS = "WAITING_FOR_METRICS"
    SELECTING_RECORDING = "SELECTING_RECORDING"
    VALIDATING = "VALIDATING"
    ANALYZING = "ANALYZING"
    DECIDING = "DECIDING"
    SAFETY_CHECK = "SAFETY_CHECK"
    COOLDOWN = "COOLDOWN"
    STIMULATING = "STIMULATING"
    RECORDING = "RECORDING"
    COMPLETE = "COMPLETE"
    STOPPING = "STOPPING"
    FAULT = "FAULT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


#: States from which the loop never resumes on its own.
TERMINAL = frozenset({State.COMPLETE, State.FAULT, State.RECOVERY_REQUIRED})

#: Restarting while one of these is the persisted state means a cycle was in
#: flight. The operator must resolve it; the loop must not guess.
ACTIVE = frozenset(
    {
        State.PREFLIGHT,
        State.BASELINE_RECORDING,
        State.WAITING_FOR_METRICS,
        State.SELECTING_RECORDING,
        State.VALIDATING,
        State.ANALYZING,
        State.DECIDING,
        State.SAFETY_CHECK,
        State.COOLDOWN,
        State.STIMULATING,
        State.RECORDING,
        State.STOPPING,
    }
)

_ALLOWED: dict[State, frozenset[State]] = {
    State.IDLE: frozenset({State.PREFLIGHT}),
    State.PREFLIGHT: frozenset({State.BASELINE_RECORDING}),
    State.BASELINE_RECORDING: frozenset({State.WAITING_FOR_METRICS}),
    State.WAITING_FOR_METRICS: frozenset({State.SELECTING_RECORDING, State.COMPLETE}),
    State.SELECTING_RECORDING: frozenset({State.VALIDATING}),
    State.VALIDATING: frozenset({State.ANALYZING}),
    State.ANALYZING: frozenset({State.DECIDING}),
    State.DECIDING: frozenset({State.SAFETY_CHECK, State.COMPLETE}),
    State.SAFETY_CHECK: frozenset({State.COOLDOWN, State.COMPLETE}),
    State.COOLDOWN: frozenset({State.STIMULATING}),
    State.STIMULATING: frozenset({State.RECORDING}),
    State.RECORDING: frozenset({State.WAITING_FOR_METRICS}),
    State.COMPLETE: frozenset(),
    State.STOPPING: frozenset({State.COMPLETE}),
    State.FAULT: frozenset(),
    State.RECOVERY_REQUIRED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def can_transition(current: State, nxt: State) -> bool:
    """FAULT and STOPPING are reachable from any non-terminal state."""
    if current in TERMINAL:
        return False
    if nxt in (State.FAULT, State.STOPPING):
        return True
    return nxt in _ALLOWED[current]


def check_transition(current: State, nxt: State) -> None:
    if not can_transition(current, nxt):
        raise InvalidTransition(f"{current.value} -> {nxt.value} is not a permitted transition")


def resume_state(persisted: State) -> State:
    """What a restart resolves to.

    Never automatically leaves FAULT or RECOVERY_REQUIRED, and never silently
    picks up an in-flight cycle.
    """
    if persisted in TERMINAL:
        return persisted
    if persisted is State.IDLE:
        return State.IDLE
    return State.RECOVERY_REQUIRED
