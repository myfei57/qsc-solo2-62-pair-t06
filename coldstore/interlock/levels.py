"""Graded leak levels, role ranks and per-level reset policy.

The two "step by step" ideas are deliberately split:

* Classification **jumps straight** to the level a reading actually warrants.
  A small L1 leak and a large L3 leak look different, so an L3 reading trips
  L3 directly instead of crawling L1 -> L2 -> L3 while ammonia escapes.
* The shutdown *sequence* is the only thing that advances one rung at a time,
  and only after the previous rung has been confirmed in position.

While latched a level can only move up; coming back down requires an
authorized reset (see :data:`RESET_POLICY`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Grade(str, Enum):
    """One graded leak response level."""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"

    @property
    def rank(self) -> int:
        return int(self.value[1:])

    def __str__(self) -> str:
        return self.value


GRADES: tuple[Grade, ...] = (Grade.L1, Grade.L2, Grade.L3)


def grade_by_name(name: str) -> Grade:
    """Look up a grade from its ``L1``/``L2``/``L3`` text form."""

    try:
        return Grade(name.upper())
    except ValueError as exc:
        raise ValueError(f"unknown grade {name!r}, expected L1, L2 or L3") from exc


# Console roles, ordered from least to most authority.
OPERATOR = "operator"
SHIFT_SUPERVISOR = "shift_supervisor"
SAFETY_OFFICER = "safety_officer"
INSTALL_MANAGER = "install_manager"

ROLES: tuple[str, ...] = (
    OPERATOR,
    SHIFT_SUPERVISOR,
    SAFETY_OFFICER,
    INSTALL_MANAGER,
)

ROLE_RANK: dict[str, int] = {role: index for index, role in enumerate(ROLES)}


# Multiples of the calibrated ammonia trip threshold.  ``1.0`` means "at the
# configured trip threshold", so retuning the single threshold retunes every
# grade without touching this table.
L1_FACTOR = 1.0
L2_FACTOR = 2.0
L3_FACTOR = 4.0


@dataclass(frozen=True)
class GradeBand:
    """Human-readable definition of one level as a multiple of the threshold."""

    grade: Grade
    factor: float
    label: str
    response: str


GRADE_BANDS: tuple[GradeBand, ...] = (
    GradeBand(Grade.L1, L1_FACTOR, "minor", "local alarm, ventilation, affected compressor"),
    GradeBand(Grade.L2, L2_FACTOR, "serious", "zone isolation and emergency pump, ordered zone stop"),
    GradeBand(Grade.L3, L3_FACTOR, "major", "plant-wide ordered emergency shutdown and ESD valve"),
)


def grade_for_ppm(
    ppm: float,
    threshold: float,
    l1_factor: float = L1_FACTOR,
    l2_factor: float = L2_FACTOR,
    l3_factor: float = L3_FACTOR,
) -> Grade | None:
    """Classify a calibrated reading directly, returning ``None`` below L1."""

    if threshold <= 0:
        raise ValueError("trip threshold must be positive")
    if not l1_factor <= l2_factor <= l3_factor:
        raise ValueError("level factors must be ordered L1 <= L2 <= L3")
    if ppm >= threshold * l3_factor:
        return Grade.L3
    if ppm >= threshold * l2_factor:
        return Grade.L2
    if ppm >= threshold * l1_factor:
        return Grade.L1
    return None


@dataclass(frozen=True)
class ResetPolicy:
    """Who may ask for a reset and who must countersign it for one grade.

    ``mode`` is ``"single"`` (one authorized confirmation) or ``"dual"``
    (requester plus one or more independent approvers).  ``approver_groups``
    lists, in order, the role each required approval must carry; an approver
    may never be the requester and one person can only satisfy one group.
    """

    grade: Grade
    mode: str
    request_role: str
    approver_groups: tuple[frozenset[str], ...]
    note: str


RESET_POLICY: dict[Grade, ResetPolicy] = {
    Grade.L1: ResetPolicy(
        grade=Grade.L1,
        mode="single",
        request_role=SHIFT_SUPERVISOR,
        approver_groups=(),
        note="shift supervisor confirms; no second signature",
    ),
    Grade.L2: ResetPolicy(
        grade=Grade.L2,
        mode="dual",
        request_role=OPERATOR,
        approver_groups=(frozenset({SAFETY_OFFICER}),),
        note="operator requests, safety officer is the independent second person",
    ),
    Grade.L3: ResetPolicy(
        grade=Grade.L3,
        mode="dual",
        request_role=SHIFT_SUPERVISOR,
        approver_groups=(
            frozenset({SAFETY_OFFICER}),
            frozenset({INSTALL_MANAGER}),
        ),
        note="shift supervisor requests; safety officer and install manager countersign",
    ),
}


def role_has_rank(role: str, required: str) -> bool:
    """Whether ``role`` is at least as authoritative as ``required``."""

    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role!r}")
    return ROLE_RANK[role] >= ROLE_RANK[required]


def reset_scope(role: str) -> list[Grade]:
    """Grades that a role is allowed to confirm or approve a reset for.

    Operators may raise alarms and file requests but never authorize a reset.
    """

    if role not in ROLE_RANK:
        raise ValueError(f"unknown role {role!r}")
    scope: list[Grade] = []
    for grade in GRADES:
        policy = RESET_POLICY[grade]
        if policy.mode == "single":
            if role == policy.request_role:
                scope.append(grade)
        elif any(role in group for group in policy.approver_groups):
            scope.append(grade)
    return scope
