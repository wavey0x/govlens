"""Composite source cursors, proposals, and explicit write-ambiguity states."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import Action, Fact, Proposal, ProposalKey

SCHEMA_SIGNATURES = {
    "source_state": (
        ("protocol", "TEXT", 1, 1),
        ("source", "TEXT", 1, 2),
        ("next_proposal_id", "INTEGER", 1, 0),
    ),
    "proposals": (
        ("protocol", "TEXT", 1, 1),
        ("source", "TEXT", 1, 2),
        ("upstream_id", "INTEGER", 1, 3),
        ("proposal_json", "TEXT", 1, 0),
        ("status", "TEXT", 1, 0),
        ("analysis_json", "TEXT", 0, 0),
        ("gist_url", "TEXT", 0, 0),
        ("telegram_message_id", "INTEGER", 0, 0),
    ),
}


@dataclass(frozen=True)
class StoredProposal:
    proposal: Proposal
    analysis: dict[str, Any] | None
    gist_url: str | None


def _proposal(raw: str) -> Proposal:
    value = json.loads(raw)
    value["facts"] = {key: Fact(**fact) for key, fact in value["facts"].items()}
    value["actions"] = [Action(**action) for action in value["actions"]]
    value["unknowns"] = tuple(value["unknowns"])
    return Proposal(**value)


def _validate_schema(connection: sqlite3.Connection, *, allow_empty: bool) -> None:
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if not str(row[0]).startswith("sqlite_")
    }
    if not existing_tables:
        if allow_empty:
            return
        raise RuntimeError("GovLens database must use the current schema")
    if existing_tables != set(SCHEMA_SIGNATURES):
        raise RuntimeError("GovLens database must be empty or use the current schema")
    for table, expected in SCHEMA_SIGNATURES.items():
        actual = tuple(
            (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
            for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if actual != expected:
            raise RuntimeError("GovLens database must be empty or use the current schema")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            _validate_schema(self.connection, allow_empty=True)
        except Exception:
            self.connection.close()
            raise
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS source_state (
                protocol TEXT NOT NULL,
                source TEXT NOT NULL,
                next_proposal_id INTEGER NOT NULL,
                PRIMARY KEY (protocol, source)
            );
            CREATE TABLE IF NOT EXISTS proposals (
                protocol TEXT NOT NULL,
                source TEXT NOT NULL,
                upstream_id INTEGER NOT NULL,
                proposal_json TEXT NOT NULL,
                status TEXT NOT NULL,
                analysis_json TEXT,
                gist_url TEXT,
                telegram_message_id INTEGER,
                PRIMARY KEY (protocol, source, upstream_id)
            );
            """
        )
        self.connection.commit()

    @staticmethod
    def check(path: Path) -> bool:
        if not path.is_file():
            return False
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            _validate_schema(connection, allow_empty=False)
            return connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()

    def close(self) -> None:
        self.connection.close()

    def recover_interrupted(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE proposals SET status='review' WHERE status IN ('publishing', 'sending')"
            )
        return cursor.rowcount

    def cursor(self, protocol: str, source: str) -> int | None:
        row = self.connection.execute(
            "SELECT next_proposal_id FROM source_state WHERE protocol=? AND source=?",
            (protocol, source),
        ).fetchone()
        return int(row["next_proposal_id"]) if row else None

    def initialize(self, protocol: str, source: str, next_proposal_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO source_state(protocol, source, next_proposal_id) "
                "VALUES(?, ?, ?)",
                (protocol, source, next_proposal_id),
            )

    def discover(self, proposal: Proposal) -> None:
        raw = json.dumps(proposal.as_dict(), sort_keys=True, separators=(",", ":"))
        key = proposal.key
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO proposals("
                "protocol, source, upstream_id, proposal_json, status"
                ") VALUES(?, ?, ?, ?, 'pending')",
                (key.protocol, key.source, key.upstream_id, raw),
            )
            self.connection.execute(
                "UPDATE source_state SET next_proposal_id=? WHERE protocol=? AND source=?",
                (key.upstream_id + 1, key.protocol, key.source),
            )

    def pending(self) -> list[StoredProposal]:
        rows = self.connection.execute(
            "SELECT proposal_json, analysis_json, gist_url FROM proposals "
            "WHERE status IN ('pending', 'analyzed', 'published') "
            "ORDER BY protocol, source, upstream_id"
        ).fetchall()
        return [
            StoredProposal(
                proposal=_proposal(row["proposal_json"]),
                analysis=json.loads(row["analysis_json"]) if row["analysis_json"] else None,
                gist_url=row["gist_url"],
            )
            for row in rows
        ]

    @staticmethod
    def _identity(key: ProposalKey) -> tuple[str, str, int]:
        return key.protocol, key.source, key.upstream_id

    def save_analysis(self, key: ProposalKey, analysis: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE proposals SET status='analyzed', analysis_json=? "
                "WHERE protocol=? AND source=? AND upstream_id=?",
                (json.dumps(analysis, sort_keys=True), *self._identity(key)),
            )

    def publishing(self, key: ProposalKey) -> None:
        self._status(key, "publishing")

    def publication_failed(self, key: ProposalKey) -> None:
        self._status(key, "analyzed")

    def published(self, key: ProposalKey, gist_url: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE proposals SET status='published', gist_url=? "
                "WHERE protocol=? AND source=? AND upstream_id=?",
                (gist_url, *self._identity(key)),
            )

    def sending(self, key: ProposalKey) -> None:
        self._status(key, "sending")

    def sent(self, key: ProposalKey, message_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE proposals SET status='sent', telegram_message_id=? "
                "WHERE protocol=? AND source=? AND upstream_id=?",
                (message_id, *self._identity(key)),
            )

    def review(self, key: ProposalKey, gist_url: str | None = None) -> None:
        with self.connection:
            if gist_url:
                self.connection.execute(
                    "UPDATE proposals SET status='review', gist_url=? "
                    "WHERE protocol=? AND source=? AND upstream_id=?",
                    (gist_url, *self._identity(key)),
                )
            else:
                self.connection.execute(
                    "UPDATE proposals SET status='review' "
                    "WHERE protocol=? AND source=? AND upstream_id=?",
                    self._identity(key),
                )

    def _status(self, key: ProposalKey, status: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE proposals SET status=? WHERE protocol=? AND source=? AND upstream_id=?",
                (status, *self._identity(key)),
            )

    def status_counts(self) -> dict[str, int]:
        return {
            str(row["status"]): int(row["total"])
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS total FROM proposals GROUP BY status ORDER BY status"
            )
        }
