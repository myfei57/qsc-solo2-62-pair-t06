"""Graded safety interlock with staged shutdown and authorized reset.

This sits beside the legacy single-bit :class:`Interlock`.  The differences:

* a leak is assigned one of three :class:`~coldstore.interlock.levels.Grade`
  levels, and the latch jumps directly to that level (it never climbs);
* the shutdown runs as an ordered sequence that only advances after each rung
  is confirmed in position;
* a reset is gated by the per-grade :data:`RESET_POLICY` (single confirmation or
  two-person review) and requires the shutdown sequence to be complete and the
  leak to be declared clear;
* every trip, step, request, approval and reset is sealed in the hash-chained
  ledger, mirrored to the runtime audit stream.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from coldstore.audit import Auditor
from coldstore.store.store import Store

from .ledger import Ledger, LedgerRecord
from .levels import RESET_POLICY, Grade, grade_by_name, reset_scope
from .report import GradedState
from .sequence import ShutdownSequence

ACTIVE_KEY = "graded:active"
LEVEL_KEY = "graded:level"
REASON_KEY = "graded:reason"
TRIP_ID_KEY = "graded:trip_id"

# Reset request lifecycle events.
KIND_TRIP = "trip"
KIND_STEP = "shutdown_step"
KIND_RESET_REQUEST = "reset_request"
KIND_APPROVAL = "reset_approval"
KIND_REJECTION = "reset_rejected"
KIND_RESET = "reset"

OPEN = "open"
APPROVED = "approved"
REJECTED = "rejected"
EXECUTED = "executed"


@dataclass(frozen=True)
class ResetRequestView:
    """One reset request with the approvals collected so far."""

    id: str
    grade: str
    requester: str
    at: str
    status: str
    required_approvals: int
    approvals: list[dict[str, str]]
    rejectors: list[str]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "grade": self.grade,
            "requester": self.requester,
            "at": self.at,
            "status": self.status,
            "required_approvals": self.required_approvals,
            "approvals": self.approvals,
            "rejectors": self.rejectors,
        }


class GradedInterlock:
    """Coordinates level latch, shutdown sequence, reset workflow and ledger."""

    def __init__(self, store: Store, auditor: Auditor | None = None) -> None:
        self._store = store
        self.ledger = Ledger(store, auditor)
        self.sequence = ShutdownSequence(store)

    # ------------------------------------------------------------------ level

    def active_level(self) -> Grade | None:
        raw, present = self._store.get(LEVEL_KEY)
        if not present:
            return None
        return grade_by_name(raw)

    def is_active(self) -> bool:
        return self._store.get(ACTIVE_KEY)[1]

    def reason(self) -> str:
        value, present = self._store.get(REASON_KEY)
        return value if present else ""

    def trip(self, grade: Grade, reason: str, actor: str = "system") -> dict[str, object]:
        """Jump straight to ``grade`` (or higher); never climb grade by grade."""

        current = self.active_level()
        if current is not None and current.rank >= grade.rank:
            # Already responding at or above this severity: no re-trip.
            return {
                "tripped": True,
                "level": current.value,
                "raised": False,
                "reason": self.reason(),
            }

        record = self.ledger.append(
            KIND_TRIP,
            {"level": grade.value, "reason": reason, "from": "" if current is None else current.value},
            actor=actor,
        )
        self._store.put(ACTIVE_KEY, "true")
        self._store.put(LEVEL_KEY, grade.value)
        self._store.put(REASON_KEY, reason)
        self._store.put(TRIP_ID_KEY, record.id)
        self.sequence.start(grade)
        return {"tripped": True, "level": grade.value, "raised": True, "reason": reason}

    def trip_from_reading(
        self, ppm: float, threshold: float, actor: str = "monitor"
    ) -> dict[str, object]:
        """Classify a calibrated reading and trip the matching level directly."""

        from .levels import grade_for_ppm

        grade = grade_for_ppm(ppm, threshold)
        if grade is None:
            return {"tripped": self.is_active(), "level": self.level_text(), "raised": False}
        return self.trip(grade, f"ammonia {ppm:.2f} ppm >= level band", actor=actor)

    def level_text(self) -> str:
        level = self.active_level()
        return "" if level is None else level.value

    def advance_sequence(self, actor: str = "operator") -> dict[str, object]:
        """Confirm the current shutdown rung in position and move on."""

        if not self.is_active():
            raise ValueError("interlock is not active")
        before = self.sequence.projection()
        if before.complete:
            raise ValueError("shutdown sequence already complete")
        step = before.steps[before.cursor]
        projection = self.sequence.advance()
        self.ledger.append(
            KIND_STEP,
            {
                "level": self.level_text(),
                "index": step.index,
                "key": step.key,
                "status": "in_position",
                "complete": projection.complete,
            },
            actor=actor,
        )
        return projection.as_dict()

    # ----------------------------------------------------------- reset flow

    def request_reset(
        self, requester: str, role: str, clear_declared: bool = False
    ) -> ResetRequestView:
        """File a reset request under the caller's grade policy."""

        level = self._require_active()
        policy = RESET_POLICY[level]
        if role != policy.request_role:
            raise ValueError(
                f"role {role!r} may not request an {level.value} reset "
                f"(requires {policy.request_role!r})"
            )
        if not clear_declared:
            raise ValueError("reset request must declare the leak clear")
        self._require_sequence_complete(level)

        request_id = uuid.uuid4().hex
        payload: dict[str, object] = {
            "request_id": request_id,
            "level": level.value,
            "mode": policy.mode,
            "required_approvals": len(policy.approver_groups),
        }
        self.ledger.append(KIND_RESET_REQUEST, payload, actor=requester)

        if policy.mode == "single":
            return self._execute_reset(request_id, requester)
        return self.request_view(request_id)

    def approve_reset(
        self,
        request_id: str,
        approver: str,
        role: str,
        clear_attested: bool = False,
    ) -> ResetRequestView:
        """Add one independent approval; the last approval performs the reset."""

        level = self.active_level()
        request = self.request_view(request_id)
        if request.status != OPEN:
            raise ValueError(f"request {request_id} is {request.status}, not open")
        if level is None or request.grade != level.value:
            raise ValueError("requested level is no longer the active level")

        policy = RESET_POLICY[grade_by_name(request.grade)]
        if request.requester == approver:
            raise ValueError("approver must be a different person than the requester")
        if approver in request.rejectors or approver in {
            item["by"] for item in request.approvals
        }:
            raise ValueError("this approver has already acted on the request")

        approval_index = len(request.approvals)
        if approval_index >= len(policy.approver_groups):
            raise ValueError("request already holds every required approval")
        allowed_roles = policy.approver_groups[approval_index]
        if role not in allowed_roles:
            required = " or ".join(sorted(allowed_roles))
            raise ValueError(
                f"approval {approval_index + 1} of {len(policy.approver_groups)} "
                f"requires {required}"
            )
        if not clear_attested:
            raise ValueError("approver must attest that the leak is clear")
        self._require_sequence_complete(grade_by_name(request.grade))

        self.ledger.append(
            KIND_APPROVAL,
            {
                "request_id": request_id,
                "level": request.grade,
                "approval_index": approval_index,
                "role": role,
            },
            actor=approver,
        )

        view = self.request_view(request_id)
        if view.status == APPROVED:
            return self._execute_reset(request_id, approver)
        return view

    def reject_reset(self, request_id: str, rejector: str, role: str) -> ResetRequestView:
        """Refuse a request; only a role in the grade's approver scope may."""

        request = self.request_view(request_id)
        if request.status != OPEN:
            raise ValueError(f"request {request_id} is {request.status}, not open")
        level = grade_by_name(request.grade)
        policy = RESET_POLICY[level]
        allowed = set().union(*policy.approver_groups) if policy.approver_groups else set()
        if policy.mode == "single":
            allowed = {policy.request_role}
        if rejector == request.requester:
            raise ValueError("the requester cannot reject their own request")
        if role not in allowed:
            raise ValueError(f"role {role!r} may not reject an {level.value} reset request")
        self.ledger.append(
            KIND_REJECTION, {"request_id": request_id, "level": request.grade}, actor=rejector
        )
        return self.request_view(request_id)

    def _execute_reset(self, request_id: str, actor: str) -> ResetRequestView:
        level = self._require_active()
        self._require_sequence_complete(level)
        self.ledger.append(KIND_RESET, {"request_id": request_id, "level": level.value}, actor=actor)
        self._store.delete(ACTIVE_KEY)
        self._store.delete(LEVEL_KEY)
        self._store.delete(REASON_KEY)
        self._store.delete(TRIP_ID_KEY)
        self.sequence.clear()
        return self.request_view(request_id)

    # ------------------------------------------------------------- queries

    def request_view(self, request_id: str) -> ResetRequestView:
        """Reconstruct one request's status by replaying the ledger."""

        found: ResetRequestView | None = None
        for view in self.requests():
            if view.id == request_id:
                found = view
        if found is None:
            raise ValueError(f"unknown reset request {request_id}")
        return found

    def requests(self, grade: Grade | None = None) -> list[ResetRequestView]:
        """Every reset request known to the ledger, in filing order."""

        views: dict[str, ResetRequestView] = {}
        order: list[str] = []
        for record in self.ledger.records():
            request_id = str(record.payload.get("request_id", ""))
            if not request_id:
                continue
            kind = record.kind
            if kind == KIND_RESET_REQUEST:
                level_name = str(record.payload.get("level", ""))
                if grade is not None and level_name != grade.value:
                    continue
                required = int(record.payload.get("required_approvals", 0))
                views[request_id] = ResetRequestView(
                    id=request_id,
                    grade=level_name,
                    requester=record.actor,
                    at=record.at,
                    status=OPEN,
                    required_approvals=required,
                    approvals=[],
                    rejectors=[],
                )
                order.append(request_id)
                continue

            if request_id not in views:
                continue
            view = views[request_id]
            if kind == KIND_APPROVAL:
                approvals = list(view.approvals) + [{"by": record.actor, "at": record.at}]
                status = APPROVED if len(approvals) >= view.required_approvals else OPEN
                views[request_id] = ResetRequestView(
                    id=view.id,
                    grade=view.grade,
                    requester=view.requester,
                    at=view.at,
                    status=status,
                    required_approvals=view.required_approvals,
                    approvals=approvals,
                    rejectors=view.rejectors,
                )
            elif kind == KIND_REJECTION:
                views[request_id] = ResetRequestView(
                    id=view.id,
                    grade=view.grade,
                    requester=view.requester,
                    at=view.at,
                    status=REJECTED,
                    required_approvals=view.required_approvals,
                    approvals=view.approvals,
                    rejectors=view.rejectors + [record.actor],
                )
            elif kind == KIND_RESET:
                views[request_id] = ResetRequestView(
                    id=view.id,
                    grade=view.grade,
                    requester=view.requester,
                    at=view.at,
                    status=EXECUTED,
                    required_approvals=view.required_approvals,
                    approvals=view.approvals,
                    rejectors=view.rejectors,
                )
        return [views[item] for item in order]

    def open_requests(self) -> list[ResetRequestView]:
        return [view for view in self.requests() if view.status == OPEN]

    def resettable_for(self, role: str) -> dict[str, object]:
        """The levels a role may confirm/approve, with their policy detail."""

        scope = reset_scope(role)
        return {
            "role": role,
            "levels": [grade.value for grade in scope],
            "policy": [
                {
                    "level": grade.value,
                    "mode": RESET_POLICY[grade].mode,
                    "request_role": RESET_POLICY[grade].request_role,
                    "required_approvals": len(RESET_POLICY[grade].approver_groups),
                    "note": RESET_POLICY[grade].note,
                }
                for grade in scope
            ],
        }

    def ledger_records(self) -> list[dict[str, object]]:
        return [record.as_dict() for record in self.ledger.records()]

    def state(self) -> GradedState:
        return GradedState(
            active=self.is_active(),
            level=self.level_text(),
            reason=self.reason(),
            sequence=self.sequence.projection().as_dict(),
            ledger=self.ledger.verify().as_dict(),
        )

    def describe(self) -> str:
        if not self.is_active():
            return f"graded interlock idle ledger={self.ledger.count()}"
        projection = self.sequence.projection()
        return (
            f"graded interlock level={self.level_text()} "
            f"step={projection.cursor}/{len(projection.steps)} "
            f"complete={projection.complete}"
        )

    # ------------------------------------------------------------- helpers

    def _require_active(self) -> Grade:
        level = self.active_level()
        if level is None or not self.is_active():
            raise ValueError("interlock is not active")
        return level

    def _require_sequence_complete(self, level: Grade) -> None:
        projection = self.sequence.projection()
        if not projection.complete or projection.grade != level.value:
            raise ValueError(
                f"{level.value} shutdown sequence must complete and be in position before reset"
            )
