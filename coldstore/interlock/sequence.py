"""Ordered shutdown sequences that advance one rung at a time.

A grade maps to a fixed ordered list of actions.  The sequence starts at the
first action and only moves forward when the current action has been
acknowledged as in position.  Skipping a rung is rejected, so the line is
brought down in a safe order rather than every device dropping at once.

Sequence state is plain runtime state; the *decision* to start or advance it
is sealed in the ledger by the graded interlock.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from coldstore.store.store import Store

from .levels import Grade, grade_by_name

SEQUENCE_KEY = "graded:sequence"

PENDING = "pending"
IN_POSITION = "in_position"
ABANDONED = "abandoned"


@dataclass(frozen=True)
class Step:
    """One action rung inside a grade's shutdown sequence."""

    key: str
    note: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "note": self.note}


def _steps(actions: tuple[tuple[str, str], ...]) -> tuple[Step, ...]:
    return tuple(Step(key=key, note=note) for key, note in actions)


# Ordered from first to last.  Higher grades extend, rather than reorder, the
# lower grade response.
SEQUENCES: dict[Grade, tuple[Step, ...]] = {
    Grade.L1: _steps(
        (
            ("alarm", "raise local ammonia alarm"),
            ("ventilation", "start area ventilation"),
            ("isolate_compressor", "isolate and stop the affected compressor"),
        )
    ),
    Grade.L2: _steps(
        (
            ("alarm", "raise area alarm"),
            ("ventilation", "start emergency ventilation"),
            ("stop_compressors", "stop affected zone compressors"),
            ("close_suction", "close affected zone suction valves"),
            ("standby_pump", "start standby emergency pump"),
            ("isolate_zone", "isolate the affected machine-room zone"),
        )
    ),
    Grade.L3: _steps(
        (
            ("alarm", "raise plant-wide alarm and evacuation"),
            ("ventilation", "run all emergency ventilation"),
            ("stop_compressors", "stop every refrigeration compressor"),
            ("close_suction", "close all suction valves"),
            ("close_discharge", "close all discharge valves"),
            ("stop_pumps", "stop refrigerant liquid pumps"),
            ("isolate_liquid", "isolate the liquid receivers"),
            ("esd", "drive the emergency shutdown valve to fail-safe"),
        )
    ),
}


@dataclass(frozen=True)
class StepState:
    """A sequence step plus its execution status."""

    index: int
    key: str
    note: str
    status: str

    def as_dict(self) -> dict[str, object]:
        return {"index": self.index, "key": self.key, "note": self.note, "status": self.status}


@dataclass(frozen=True)
class SequenceState:
    """Projection of the running (or last run) shutdown sequence."""

    active: bool
    grade: str
    cursor: int
    steps: list[StepState]
    complete: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "grade": self.grade,
            "cursor": self.cursor,
            "complete": self.complete,
            "steps": [step.as_dict() for step in self.steps],
        }


class ShutdownSequence:
    """Persists the ordered sequence and its one moving cursor."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def steps_for(self, grade: Grade) -> tuple[Step, ...]:
        return SEQUENCES[grade]

    def start(self, grade: Grade) -> SequenceState:
        steps = SEQUENCES[grade]
        state = {
            "active": True,
            "grade": grade.value,
            "cursor": 0,
            "statuses": [PENDING for _ in steps],
        }
        self._store.put(SEQUENCE_KEY, json.dumps(state, ensure_ascii=False))
        return self.projection()

    def advance(self) -> SequenceState:
        """Mark the current rung in position, then move the cursor on."""

        state = self._load()
        if not state:
            raise ValueError("no active shutdown sequence")
        if not state["active"]:
            raise ValueError("shutdown sequence is not active")
        cursor = state["cursor"]
        statuses = state["statuses"]
        if cursor >= len(statuses):
            raise ValueError("shutdown sequence already complete")
        if statuses[cursor] != PENDING:
            raise ValueError(f"step {cursor} is already {statuses[cursor]}")
        statuses[cursor] = IN_POSITION
        cursor += 1
        state["cursor"] = cursor
        state["statuses"] = statuses
        if cursor >= len(statuses):
            state["active"] = False
        self._store.put(SEQUENCE_KEY, json.dumps(state, ensure_ascii=False))
        return self.projection()

    def clear(self) -> None:
        """Drop the sequence state once a reset has been authorized."""

        self._store.delete(SEQUENCE_KEY)

    def projection(self) -> SequenceState:
        state = self._load()
        if not state:
            return SequenceState(active=False, grade="", cursor=0, steps=[], complete=False)
        grade = grade_by_name(str(state["grade"]))
        steps = SEQUENCES[grade]
        statuses = [str(value) for value in state["statuses"]]
        step_states = [
            StepState(index=index, key=step.key, note=step.note, status=statuses[index])
            for index, step in enumerate(steps)
        ]
        cursor = int(state["cursor"])
        complete = cursor >= len(steps) and all(
            status == IN_POSITION for status in statuses
        )
        return SequenceState(
            active=bool(state["active"]),
            grade=grade.value,
            cursor=cursor,
            steps=step_states,
            complete=complete,
        )

    def _load(self) -> dict[str, object] | None:
        raw, present = self._store.get(SEQUENCE_KEY)
        if not present:
            return None
        try:
            state = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(state, dict):
            return None
        statuses = state.get("statuses")
        if not isinstance(statuses, list):
            return None
        state["statuses"] = [str(value) for value in statuses]
        return state
