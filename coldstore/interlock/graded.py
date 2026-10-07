"""Graded safety interlock with staged shutdown sequences.

A trip jumps straight to the level the hazard demands and runs that level's
shutdown sequence one confirmed step at a time: a step only activates once
the previous step reports its equipment in place. A step that cannot
confirm before its deadline escalates the trip to the next level, because a
failed isolation means the hazard is not contained.

Resets are levelled too: each level latches independently, clears only its
own latch, and demands its own approval quorum (dual control with a safety
officer at the top level). Every trip, step, approval and reset is written
to the tamper evident ledger and mirrored to the audit stream.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from coldstore.audit import Auditor
from coldstore.comp import Bank
from coldstore.fan import FanSet
from coldstore.room import RoomRegistry
from coldstore.store.store import Store
from coldstore.valve import SupplyValve
from coldstore.valve.report import CLOSED

from .ledger import Ledger, Record
from .levels import (
    DEFAULT_THRESHOLDS,
    LEVELS,
    RESET_POLICIES,
    level_for_ppm,
    next_level,
    role_rank,
    validate_level,
    validate_role,
)
from .report import GradedState

LATCH_KEY_PREFIX = "interlock:graded:latch:"
THRESHOLDS_KEY = "interlock:graded:thresholds"
RUNS_KEY = "interlock:graded:runs"
APPROVALS_KEY_PREFIX = "interlock:graded:approvals:"

STEP_STOP_COMPRESSOR = "stop_compressor"
STEP_CLOSE_VALVE = "close_valve"
STEP_STOP_COLD_FANS = "stop_cold_fans"
STEP_STOP_EVAP_FANS = "stop_evap_fans"

RUN_ACTIVE = "active"
RUN_COMPLETE = "complete"
RUN_ESCALATED = "escalated"
RUN_STALLED = "stalled"

DEFAULT_STEP_TIMEOUT = 30.0


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _step(name: str, target: str) -> dict[str, object]:
    return {
        "name": name,
        "target": target,
        "status": "pending",
        "activated_at": "",
        "activated_epoch": 0.0,
        "confirmed_at": "",
    }


class GradedInterlock:
    """Runs levelled trips, staged shutdowns and quorum based resets."""

    def __init__(
        self,
        store: Store,
        auditor: Auditor,
        compressors: Bank,
        supply: SupplyValve,
        rooms: RoomRegistry,
        cold_fans: FanSet,
        evap_fans: FanSet,
        clock: Callable[[], float] = time.time,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
    ) -> None:
        self._store = store
        self._auditor = auditor
        self._compressors = compressors
        self._supply = supply
        self._rooms = rooms
        self._cold_fans = cold_fans
        self._evap_fans = evap_fans
        self._clock = clock
        self._step_timeout = step_timeout
        self._ledger = Ledger(store)

    # ------------------------------------------------------------------
    # configuration

    def thresholds(self) -> dict[str, float]:
        raw, present = self._store.get(THRESHOLDS_KEY)
        thresholds = dict(DEFAULT_THRESHOLDS)
        if present:
            try:
                stored = json.loads(raw)
            except ValueError:
                stored = {}
            if isinstance(stored, dict):
                for level in LEVELS:
                    if level in stored:
                        thresholds[level] = float(stored[level])
        return thresholds

    def set_threshold(self, level: str, value: float) -> dict[str, float]:
        validate_level(level)
        if value <= 0:
            raise ValueError("threshold must be positive")
        thresholds = self.thresholds()
        thresholds[level] = value
        if not (thresholds["L1"] <= thresholds["L2"] <= thresholds["L3"]):
            raise ValueError("thresholds must satisfy L1 <= L2 <= L3")
        self._store.put(THRESHOLDS_KEY, json.dumps(thresholds, ensure_ascii=False))
        return thresholds

    # ------------------------------------------------------------------
    # latches

    def latched(self, level: str) -> bool:
        validate_level(level)
        return self._store.get(f"{LATCH_KEY_PREFIX}{level}")[1]

    def latched_levels(self) -> list[str]:
        return [level for level in LEVELS if self.latched(level)]

    # ------------------------------------------------------------------
    # trips and shutdown sequences

    def evaluate(self, ppm: float, actor: str, zone: int = 0, target: str = "") -> dict[str, object]:
        """Trip the level a reading demands, unless a higher latch covers it."""

        level = level_for_ppm(ppm, self.thresholds())
        if not level:
            return {"tripped": False, "level": "", "run": None, "note": "below thresholds"}
        index = LEVELS.index(level)
        covering = [higher for higher in LEVELS[index:] if self.latched(higher)]
        if covering:
            return {
                "tripped": False,
                "level": level,
                "run": None,
                "note": f"covered by latched {covering[-1]}",
            }
        run = self.trip(level, f"ammonia {ppm:.2f} ppm", actor, zone=zone, target=target)
        return {"tripped": True, "level": level, "run": run, "note": ""}

    def trip(
        self, level: str, reason: str, actor: str, zone: int = 0, target: str = ""
    ) -> dict[str, object]:
        """Latch a level and start its shutdown sequence.

        Trips jump straight to the demanded level; they never climb through
        the lower levels first. A trip without a precise scope widens to the
        zone, then to the whole line, because failing safe beats failing
        narrow.
        """

        validate_level(level)
        if self.active_run(level) is not None:
            raise ValueError(f"level {level} shutdown sequence already active")
        steps = self._sequence(level, zone, target)
        run = {
            "id": uuid.uuid4().hex,
            "level": level,
            "reason": reason,
            "actor": actor,
            "zone": zone,
            "target": target,
            "started_at": _now(),
            "status": RUN_ACTIVE,
            "steps": steps,
        }
        runs = self._load_runs()
        runs.append(run)
        self._store.put(f"{LATCH_KEY_PREFIX}{level}", "true")
        self._save_approvals(level, [])
        self._record(
            "trip",
            level,
            actor,
            {"reason": reason, "run": run["id"], "steps": len(steps)},
        )
        if steps:
            self._activate(steps[0])
            self._save_runs(runs)
        else:
            self._complete(runs, run)
        return run

    def advance(self, run_id: str = "") -> dict[str, object]:
        """Drive the active shutdown sequence forward by exactly one step.

        The next step only activates once the current step confirms its
        equipment is in place. A step that stays unconfirmed past its
        deadline escalates the trip one level up.
        """

        runs = self._load_runs()
        run = self._find_run(runs, run_id)
        if run is None:
            raise ValueError("no active shutdown sequence")
        step = self._active_step(run)
        if step is None:
            self._complete(runs, run)
            return run
        try:
            self._execute(step)
        except ValueError:
            pass  # device did not take the command; confirmation decides
        if self._confirmed(step):
            step["status"] = "confirmed"
            step["confirmed_at"] = _now()
            self._record(
                "step",
                str(run["level"]),
                str(run["actor"]),
                {"run": run["id"], "step": step["name"], "target": step["target"]},
            )
            steps = run["steps"]
            index = steps.index(step)
            if index + 1 < len(steps):
                self._activate(steps[index + 1])
                self._save_runs(runs)
            else:
                self._complete(runs, run)
        elif self._clock() - float(step["activated_epoch"]) > self._step_timeout:
            step["status"] = "timeout"
            self._record(
                "step_timeout",
                str(run["level"]),
                str(run["actor"]),
                {"run": run["id"], "step": step["name"], "target": step["target"]},
            )
            self._escalate(runs, run)
        else:
            self._save_runs(runs)
        return run

    # ------------------------------------------------------------------
    # reset approvals

    def approve(self, actor: str, role: str, level: str) -> dict[str, object]:
        """Record one reset approval; reset the level once its quorum met.

        Every level resets on its own: clearing L3 never touches the L1/L2
        latches. The top level demands two distinct approvers, at least one
        of them a safety officer.
        """

        validate_level(level)
        validate_role(role)
        if not actor:
            raise ValueError("actor is required")
        policy = RESET_POLICIES[level]
        if not self.latched(level):
            raise ValueError(f"level {level} is not latched")
        if self.active_run(level) is not None:
            raise ValueError(f"level {level} shutdown sequence still active")
        if role_rank(role) < role_rank(policy.min_role):
            self._record("approve_denied", level, actor, {"role": role, "cause": "role too low"})
            raise ValueError(f"role {role} may not approve a {level} reset")
        approvals = self._approvals(level)
        if any(entry["actor"] == actor for entry in approvals):
            self._record("approve_denied", level, actor, {"role": role, "cause": "duplicate"})
            raise ValueError(f"{actor} already approved the {level} reset")
        approvals.append({"actor": actor, "role": role, "at": _now()})
        self._save_approvals(level, approvals)
        self._record("approve", level, actor, {"role": role, "count": len(approvals)})
        reset = False
        if self._quorum_met(level, approvals):
            self._store.delete(f"{LATCH_KEY_PREFIX}{level}")
            self._save_approvals(level, [])
            reset = True
            self._record(
                "reset",
                level,
                actor,
                {"approvals": [entry["actor"] for entry in approvals]},
            )
        return {
            "level": level,
            "approved": True,
            "reset": reset,
            "pending": 0 if reset else len(approvals),
            "required": policy.approvals,
        }

    def resettable(self, role: str = "") -> dict[str, object]:
        """Per level reset scope: latch, quorum and who may approve.

        This backs the review endpoint: an auditor sees, for every level,
        what is latched, how many approvals it still needs and whether the
        supplied role is allowed to give one.
        """

        if role:
            validate_role(role)
        levels: list[dict[str, object]] = []
        for level in LEVELS:
            policy = RESET_POLICIES[level]
            approvals = self._approvals(level)
            entry: dict[str, object] = {
                "level": level,
                "latched": self.latched(level),
                "active_run": self.active_run(level) is not None,
                "approvals_required": policy.approvals,
                "min_role": policy.min_role,
                "require_safety": policy.require_safety,
                "pending_approvals": [item["actor"] for item in approvals],
            }
            if role:
                entry["may_approve"] = role_rank(role) >= role_rank(policy.min_role)
            levels.append(entry)
        return {"levels": levels, "role": role}

    # ------------------------------------------------------------------
    # history and projections

    def ledger_records(self) -> list[Record]:
        return self._ledger.records()

    def ledger_count(self) -> int:
        return self._ledger.count()

    def verify(self) -> tuple[bool, int]:
        return self._ledger.verify()

    def runs(self) -> list[dict[str, object]]:
        return self._load_runs()

    def active_run(self, level: str = "") -> dict[str, object] | None:
        for run in self._load_runs():
            if run["status"] != RUN_ACTIVE:
                continue
            if level and run["level"] != level:
                continue
            return run
        return None

    def state(self) -> GradedState:
        return GradedState(
            thresholds=self.thresholds(),
            latched={level: self.latched(level) for level in LEVELS},
            runs=[dict(run) for run in self._load_runs()],
            approvals={level: self._approvals(level) for level in LEVELS},
            ledger=self._ledger.count(),
        )

    def describe(self) -> str:
        active = sum(1 for run in self._load_runs() if run["status"] == RUN_ACTIVE)
        latched = ",".join(self.latched_levels()) or "none"
        return f"graded latched={latched} active_runs={active} ledger={self._ledger.count()}"

    # ------------------------------------------------------------------
    # sequence construction

    def _sequence(self, level: str, zone: int, target: str) -> list[dict[str, object]]:
        if level == "L1":
            comps = [target] if target else self._zone_comps(zone) or self._all_comps()
            return self._isolation_steps(comps)
        if level == "L2":
            comps = self._zone_comps(zone) if zone else self._all_comps()
            return self._isolation_steps(comps)
        steps = self._isolation_steps(self._all_comps())
        if self._cold_fans.count():
            steps.append(_step(STEP_STOP_COLD_FANS, "cold"))
        if self._evap_fans.count():
            steps.append(_step(STEP_STOP_EVAP_FANS, "evap"))
        return steps

    def _isolation_steps(self, comps: list[str]) -> list[dict[str, object]]:
        steps = [_step(STEP_STOP_COMPRESSOR, comp) for comp in comps]
        steps += [_step(STEP_CLOSE_VALVE, comp) for comp in comps]
        return steps

    def _all_comps(self) -> list[str]:
        return self._compressors.compressor_ids()

    def _zone_comps(self, zone: int) -> list[str]:
        if zone <= 0:
            return []
        comps: list[str] = []
        for room_zone, room_id in self._rooms.mapping().items():
            if room_zone != zone:
                continue
            compressor, _, assigned = self._rooms.assignment(room_id)
            if assigned and compressor:
                comps.append(compressor)
        return sorted(set(comps))

    def _zone_for_target(self, target: str) -> int:
        for room_id in self._rooms.rooms_for_compressor(target):
            zone, present = self._rooms.room_zone(room_id)
            if present:
                return zone
        return 0

    # ------------------------------------------------------------------
    # step execution and confirmation

    def _activate(self, step: dict[str, object]) -> None:
        step["status"] = "active"
        step["activated_at"] = _now()
        step["activated_epoch"] = self._clock()

    def _execute(self, step: dict[str, object]) -> None:
        name = step["name"]
        target = str(step["target"])
        if name == STEP_STOP_COMPRESSOR:
            self._compressors.stop(target)
        elif name == STEP_CLOSE_VALVE:
            self._supply.close_valve(target)
        elif name == STEP_STOP_COLD_FANS:
            self._cold_fans.stop_all()
        elif name == STEP_STOP_EVAP_FANS:
            self._evap_fans.stop_all()

    def _confirmed(self, step: dict[str, object]) -> bool:
        name = step["name"]
        target = str(step["target"])
        if name == STEP_STOP_COMPRESSOR:
            compressor = self._compressors.compressor(target)
            return compressor is not None and not compressor.running
        if name == STEP_CLOSE_VALVE:
            return self._supply.position(target) == CLOSED
        if name == STEP_STOP_COLD_FANS:
            return not self._cold_fans.running_ids()
        if name == STEP_STOP_EVAP_FANS:
            return not self._evap_fans.running_ids()
        return False

    def _complete(self, runs: list[dict[str, object]], run: dict[str, object]) -> None:
        run["status"] = RUN_COMPLETE
        self._save_runs(runs)
        self._record("run_complete", str(run["level"]), str(run["actor"]), {"run": run["id"]})

    def _escalate(self, runs: list[dict[str, object]], run: dict[str, object]) -> None:
        level = str(run["level"])
        higher = next_level(level)
        if not higher:
            run["status"] = RUN_STALLED
            self._save_runs(runs)
            self._record("stalled", level, "system", {"run": run["id"]})
            return
        run["status"] = RUN_ESCALATED
        self._save_runs(runs)
        zone = int(run["zone"]) or self._zone_for_target(str(run["target"]))
        if self.active_run(higher) is not None:
            self._record(
                "escalate", higher, "system", {"from_run": run["id"], "note": "already active"}
            )
            return
        self._record("escalate", higher, "system", {"from_run": run["id"]})
        self.trip(higher, f"escalated from {level} run {run['id']}", "system", zone=zone)

    # ------------------------------------------------------------------
    # approvals and quorum

    def _quorum_met(self, level: str, approvals: list[dict[str, str]]) -> bool:
        policy = RESET_POLICIES[level]
        if len(approvals) < policy.approvals:
            return False
        if policy.require_safety and not any(
            entry["role"] == "safety" for entry in approvals
        ):
            return False
        return True

    def _approvals(self, level: str) -> list[dict[str, str]]:
        raw, present = self._store.get(f"{APPROVALS_KEY_PREFIX}{level}")
        if not present:
            return []
        try:
            stored = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(stored, list):
            return []
        return [
            {
                "actor": str(entry.get("actor", "")),
                "role": str(entry.get("role", "")),
                "at": str(entry.get("at", "")),
            }
            for entry in stored
            if isinstance(entry, dict)
        ]

    def _save_approvals(self, level: str, approvals: list[dict[str, str]]) -> None:
        key = f"{APPROVALS_KEY_PREFIX}{level}"
        if approvals:
            self._store.put(key, json.dumps(approvals, ensure_ascii=False))
        else:
            self._store.delete(key)

    # ------------------------------------------------------------------
    # persistence and history

    def _load_runs(self) -> list[dict[str, object]]:
        raw, present = self._store.get(RUNS_KEY)
        if not present:
            return []
        try:
            stored = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(stored, list):
            return []
        return [run for run in stored if isinstance(run, dict)]

    def _save_runs(self, runs: list[dict[str, object]]) -> None:
        if runs:
            self._store.put(RUNS_KEY, json.dumps(runs, ensure_ascii=False))
        else:
            self._store.delete(RUNS_KEY)

    def _find_run(
        self, runs: list[dict[str, object]], run_id: str
    ) -> dict[str, object] | None:
        if run_id:
            for run in runs:
                if run["id"] == run_id and run["status"] == RUN_ACTIVE:
                    return run
            return None
        active = [run for run in runs if run["status"] == RUN_ACTIVE]
        if not active:
            return None
        return max(active, key=lambda run: LEVELS.index(run["level"]))

    def _active_step(self, run: dict[str, object]) -> dict[str, object] | None:
        for step in run["steps"]:
            if step["status"] == "active":
                return step
        return None

    def _record(self, kind: str, level: str, actor: str, detail: dict[str, object]) -> None:
        """Append to the immutable ledger and mirror to the audit stream."""

        payload = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        self._ledger.append(kind, level, actor, payload)
        self._auditor.record(f"interlock:{kind}", payload)
