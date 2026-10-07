"""Tamper-evident, append-only history for graded interlock events.

Shutdown steps, reset requests, approvals and the reset action itself are all
written here.  Every record carries the SHA-256 hash of the canonical record
before it, so deleting or altering a past record breaks every link that
follows.  There is deliberately no update or delete method.

Each append is also mirrored into the runtime :class:`~coldstore.audit.Auditor`
so the graded safety history lands in the same audit stream as the rest of the
control line.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass

from coldstore.audit import Auditor
from coldstore.store.commands import load_commands
from coldstore.store.store import Store

LEDGER_KEY = "graded:ledger"

GENESIS_PREV = "0" * 64
GENESIS_KIND = "genesis"


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def canonical_payload(payload: dict[str, object]) -> str:
    """Stable JSON text used as the hash input."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(prev_hash: str, sequence: int, at: str, kind: str, payload_text: str) -> str:
    body = f"{prev_hash}\n{sequence}\n{at}\n{kind}\n{payload_text}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LedgerRecord:
    """One sealed link in the chain."""

    sequence: int
    id: str
    at: str
    kind: str
    actor: str
    payload: dict[str, object]
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "id": self.id,
            "at": self.at,
            "kind": self.kind,
            "actor": self.actor,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class LedgerVerification:
    """Result of walking the whole chain."""

    records: int
    ok: bool
    broken_at: int | None
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "records": self.records,
            "ok": self.ok,
            "broken_at": self.broken_at,
            "reason": self.reason,
        }


class Ledger:
    """Appends graded interlock events and verifies the chain."""

    def __init__(self, store: Store, auditor: Auditor | None = None) -> None:
        self._store = store
        self._auditor = auditor

    def _read_raw(self) -> list[str]:
        return load_commands(self._store, LEDGER_KEY)

    def records(self) -> list[LedgerRecord]:
        records: list[LedgerRecord] = []
        for item in self._read_raw():
            try:
                raw = json.loads(item)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload")
            records.append(
                LedgerRecord(
                    sequence=int(raw.get("sequence", 0)),
                    id=str(raw.get("id", "")),
                    at=str(raw.get("at", "")),
                    kind=str(raw.get("kind", "")),
                    actor=str(raw.get("actor", "")),
                    prev_hash=str(raw.get("prev_hash", "")),
                    hash=str(raw.get("hash", "")),
                    payload=payload if isinstance(payload, dict) else {},
                )
            )
        return records

    def count(self) -> int:
        return len(self.records())

    def last(self) -> LedgerRecord | None:
        records = self.records()
        return records[-1] if records else None

    def append(
        self, kind: str, payload: dict[str, object], actor: str = "system"
    ) -> LedgerRecord:
        """Seal one new record; also seed the chain and mirror to the audit."""

        records = self.records()
        if not records:
            records.append(self._genesis_record())

        previous = records[-1]
        sequence = previous.sequence + 1
        at = utc_now()
        payload_text = canonical_payload(payload)
        record = LedgerRecord(
            sequence=sequence,
            id=uuid.uuid4().hex,
            at=at,
            kind=kind,
            actor=actor,
            payload=dict(payload),
            prev_hash=previous.hash,
            hash=digest(previous.hash, sequence, at, kind, payload_text),
        )

        chain = self._read_raw()
        if not chain:
            chain.append(json.dumps(records[0].as_dict(), ensure_ascii=False))
        chain.append(json.dumps(record.as_dict(), ensure_ascii=False))
        self._store.put(LEDGER_KEY, json.dumps(chain, ensure_ascii=False))

        if self._auditor is not None:
            self._auditor.record(f"graded:{kind}", payload_text)
        return record

    def _genesis_record(self) -> LedgerRecord:
        at = utc_now()
        payload: dict[str, object] = {}
        payload_text = canonical_payload(payload)
        return LedgerRecord(
            sequence=0,
            id=uuid.uuid4().hex,
            at=at,
            kind=GENESIS_KIND,
            actor="system",
            payload=payload,
            prev_hash=GENESIS_PREV,
            hash=digest(GENESIS_PREV, 0, at, GENESIS_KIND, payload_text),
        )

    def verify(self) -> LedgerVerification:
        """Recompute every link; report the first broken one, if any."""

        records = self.records()
        if not records:
            return LedgerVerification(0, True, None, "empty chain")
        first = records[0]
        if first.sequence != 0 or first.kind != GENESIS_KIND or first.prev_hash != GENESIS_PREV:
            return LedgerVerification(len(records), False, first.sequence, "bad genesis record")
        expected_hash = digest(
            GENESIS_PREV,
            first.sequence,
            first.at,
            first.kind,
            canonical_payload(first.payload),
        )
        if expected_hash != first.hash:
            return LedgerVerification(len(records), False, first.sequence, "genesis hash mismatch")

        prev_hash = first.hash
        for record in records[1:]:
            if record.prev_hash != prev_hash:
                return LedgerVerification(
                    len(records), False, record.sequence, "previous hash link broken"
                )
            expected_hash = digest(
                prev_hash,
                record.sequence,
                record.at,
                record.kind,
                canonical_payload(record.payload),
            )
            if expected_hash != record.hash:
                return LedgerVerification(
                    len(records), False, record.sequence, "record hash mismatch"
                )
            prev_hash = record.hash
        return LedgerVerification(len(records), True, None, "chain intact")
