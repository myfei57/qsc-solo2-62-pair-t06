"""Interlock projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InterlockState:
    """Latch state together with the recorded trip log."""

    tripped: bool
    reason: str
    trips: list[dict[str, str]]
    count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "tripped": self.tripped,
            "reason": self.reason,
            "trips": self.trips,
            "count": self.count,
        }


@dataclass(frozen=True)
class GradedState:
    """Graded interlock state: active level, sequence and chain check."""

    active: bool
    level: str
    reason: str
    sequence: dict[str, object]
    ledger: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "active": self.active,
            "level": self.level,
            "reason": self.reason,
            "sequence": self.sequence,
            "ledger": self.ledger,
        }
