"""Graded interlock levels, trip thresholds and the reset approval policy.

Trip levels jump straight to the severity the reading demands: a large leak
must not wait for the smaller responses to run first. Resets walk back one
level at a time, each with its own approval quorum.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("L1", "L2", "L3")

DEFAULT_THRESHOLDS = {"L1": 15.0, "L2": 25.0, "L3": 50.0}

ROLE_RANKS = {"operator": 1, "supervisor": 2, "safety": 3}


@dataclass(frozen=True)
class ResetPolicy:
    """Who may reset a level and how many distinct approvers it takes."""

    approvals: int
    min_role: str
    require_safety: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "approvals": self.approvals,
            "min_role": self.min_role,
            "require_safety": self.require_safety,
        }


RESET_POLICIES = {
    "L1": ResetPolicy(approvals=1, min_role="operator", require_safety=False),
    "L2": ResetPolicy(approvals=1, min_role="supervisor", require_safety=False),
    "L3": ResetPolicy(approvals=2, min_role="supervisor", require_safety=True),
}


def validate_level(level: str) -> str:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    return level


def validate_role(role: str) -> str:
    if role not in ROLE_RANKS:
        raise ValueError(f"role must be one of {', '.join(ROLE_RANKS)}")
    return role


def role_rank(role: str) -> int:
    return ROLE_RANKS.get(role, 0)


def next_level(level: str) -> str:
    """The level one step up, or an empty string at the top."""

    index = LEVELS.index(level)
    return LEVELS[index + 1] if index + 1 < len(LEVELS) else ""


def level_for_ppm(ppm: float, thresholds: dict[str, float]) -> str:
    """The highest level whose threshold the reading crosses, else ''.

    Grading jumps straight to the demanded level instead of climbing one
    rung at a time: the response must match the hazard now, not after the
    smaller sequences have had their turn.
    """

    demanded = ""
    for level in LEVELS:
        if ppm >= thresholds.get(level, 0.0) > 0:
            demanded = level
    return demanded
