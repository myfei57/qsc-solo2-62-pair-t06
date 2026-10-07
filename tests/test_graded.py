"""Behavioural tests for the graded interlock, shutdown sequence and ledger."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.audit import Auditor
from coldstore.interlock import (
    INSTALL_MANAGER,
    OPERATOR,
    SAFETY_OFFICER,
    SHIFT_SUPERVISOR,
    Grade,
    GradedInterlock,
    grade_for_ppm,
    reset_scope,
)
from coldstore.interlock.ledger import LEDGER_KEY
from coldstore.interlock.sequence import IN_POSITION, PENDING, SEQUENCES
from coldstore.store import Store


class GradeClassificationCase(unittest.TestCase):
    def test_reading_jumps_directly_to_measured_level(self) -> None:
        self.assertIsNone(grade_for_ppm(24.9, 25.0))
        self.assertEqual(grade_for_ppm(25.0, 25.0), Grade.L1)
        self.assertEqual(grade_for_ppm(50.0, 25.0), Grade.L2)
        self.assertEqual(grade_for_ppm(100.0, 25.0), Grade.L3)

    def test_threshold_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            grade_for_ppm(1.0, 0.0)


class ShutdownSequenceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.graded = GradedInterlock(self.store, Auditor(self.store))

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_level_jumps_straight_to_l3(self) -> None:
        result = self.graded.trip(Grade.L3, "major leak")
        self.assertTrue(result["raised"])
        self.assertEqual(self.graded.active_level(), Grade.L3)

    def test_level_can_only_move_up_while_active(self) -> None:
        self.graded.trip(Grade.L1, "minor")
        down = self.graded.trip(Grade.L3, "worse")
        self.assertTrue(down["raised"])
        back = self.graded.trip(Grade.L1, "smaller reading")
        self.assertFalse(back["raised"])
        self.assertEqual(self.graded.active_level(), Grade.L3)

    def test_sequence_advances_one_rung_after_each_confirmation(self) -> None:
        self.graded.trip(Grade.L1, "minor")
        projection = self.graded.sequence.projection()
        self.assertEqual(len(projection.steps), len(SEQUENCES[Grade.L1]))
        self.assertEqual(projection.steps[0].status, PENDING)
        self.graded.advance_sequence()
        projection = self.graded.sequence.projection()
        self.assertEqual(projection.steps[0].status, IN_POSITION)
        self.assertEqual(projection.steps[1].status, PENDING)
        self.assertFalse(projection.complete)

    def test_sequence_must_finish_before_reset(self) -> None:
        self.graded.trip(Grade.L1, "minor")
        with self.assertRaises(ValueError):
            self.graded.request_reset(SHIFT_SUPERVISOR, SHIFT_SUPERVISOR, True)
        for _ in SEQUENCES[Grade.L1]:
            self.graded.advance_sequence()
        self.assertTrue(self.graded.sequence.projection().complete)


class ResetWorkflowCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(self.store)
        self.graded = GradedInterlock(self.store, self.auditor)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _trip_and_finish(self, grade: Grade) -> None:
        self.graded.trip(grade, f"{grade.value} leak")
        for _ in SEQUENCES[grade]:
            self.graded.advance_sequence()

    def test_l1_single_confirmation(self) -> None:
        self._trip_and_finish(Grade.L1)
        with self.assertRaises(ValueError):
            self.graded.request_reset("op", OPERATOR, True)
        with self.assertRaises(ValueError):
            self.graded.request_reset("sup", SHIFT_SUPERVISOR, False)
        view = self.graded.request_reset("sup", SHIFT_SUPERVISOR, True)
        self.assertEqual(view.status, "executed")
        self.assertFalse(self.graded.is_active())

    def test_l2_requires_two_different_people(self) -> None:
        self._trip_and_finish(Grade.L2)
        view = self.graded.request_reset("op", OPERATOR, True)
        self.assertEqual(view.status, "open")
        with self.assertRaises(ValueError):
            self.graded.approve_reset(view.id, "op", OPERATOR, True)
        with self.assertRaises(ValueError):
            self.graded.approve_reset(view.id, "mgr", INSTALL_MANAGER, True)
        with self.assertRaises(ValueError):
            self.graded.approve_reset(view.id, "so", SAFETY_OFFICER, False)
        view = self.graded.approve_reset(view.id, "so", SAFETY_OFFICER, True)
        self.assertEqual(view.status, "executed")
        self.assertFalse(self.graded.is_active())

    def test_l3_requires_two_independent_counter_signatures(self) -> None:
        self._trip_and_finish(Grade.L3)
        with self.assertRaises(ValueError):
            self.graded.request_reset("op", OPERATOR, True)
        view = self.graded.request_reset("sup", SHIFT_SUPERVISOR, True)
        self.graded.approve_reset(view.id, "so", SAFETY_OFFICER, True)
        self.assertEqual(self.graded.request_view(view.id).status, "open")
        view = self.graded.approve_reset(view.id, "mgr", INSTALL_MANAGER, True)
        self.assertEqual(view.status, "executed")
        self.assertFalse(self.graded.is_active())

    def test_one_person_cannot_sign_twice(self) -> None:
        self._trip_and_finish(Grade.L3)
        view = self.graded.request_reset("sup", SHIFT_SUPERVISOR, True)
        self.graded.approve_reset(view.id, "so", SAFETY_OFFICER, True)
        with self.assertRaises(ValueError):
            self.graded.approve_reset(view.id, "so", SAFETY_OFFICER, True)

    def test_rejection_closes_the_request(self) -> None:
        self._trip_and_finish(Grade.L2)
        view = self.graded.request_reset("op", OPERATOR, True)
        view = self.graded.reject_reset(view.id, "so", SAFETY_OFFICER)
        self.assertEqual(view.status, "rejected")
        with self.assertRaises(ValueError):
            self.graded.approve_reset(view.id, "so2", SAFETY_OFFICER, True)

    def test_reset_scope_lists_only_authorized_levels(self) -> None:
        self.assertEqual(reset_scope(OPERATOR), [])
        self.assertEqual(reset_scope(SHIFT_SUPERVISOR), [Grade.L1])
        self.assertEqual(reset_scope(SAFETY_OFFICER), [Grade.L2, Grade.L3])
        self.assertEqual(reset_scope(INSTALL_MANAGER), [Grade.L3])


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(self.store)
        self.graded = GradedInterlock(self.store, self.auditor)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_history_is_complete_and_mirrored_to_audit(self) -> None:
        self.graded.trip(Grade.L1, "minor")
        self.graded.advance_sequence()
        kinds = {record.kind for record in self.graded.ledger.records()}
        self.assertIn("trip", kinds)
        self.assertIn("shutdown_step", kinds)
        audit_kinds = set(self.auditor.count_by_kind())
        self.assertIn("graded:trip", audit_kinds)
        self.assertIn("graded:shutdown_step", audit_kinds)

    def test_chain_detects_tampering(self) -> None:
        self.graded.trip(Grade.L1, "original")
        self.assertTrue(self.graded.ledger.verify().ok)
        raw = json.loads(self.store.get(LEDGER_KEY)[0])
        record = json.loads(raw[1])
        record["payload"]["reason"] = "forged"
        raw[1] = json.dumps(record)
        self.store.put(LEDGER_KEY, json.dumps(raw))
        verdict = self.graded.ledger.verify()
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.broken_at, 1)


if __name__ == "__main__":
    unittest.main()
