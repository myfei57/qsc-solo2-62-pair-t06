"""HTTP handlers for the graded interlock.

Identity is carried by the JSON fields ``actor`` and ``role``; this service is
the in-process control logic and does not itself authenticate.  A deployment
must populate those fields from the real authenticated principal (for example
at an authenticating gateway) instead of trusting the client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from coldstore.interlock import GRADE_BANDS, Grade, grade_by_name, grade_for_ppm

from .http import Request, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _grade_field(request: Request, name: str = "level") -> Grade:
    return grade_by_name(request.str_field(name))


def graded_state(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.graded.state().as_dict())


def graded_levels(server: "Server", request: Request) -> Response:
    threshold = server.runtime.ammonia.threshold()
    return json_response(
        {
            "policy": "classification jumps directly to the measured level",
            "threshold": threshold,
            "bands": [
                {
                    "level": band.grade.value,
                    "factor": band.factor,
                    "ppm_at": threshold * band.factor,
                    "label": band.label,
                    "response": band.response,
                }
                for band in GRADE_BANDS
            ],
        }
    )


def graded_trip(server: "Server", request: Request) -> Response:
    grade = _grade_field(request)
    reason = request.str_field("reason", "manual")
    actor = request.str_field("actor", "operator")
    result = server.runtime.graded.trip(grade, reason, actor=actor)
    return json_response(result)


def graded_trip_reading(server: "Server", request: Request) -> Response:
    ppm = request.float_field("ppm")
    actor = request.str_field("actor", "monitor")
    threshold = server.runtime.ammonia.threshold()
    result = server.runtime.graded.trip_from_reading(ppm, threshold, actor=actor)
    result["ppm"] = ppm
    result["threshold"] = threshold
    result["classified_as"] = (
        None
        if grade_for_ppm(ppm, threshold) is None
        else grade_for_ppm(ppm, threshold).value
    )
    return json_response(result)


def graded_sequence(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.graded.sequence.projection().as_dict())


def graded_sequence_advance(server: "Server", request: Request) -> Response:
    actor = request.str_field("actor", "operator")
    projection = server.runtime.graded.advance_sequence(actor=actor)
    return json_response(projection)


def graded_reset_request(server: "Server", request: Request) -> Response:
    requester = request.str_field("actor")
    role = request.str_field("role")
    clear_declared = bool(request.payload.get("clear_declared", False))
    view = server.runtime.graded.request_reset(requester, role, clear_declared)
    return json_response(view.as_dict())


def graded_reset_approve(server: "Server", request: Request) -> Response:
    request_id = request.str_field("request_id")
    approver = request.str_field("actor")
    role = request.str_field("role")
    clear_attested = bool(request.payload.get("clear_attested", False))
    view = server.runtime.graded.approve_reset(request_id, approver, role, clear_attested)
    return json_response(view.as_dict())


def graded_reset_reject(server: "Server", request: Request) -> Response:
    request_id = request.str_field("request_id")
    rejector = request.str_field("actor")
    role = request.str_field("role")
    view = server.runtime.graded.reject_reset(request_id, rejector, role)
    return json_response(view.as_dict())


def graded_requests(server: "Server", request: Request) -> Response:
    level = request.str_field("level") or (
        request.query.get("level", [""])[0] if request.query else ""
    )
    grade = grade_by_name(level) if level else None
    views = server.runtime.graded.requests(grade)
    return json_response({"requests": [view.as_dict() for view in views], "count": len(views)})


def graded_reset_scope(server: "Server", request: Request) -> Response:
    role = request.str_field("role") or (
        request.query.get("role", [""])[0] if request.query else ""
    )
    if not role:
        from .http import RequestError

        raise RequestError(400, "field role is required")
    payload = server.runtime.graded.resettable_for(role)
    active = server.runtime.graded.active_level()
    payload["active_level"] = "" if active is None else active.value
    open_requests = server.runtime.graded.open_requests()
    payload["open_requests"] = [view.as_dict() for view in open_requests]
    return json_response(payload)


def graded_ledger(server: "Server", request: Request) -> Response:
    graded = server.runtime.graded
    records = graded.ledger_records()
    return json_response(
        {"records": records, "count": len(records), "verification": graded.ledger.verify().as_dict()}
    )


def graded_ledger_verify(server: "Server", request: Request) -> Response:
    return json_response(server.runtime.graded.ledger.verify().as_dict())
