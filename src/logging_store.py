"""The decision log.

Every automated decision is written here, at every stage, whatever the outcome.
A8 reconciles logged decisions against tickets processed, and a gap means some
path through the system takes a decision without recording it - which is
precisely the path where something will eventually go wrong unobserved.

SQLite because it is a file, needs no service, and is entirely adequate at this
volume. The schema follows the minimum record in the governance framework, with
the fields kept as columns rather than one JSON blob so that a support manager
can answer "how many tickets did we block for private data last week" with a
query rather than a script.

Serves FR-11, NFR-05. Acceptance criterion A8.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from .config import settings
from .schema import DecisionRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    decision_id       TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    ticket_id         TEXT NOT NULL,
    stage             TEXT NOT NULL,
    input_summary     TEXT,
    model_name        TEXT,
    model_version     TEXT,
    prediction        TEXT,
    confidence        REAL,
    alternatives      TEXT,
    sources_used      TEXT,
    threshold_applied REAL,
    action_taken      TEXT NOT NULL,
    reason            TEXT NOT NULL,
    guardrail_results TEXT,
    prompt_version    TEXT,
    requirement_ids   TEXT,
    run_id            TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticket ON decisions(ticket_id);
CREATE INDEX IF NOT EXISTS idx_decisions_stage  ON decisions(stage);
CREATE INDEX IF NOT EXISTS idx_decisions_run    ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_action ON decisions(action_taken);
"""


class DecisionLog:
    """Append-only in practice; nothing in the system updates or deletes a row.

    Writes are serialised through a lock. SQLite handles concurrent writers
    itself, but the harness can run tickets in a thread pool and a lock here is
    cheaper than discovering a 'database is locked' error on ticket ninety.
    """

    def __init__(self, path: str | Path | None = None, *, run_id: str = ""):
        self.path = Path(path) if path else settings.sqlite_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # WAL so that a reader (the metrics endpoint, or me with a sqlite shell)
        # does not block the run that is writing.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.executescript(SCHEMA)
        self._connection.commit()
        self.written = 0

    def write(self, record: DecisionRecord) -> None:
        row = (
            record.decision_id,
            record.timestamp,
            record.ticket_id,
            record.stage,
            record.input_summary,
            record.model_name,
            record.model_version,
            record.prediction,
            record.confidence,
            json.dumps(record.alternatives),
            json.dumps(record.sources_used),
            record.threshold_applied,
            record.action_taken,
            record.reason,
            json.dumps(record.guardrail_results),
            record.prompt_version,
            json.dumps(record.requirement_ids),
            self.run_id,
        )
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            self._connection.commit()
            self.written += 1

    def write_many(self, records: Sequence[DecisionRecord]) -> None:
        for record in records:
            self.write(record)

    # -- reading back ----------------------------------------------------

    def for_ticket(self, ticket_id: str) -> list[dict[str, Any]]:
        cursor = self._connection.execute(
            "SELECT * FROM decisions WHERE ticket_id = ? ORDER BY timestamp, rowid", (ticket_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

    def count(self, *, run_id: str | None = None, stage: str | None = None) -> int:
        clauses, params = [], []
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if stage:
            clauses.append("stage = ?")
            params.append(stage)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self._connection.execute(f"SELECT COUNT(*) FROM decisions{where}", params).fetchone()[0]

    def distinct_tickets(self, *, run_id: str | None = None) -> int:
        if run_id:
            return self._connection.execute(
                "SELECT COUNT(DISTINCT ticket_id) FROM decisions WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
        return self._connection.execute("SELECT COUNT(DISTINCT ticket_id) FROM decisions").fetchone()[0]

    def reconcile(self, tickets_processed: int, *, run_id: str | None = None) -> dict[str, Any]:
        """A8. Decisions must reconcile against tickets processed.

        Returns the comparison rather than asserting it, so that the harness can
        put the result in the metrics report and the report can quote it even
        when it does not reconcile.
        """
        logged = self.distinct_tickets(run_id=run_id or self.run_id or None)
        return {
            "tickets_processed": tickets_processed,
            "tickets_with_decisions": logged,
            "total_decisions": self.count(run_id=run_id or self.run_id or None),
            "reconciles": logged == tickets_processed,
            "missing": max(0, tickets_processed - logged),
        }

    def counts_by(self, column: str, *, run_id: str | None = None) -> dict[str, int]:
        if column not in {"action_taken", "stage", "prediction"}:
            raise ValueError(f"not a groupable column: {column}")
        run = run_id or self.run_id
        if run:
            cursor = self._connection.execute(
                f"SELECT {column}, COUNT(*) FROM decisions WHERE run_id = ? GROUP BY {column}", (run,)
            )
        else:
            cursor = self._connection.execute(f"SELECT {column}, COUNT(*) FROM decisions GROUP BY {column}")
        return {row[0]: row[1] for row in cursor.fetchall()}

    def guardrail_activations(self, *, run_id: str | None = None) -> dict[str, int]:
        """Counts by guardrail, for the governance section of the report."""
        run = run_id or self.run_id
        query = "SELECT guardrail_results FROM decisions WHERE stage = 'validation'"
        params: tuple = ()
        if run:
            query += " AND run_id = ?"
            params = (run,)
        counts: dict[str, int] = {}
        for (raw,) in self._connection.execute(query, params).fetchall():
            try:
                results = json.loads(raw or "{}")
            except json.JSONDecodeError:
                continue
            for name, verdict in results.items():
                if verdict != "pass":
                    counts[name] = counts.get(name, 0) + 1
        return counts

    def export_jsonl(self, path: str | Path, *, run_id: str | None = None) -> int:
        """The log as a file somebody can read without sqlite installed. The
        report quotes from this and the video shows it."""
        run = run_id or self.run_id
        query = "SELECT * FROM decisions"
        params: tuple = ()
        if run:
            query += " WHERE run_id = ?"
            params = (run,)
        query += " ORDER BY timestamp, rowid"
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with path.open("w", encoding="utf-8") as handle:
            for row in self._connection.execute(query, params):
                record = dict(row)
                for field in ("alternatives", "sources_used", "guardrail_results", "requirement_ids"):
                    try:
                        record[field] = json.loads(record[field] or "null")
                    except (json.JSONDecodeError, TypeError):
                        pass
                handle.write(json.dumps(record) + "\n")
                written += 1
        return written

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "DecisionLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def open_log(path: str | Path | None = None, *, run_id: str = "") -> Iterator[DecisionLog]:
    log = DecisionLog(path, run_id=run_id)
    try:
        yield log
    finally:
        log.close()
