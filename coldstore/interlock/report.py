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
    """Levelled latches, shutdown runs and pending reset approvals."""

    thresholds: dict[str, float]
    latched: dict[str, bool]
    runs: list[dict[str, object]]
    approvals: dict[str, list[dict[str, str]]]
    ledger: int

    def as_dict(self) -> dict[str, object]:
        return {
            "thresholds": self.thresholds,
            "latched": self.latched,
            "runs": self.runs,
            "approvals": self.approvals,
            "ledger": self.ledger,
        }
