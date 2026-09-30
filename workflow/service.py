"""Transactional workflow operations and finite-state-machine enforcement."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import SCHEMA_VERSION, connect, initialize
from .ticket_document import (
    TicketDocumentError,
    parse_document,
    render_document,
    validate_ticket_id,
)


ACTORS = {"project-manager", "developer", "tester", "reviewer", "human"}
PRIORITIES = {"LOW", "NORMAL", "HIGH", "URGENT"}
WORKFLOW_GITIGNORE = "workflow.db\nworkflow.db-shm\nworkflow.db-wal\n"
ROLE_FOR_STATUS = {
    "BACKLOG": None,
    "READY": None,
    "DEVELOPMENT": "developer",
    "TESTING": "tester",
    "REVIEW": "reviewer",
    "DONE": None,
}


class WorkflowError(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class WorkflowService:
    def __init__(self, root: Path, schema_path: Path | None = None):
        self.root = Path(root).resolve()
        self.workflow_path = self.root / ".agent"
        self.database_path = self.workflow_path / "workflow.db"
        self.schema_path = schema_path or Path(__file__).resolve().parents[1] / "schema" / "schema.sql"
        self.tickets_path = self.workflow_path / "tickets"

    def init(self) -> None:
        self.tickets_path.mkdir(parents=True, exist_ok=True)
        try:
            self._write_workflow_gitignore()
            initialize(self.database_path, self.schema_path)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            raise WorkflowError("INITIALIZATION_FAILED", str(error)) from error

    def migrate_legacy(self) -> dict[str, Any]:
        legacy_database = self.root / "data" / "workflow.db"
        legacy_tickets = self.root / "tickets"
        if not legacy_database.is_file() or not legacy_tickets.is_dir():
            raise WorkflowError(
                "LEGACY_WORKFLOW_NOT_FOUND",
                "Expected legacy data/workflow.db and tickets/ in the current project",
            )
        if self.workflow_path.exists():
            raise WorkflowError(
                "MIGRATION_CONFLICT",
                f"Migration destination already exists: {self.workflow_path}",
            )

        staging_path = Path(tempfile.mkdtemp(prefix=".agent.migrate.", dir=self.root))
        try:
            staging_tickets = staging_path / "tickets"
            staging_tickets.mkdir()
            (staging_path / ".gitignore").write_text(WORKFLOW_GITIGNORE, encoding="utf-8")
            source = connect(legacy_database)
            try:
                versions = [row[0] for row in source.execute("SELECT version FROM schema_version")]
                if versions != [SCHEMA_VERSION]:
                    raise WorkflowError(
                        "UNSUPPORTED_SCHEMA",
                        f"Unsupported schema version(s): {versions}; expected [{SCHEMA_VERSION}]",
                    )
                ticket_ids = {
                    row[0] for row in source.execute("SELECT ticket_id FROM tickets ORDER BY ticket_id")
                }
                destination = connect(staging_path / "workflow.db")
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()

            document_ids: set[str] = set()
            for path in legacy_tickets.glob("TICKET-*.md"):
                try:
                    document = parse_document(path.read_text(encoding="utf-8"), expect_canonical=True)
                except (OSError, UnicodeError) as error:
                    raise WorkflowError("DOCUMENT_READ_FAILED", str(error)) from error
                except TicketDocumentError as error:
                    raise WorkflowError("INVALID_DOCUMENT", str(error)) from error
                if path.name != f"{document.ticket_id}.md":
                    raise WorkflowError(
                        "DOCUMENT_ID_MISMATCH",
                        f"Document ID does not match filename: {path}",
                    )
                document_ids.add(document.ticket_id)
                shutil.copy2(path, staging_tickets / path.name)

            if ticket_ids != document_ids:
                missing = sorted(ticket_ids - document_ids)
                orphaned = sorted(document_ids - ticket_ids)
                raise WorkflowError(
                    "MIGRATION_STATE_MISMATCH",
                    "Legacy database rows and ticket documents do not match",
                    missing_documents=missing,
                    orphaned_documents=orphaned,
                )

            staging_path.replace(self.workflow_path)
        except WorkflowError:
            shutil.rmtree(staging_path, ignore_errors=True)
            raise
        except (OSError, sqlite3.Error) as error:
            shutil.rmtree(staging_path, ignore_errors=True)
            raise WorkflowError("MIGRATION_FAILED", str(error)) from error

        return {
            "migrated_to": str(self.workflow_path),
            "legacy_database_retained": str(legacy_database),
            "legacy_tickets_retained": str(legacy_tickets),
            "ticket_count": len(ticket_ids),
        }

    def create(self, draft_path: Path, actor: str, priority: str = "NORMAL") -> dict[str, Any]:
        self._require_actor(actor, {"project-manager", "human"})
        priority = priority.upper()
        if priority not in PRIORITIES:
            raise WorkflowError("INVALID_PRIORITY", f"Invalid priority: {priority}")
        try:
            draft = parse_document(Path(draft_path).read_text(encoding="utf-8"), expect_canonical=False)
        except (OSError, UnicodeError) as error:
            raise WorkflowError("DOCUMENT_READ_FAILED", str(error)) from error
        except TicketDocumentError as error:
            raise WorkflowError("INVALID_DOCUMENT", str(error)) from error

        connection = self._connection()
        final_path: Path | None = None
        final_identity: tuple[int, int] | None = None
        temporary_path: Path | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            next_number = connection.execute(
                "SELECT COALESCE(MAX(CAST(substr(ticket_id, 8) AS INTEGER)), 0) + 1 FROM tickets"
            ).fetchone()[0]
            ticket_id = f"TICKET-{next_number:03d}"
            if ticket_id in draft.dependencies:
                raise WorkflowError("SELF_DEPENDENCY", f"{ticket_id} cannot depend on itself")
            candidate_path = self.tickets_path / f"{ticket_id}.md"
            if candidate_path.exists():
                raise WorkflowError("DOCUMENT_EXISTS", f"Ticket document already exists: {candidate_path}")
            now = _timestamp()
            connection.execute(
                """INSERT INTO tickets
                   (ticket_id, status, assigned_to, priority, attempt_count,
                    blocked_from_status, created_at, started_at, completed_at)
                   VALUES (?, 'BACKLOG', NULL, ?, 0, NULL, ?, NULL, NULL)""",
                (ticket_id, priority, now),
            )
            self._insert_event(connection, ticket_id, actor, "CREATED", f"Created with priority {priority}", now)

            self.tickets_path.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{ticket_id}.", suffix=".tmp", dir=self.tickets_path
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(render_document(ticket_id, draft))
                handle.flush()
                os.fsync(handle.fileno())
            # Install without overwriting a document created after the check.
            os.link(temporary_path, candidate_path)
            final_path = candidate_path
            installed = final_path.stat()
            final_identity = (installed.st_dev, installed.st_ino)
            temporary_path.unlink()
            temporary_path = None
            result = dict(self._ticket_row(connection, ticket_id))
            connection.execute("COMMIT")
            return result
        except WorkflowError:
            self._rollback(connection)
            self._cleanup_created_file(final_path, final_identity, temporary_path)
            raise
        except (OSError, sqlite3.Error) as error:
            self._rollback(connection)
            self._cleanup_created_file(final_path, final_identity, temporary_path)
            raise WorkflowError("CREATE_FAILED", str(error)) from error
        finally:
            connection.close()

    def ready(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"project-manager", "human"})
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            document = self._read_document(ticket_id)
            ticket = self._require_status(connection, ticket_id, "BACKLOG", "READY")
            for dependency in document.dependencies:
                if dependency == ticket_id:
                    raise WorkflowError("SELF_DEPENDENCY", f"{ticket_id} cannot depend on itself")
                row = connection.execute(
                    "SELECT status FROM tickets WHERE ticket_id = ?", (dependency,)
                ).fetchone()
                if row is None:
                    raise WorkflowError(
                        "UNKNOWN_DEPENDENCY", f"Dependency does not exist: {dependency}", dependency=dependency
                    )
                if row["status"] != "DONE":
                    raise WorkflowError(
                        "DEPENDENCY_NOT_DONE",
                        f"Dependency {dependency} is {row['status']}, not DONE",
                        dependency=dependency,
                        dependency_status=row["status"],
                    )
            self._update_status(connection, ticket, "READY", actor, "MARKED_READY", message)
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()
        return self._state(ticket_id)

    def start(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"developer"})
        return self._transition(ticket_id, actor, "READY", "DEVELOPMENT", "STARTED", message, increment=True)

    def submit(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"developer"})
        return self._transition(ticket_id, actor, "DEVELOPMENT", "TESTING", "SUBMITTED", message)

    def test_pass(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"tester"})
        return self._transition(ticket_id, actor, "TESTING", "REVIEW", "TEST_PASSED", message)

    def test_fail(self, ticket_id: str, actor: str, message: str) -> dict[str, Any]:
        self._require_actor(actor, {"tester"})
        self._require_message(message, "test-fail")
        return self._transition(
            ticket_id, actor, "TESTING", "DEVELOPMENT", "TEST_FAILED", message, increment=True
        )

    def approve(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"reviewer"})
        return self._transition(ticket_id, actor, "REVIEW", "DONE", "APPROVED", message)

    def reject(self, ticket_id: str, actor: str, message: str) -> dict[str, Any]:
        self._require_actor(actor, {"reviewer"})
        self._require_message(message, "reject")
        return self._transition(
            ticket_id, actor, "REVIEW", "DEVELOPMENT", "CHANGES_REQUESTED", message, increment=True
        )

    def block(self, ticket_id: str, actor: str, message: str) -> dict[str, Any]:
        self._require_actor(actor, ACTORS)
        self._require_message(message, "block")
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ticket = self._ticket_row(connection, ticket_id)
            status = ticket["status"]
            if status in {"DONE", "BLOCKED"}:
                self._invalid_transition(ticket_id, status, "BLOCKED")
            allowed = {"project-manager", "human"}
            owner = ROLE_FOR_STATUS[status]
            if owner:
                allowed.add(owner)
            self._require_actor(actor, allowed)
            now = _timestamp()
            connection.execute(
                "UPDATE tickets SET status = 'BLOCKED', blocked_from_status = ? WHERE ticket_id = ?",
                (status, ticket_id),
            )
            self._insert_event(connection, ticket_id, actor, "BLOCKED", message, now)
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()
        return self._state(ticket_id)

    def unblock(self, ticket_id: str, actor: str, message: str | None = None) -> dict[str, Any]:
        self._require_actor(actor, {"project-manager", "human"})
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ticket = self._require_status(connection, ticket_id, "BLOCKED", "previous state")
            restored = ticket["blocked_from_status"]
            connection.execute(
                "UPDATE tickets SET status = ?, blocked_from_status = NULL WHERE ticket_id = ?",
                (restored, ticket_id),
            )
            self._insert_event(connection, ticket_id, actor, "UNBLOCKED", message, _timestamp())
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()
        return self._state(ticket_id)

    def get(self, ticket_id: str) -> dict[str, Any]:
        ticket = self._state(ticket_id)
        document = self._read_document(ticket_id)
        ticket.update(
            title=document.title,
            dependencies=list(document.dependencies),
            document_path=str(self.tickets_path / f"{ticket_id}.md"),
            content=(self.tickets_path / f"{ticket_id}.md").read_text(encoding="utf-8"),
        )
        return ticket

    def _state(self, ticket_id: str) -> dict[str, Any]:
        connection = self._connection()
        try:
            return dict(self._ticket_row(connection, ticket_id))
        finally:
            connection.close()

    def list(self, status: str | None = None, assigned_to: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[str] = []
        if status:
            status = status.upper()
            if status not in {*ROLE_FOR_STATUS, "BLOCKED"}:
                raise WorkflowError("INVALID_STATUS", f"Invalid status: {status}")
            clauses.append("status = ?")
            parameters.append(status)
        if assigned_to:
            if assigned_to not in ACTORS - {"human"}:
                raise WorkflowError("INVALID_ASSIGNEE", f"Invalid logical role: {assigned_to}")
            clauses.append("assigned_to = ?")
            parameters.append(assigned_to)
        sql = "SELECT * FROM tickets"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CAST(substr(ticket_id, 8) AS INTEGER)"
        connection = self._connection()
        try:
            return [dict(row) for row in connection.execute(sql, parameters)]
        finally:
            connection.close()

    def history(self, ticket_id: str) -> list[dict[str, Any]]:
        self._validate_id(ticket_id)
        connection = self._connection()
        try:
            self._ticket_row(connection, ticket_id)
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM ticket_events WHERE ticket_id = ? ORDER BY id", (ticket_id,)
                )
            ]
        finally:
            connection.close()

    def _transition(
        self,
        ticket_id: str,
        actor: str,
        expected: str,
        target: str,
        event: str,
        message: str | None,
        increment: bool = False,
    ) -> dict[str, Any]:
        connection = self._connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ticket = self._require_status(connection, ticket_id, expected, target)
            self._update_status(connection, ticket, target, actor, event, message, increment)
            connection.execute("COMMIT")
        except Exception:
            self._rollback(connection)
            raise
        finally:
            connection.close()
        return self._state(ticket_id)

    def _update_status(
        self,
        connection: sqlite3.Connection,
        ticket: sqlite3.Row,
        target: str,
        actor: str,
        event: str,
        message: str | None,
        increment: bool = False,
    ) -> None:
        now = _timestamp()
        started_at = ticket["started_at"]
        if target == "DEVELOPMENT" and started_at is None:
            started_at = now
        completed_at = now if target == "DONE" else ticket["completed_at"]
        attempts = ticket["attempt_count"] + (1 if increment else 0)
        connection.execute(
            """UPDATE tickets
               SET status = ?, assigned_to = ?, attempt_count = ?, started_at = ?,
                   completed_at = ?, blocked_from_status = NULL
               WHERE ticket_id = ?""",
            (target, ROLE_FOR_STATUS[target], attempts, started_at, completed_at, ticket["ticket_id"]),
        )
        self._insert_event(connection, ticket["ticket_id"], actor, event, message, now)

    def _read_document(self, ticket_id: str):
        self._validate_id(ticket_id)
        path = self.tickets_path / f"{ticket_id}.md"
        try:
            document = parse_document(path.read_text(encoding="utf-8"), expect_canonical=True)
        except (OSError, UnicodeError) as error:
            raise WorkflowError("DOCUMENT_READ_FAILED", str(error), ticket_id=ticket_id) from error
        except TicketDocumentError as error:
            raise WorkflowError("INVALID_DOCUMENT", str(error), ticket_id=ticket_id) from error
        if document.ticket_id != ticket_id:
            raise WorkflowError("DOCUMENT_ID_MISMATCH", "Document ID does not match filename", ticket_id=ticket_id)
        return document

    def _connection(self) -> sqlite3.Connection:
        if not self.database_path.exists():
            raise WorkflowError("NOT_INITIALIZED", "Workflow database does not exist; run 'tickets init'")
        try:
            return connect(self.database_path)
        except sqlite3.Error as error:
            raise WorkflowError("DATABASE_ERROR", str(error)) from error

    def _write_workflow_gitignore(self) -> None:
        path = self.workflow_path / ".gitignore"
        if not path.exists():
            path.write_text(WORKFLOW_GITIGNORE, encoding="utf-8")

    def _ticket_row(self, connection: sqlite3.Connection, ticket_id: str) -> sqlite3.Row:
        self._validate_id(ticket_id)
        row = connection.execute("SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise WorkflowError("TICKET_NOT_FOUND", f"Ticket not found: {ticket_id}", ticket_id=ticket_id)
        return row

    def _require_status(
        self,
        connection: sqlite3.Connection,
        ticket_id: str,
        expected: str,
        target: str,
    ) -> sqlite3.Row:
        ticket = self._ticket_row(connection, ticket_id)
        if ticket["status"] != expected:
            self._invalid_transition(ticket_id, ticket["status"], target)
        return ticket

    @staticmethod
    def _invalid_transition(ticket_id: str, current: str, target: str) -> None:
        raise WorkflowError(
            "INVALID_TRANSITION",
            f"Cannot move {ticket_id} from {current} to {target}",
            ticket_id=ticket_id,
            current_status=current,
            target_status=target,
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        ticket_id: str,
        actor: str,
        event: str,
        message: str | None,
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO ticket_events (ticket_id, agent, event, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, actor, event, message, timestamp),
        )

    @staticmethod
    def _require_actor(actor: str, allowed: set[str]) -> None:
        if actor not in ACTORS:
            raise WorkflowError("INVALID_ACTOR", f"Invalid actor: {actor}")
        if actor not in allowed:
            raise WorkflowError("ACTOR_NOT_AUTHORIZED", f"Actor {actor} is not authorized for this action")

    @staticmethod
    def _require_message(message: str | None, command: str) -> None:
        if not message or not message.strip():
            raise WorkflowError("MESSAGE_REQUIRED", f"A non-empty message is required for {command}")

    @staticmethod
    def _validate_id(ticket_id: str) -> None:
        try:
            validate_ticket_id(ticket_id)
        except TicketDocumentError as error:
            raise WorkflowError("INVALID_TICKET_ID", str(error), ticket_id=ticket_id) from error

    @staticmethod
    def _rollback(connection: sqlite3.Connection) -> None:
        if connection.in_transaction:
            connection.execute("ROLLBACK")

    @staticmethod
    def _cleanup_created_file(
        final_path: Path | None,
        final_identity: tuple[int, int] | None,
        temporary_path: Path | None,
    ) -> None:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        if final_path is not None and final_identity is not None:
            try:
                current = final_path.stat()
                if (current.st_dev, current.st_ino) == final_identity:
                    final_path.unlink()
            except OSError:
                pass


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
