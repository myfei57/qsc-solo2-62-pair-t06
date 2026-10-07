"""Tamper evident, append only history for graded interlock events.

Every record carries the hash of its predecessor, so rewriting or dropping
history breaks the chain and is caught by :meth:`Ledger.verify`. There is
deliberately no update or delete: shutdowns, approvals and resets must stay
auditable.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

LEDGER_KEY = "interlock:graded:ledger"
GENESIS = "0" * 64

HASHED_FIELDS = ("seq", "time", "kind", "level", "actor", "detail", "prev_hash")


def _digest(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Record:
    """One immutable ledger entry."""

    seq: int
    time: str
    kind: str
    level: str
    actor: str
    detail: str
    prev_hash: str
    hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "seq": self.seq,
            "time": self.time,
            "kind": self.kind,
            "level": self.level,
            "actor": self.actor,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    def hashed_payload(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in HASHED_FIELDS}


class Ledger:
    """Appends hash chained records and verifies the chain."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def append(self, kind: str, level: str, actor: str, detail: str) -> Record:
        records = self.records()
        payload: dict[str, object] = {
            "seq": len(records) + 1,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": kind,
            "level": level,
            "actor": actor,
            "detail": detail,
            "prev_hash": records[-1].hash if records else GENESIS,
        }
        record = Record(
            seq=int(payload["seq"]),
            time=str(payload["time"]),
            kind=kind,
            level=level,
            actor=actor,
            detail=detail,
            prev_hash=str(payload["prev_hash"]),
            hash=_digest(payload),
        )
        append_command(self._store, LEDGER_KEY, json.dumps(record.as_dict(), ensure_ascii=False))
        return record

    def records(self) -> list[Record]:
        records: list[Record] = []
        for item in load_commands(self._store, LEDGER_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            records.append(
                Record(
                    seq=int(payload.get("seq", 0)),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    level=str(payload.get("level", "")),
                    actor=str(payload.get("actor", "")),
                    detail=str(payload.get("detail", "")),
                    prev_hash=str(payload.get("prev_hash", "")),
                    hash=str(payload.get("hash", "")),
                )
            )
        return records

    def count(self) -> int:
        return len(self.records())

    def verify(self) -> tuple[bool, int]:
        """Walk the chain; return (ok, sequence number of the first bad record)."""

        prev_hash = GENESIS
        for record in self.records():
            if record.prev_hash != prev_hash:
                return False, record.seq
            if record.hash != _digest(record.hashed_payload()):
                return False, record.seq
            prev_hash = record.hash
        return True, 0
