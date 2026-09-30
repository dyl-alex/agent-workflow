from __future__ import annotations

import contextlib
import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow.cli import main
from workflow.service import WorkflowError, WorkflowService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def draft(title: str = "Example", dependencies: str = "None") -> str:
    return f"""# {title}

## Objective

Implement the requested behavior.

## Requirements

- Add the behavior.

## Acceptance Criteria

- The behavior is verified.

## Constraints

None

## Dependencies

{dependencies}
"""


class WorkflowTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "schema").mkdir()
        shutil.copyfile(PROJECT_ROOT / "schema" / "schema.sql", self.root / "schema" / "schema.sql")
        self.service = WorkflowService(self.root, self.root / "schema" / "schema.sql")
        self.service.init()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_draft(self, content: str | None = None, name: str = "draft.md") -> Path:
        path = self.root / name
        path.write_text(content or draft(), encoding="utf-8")
        return path

    def create(self, content: str | None = None) -> dict:
        return self.service.create(self.write_draft(content), "project-manager")

    def move_to_testing(self, ticket_id: str) -> None:
        self.service.ready(ticket_id, "project-manager")
        self.service.start(ticket_id, "developer")
        self.service.submit(ticket_id, "developer", "Implemented")

    def move_to_review(self, ticket_id: str) -> None:
        self.move_to_testing(ticket_id)
        self.service.test_pass(ticket_id, "tester", "All checks passed")

    def test_initialization_is_idempotent(self) -> None:
        self.service.init()
        connection = sqlite3.connect(self.service.database_path)
        try:
            self.assertEqual(connection.execute("SELECT version FROM schema_version").fetchall(), [(1,)])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue({"tickets", "ticket_events", "schema_version"} <= tables)
        finally:
            connection.close()

    def test_creation_allocates_id_writes_document_and_event(self) -> None:
        ticket = self.create()
        self.assertEqual(ticket["ticket_id"], "TICKET-001")
        self.assertEqual(ticket["status"], "BACKLOG")
        self.assertEqual(ticket["priority"], "NORMAL")
        self.assertTrue((self.root / ".agent" / "tickets" / "TICKET-001.md").exists())
        self.assertEqual(
            (self.root / ".agent" / ".gitignore").read_text(encoding="utf-8"),
            "workflow.db\nworkflow.db-shm\nworkflow.db-wal\n",
        )
        history = self.service.history("TICKET-001")
        self.assertEqual([(item["event"], item["agent"]) for item in history], [("CREATED", "project-manager")])

    def test_creation_collision_preserves_existing_document_and_rolls_back(self) -> None:
        existing = self.root / ".agent" / "tickets" / "TICKET-001.md"
        existing.write_text("do not replace\n", encoding="utf-8")
        with self.assertRaises(WorkflowError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "DOCUMENT_EXISTS")
        self.assertEqual(existing.read_text(encoding="utf-8"), "do not replace\n")
        self.assertEqual(self.service.list(), [])

    def test_whitespace_title_is_rejected_without_database_row(self) -> None:
        with self.assertRaises(WorkflowError) as caught:
            self.create(draft("   "))
        self.assertEqual(caught.exception.code, "INVALID_DOCUMENT")
        self.assertEqual(self.service.list(), [])

    def test_creation_event_failure_rolls_back_and_removes_its_document(self) -> None:
        connection = sqlite3.connect(self.service.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER reject_created_event
                   BEFORE INSERT ON ticket_events
                   WHEN NEW.event = 'CREATED'
                   BEGIN SELECT RAISE(ABORT, 'simulated creation event failure'); END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(WorkflowError) as caught:
            self.create()
        self.assertEqual(caught.exception.code, "CREATE_FAILED")
        self.assertEqual(self.service.list(), [])
        self.assertFalse((self.root / ".agent" / "tickets" / "TICKET-001.md").exists())

    def test_full_valid_lifecycle(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.service.ready(ticket_id, "project-manager")
        started = self.service.start(ticket_id, "developer")
        self.assertEqual(started["attempt_count"], 1)
        self.assertIsNotNone(started["started_at"])
        self.assertEqual(self.service.submit(ticket_id, "developer")["assigned_to"], "tester")
        self.assertEqual(self.service.test_pass(ticket_id, "tester")["assigned_to"], "reviewer")
        done = self.service.approve(ticket_id, "reviewer")
        self.assertEqual(done["status"], "DONE")
        self.assertIsNone(done["assigned_to"])
        self.assertIsNotNone(done["completed_at"])
        self.assertEqual(len(self.service.history(ticket_id)), 6)

    def test_invalid_transition_changes_nothing(self) -> None:
        ticket_id = self.create()["ticket_id"]
        with self.assertRaises(WorkflowError) as caught:
            self.service.start(ticket_id, "developer")
        self.assertEqual(caught.exception.code, "INVALID_TRANSITION")
        self.assertEqual(caught.exception.details["target_status"], "DEVELOPMENT")
        self.assertEqual(self.service.get(ticket_id)["status"], "BACKLOG")
        self.assertEqual(len(self.service.history(ticket_id)), 1)

    def test_transition_response_does_not_depend_on_document_after_ready(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.service.ready(ticket_id, "project-manager")
        (self.root / ".agent" / "tickets" / f"{ticket_id}.md").unlink()
        result = self.service.start(ticket_id, "developer")
        self.assertEqual(result["status"], "DEVELOPMENT")
        self.assertEqual(self.service.history(ticket_id)[-1]["event"], "STARTED")

    def test_role_authorization_is_enforced(self) -> None:
        ticket_id = self.create()["ticket_id"]
        with self.assertRaises(WorkflowError) as caught:
            self.service.ready(ticket_id, "developer")
        self.assertEqual(caught.exception.code, "ACTOR_NOT_AUTHORIZED")
        with self.assertRaises(WorkflowError) as caught:
            self.service.start(ticket_id, "intruder")
        self.assertEqual(caught.exception.code, "INVALID_ACTOR")

    def test_test_failure_returns_to_development(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.move_to_testing(ticket_id)
        ticket = self.service.test_fail(ticket_id, "tester", "Expected output was absent")
        self.assertEqual(ticket["status"], "DEVELOPMENT")
        self.assertEqual(ticket["assigned_to"], "developer")
        self.assertEqual(ticket["attempt_count"], 2)
        self.assertEqual(self.service.history(ticket_id)[-1]["event"], "TEST_FAILED")

    def test_review_rejection_returns_to_development(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.move_to_review(ticket_id)
        ticket = self.service.reject(ticket_id, "reviewer", "Input is not validated")
        self.assertEqual(ticket["status"], "DEVELOPMENT")
        self.assertEqual(ticket["attempt_count"], 2)
        self.assertEqual(self.service.history(ticket_id)[-1]["event"], "CHANGES_REQUESTED")

    def test_failure_commands_require_messages(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.move_to_testing(ticket_id)
        with self.assertRaises(WorkflowError) as caught:
            self.service.test_fail(ticket_id, "tester", " ")
        self.assertEqual(caught.exception.code, "MESSAGE_REQUIRED")

    def test_blocking_immediately_restores_every_previous_state(self) -> None:
        actors = {
            "BACKLOG": "project-manager",
            "READY": "project-manager",
            "DEVELOPMENT": "developer",
            "TESTING": "tester",
            "REVIEW": "reviewer",
        }
        for target_status, actor in actors.items():
            with self.subTest(target_status=target_status):
                ticket_id = self.create(draft(f"Blocked from {target_status}"))["ticket_id"]
                if target_status != "BACKLOG":
                    self.service.ready(ticket_id, "project-manager")
                if target_status in {"DEVELOPMENT", "TESTING", "REVIEW"}:
                    self.service.start(ticket_id, "developer")
                if target_status in {"TESTING", "REVIEW"}:
                    self.service.submit(ticket_id, "developer")
                if target_status == "REVIEW":
                    self.service.test_pass(ticket_id, "tester")

                before = self.service._state(ticket_id)
                blocked = self.service.block(ticket_id, actor, "Temporarily unavailable")
                self.assertEqual(blocked["status"], "BLOCKED")
                self.assertEqual(blocked["blocked_from_status"], target_status)
                self.assertEqual(blocked["assigned_to"], before["assigned_to"])

                restored = self.service.unblock(ticket_id, "human", "Available again")
                self.assertEqual(restored, before)
                self.assertEqual(
                    [event["event"] for event in self.service.history(ticket_id)[-2:]],
                    ["BLOCKED", "UNBLOCKED"],
                )

    def test_done_ticket_cannot_be_blocked_or_reopened(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.move_to_review(ticket_id)
        self.service.approve(ticket_id, "reviewer")
        with self.assertRaises(WorkflowError):
            self.service.block(ticket_id, "human", "No")
        with self.assertRaises(WorkflowError):
            self.service.start(ticket_id, "developer")

    def test_malformed_ticket_ids_are_rejected(self) -> None:
        for malformed in ("1", "TICKET-1", "TICKET-001-x", "ticket-001", "../TICKET-001"):
            with self.subTest(malformed=malformed):
                with self.assertRaises(WorkflowError) as caught:
                    self.service.get(malformed)
                self.assertEqual(caught.exception.code, "INVALID_TICKET_ID")

    def test_invalid_document_is_rejected_without_database_row(self) -> None:
        path = self.write_draft("# Missing Sections\n")
        with self.assertRaises(WorkflowError) as caught:
            self.service.create(path, "project-manager")
        self.assertEqual(caught.exception.code, "INVALID_DOCUMENT")
        self.assertEqual(self.service.list(), [])

    def test_dependency_must_exist_and_be_done(self) -> None:
        dependent = self.create(draft("Dependent", "- TICKET-999"))["ticket_id"]
        with self.assertRaises(WorkflowError) as caught:
            self.service.ready(dependent, "project-manager")
        self.assertEqual(caught.exception.code, "UNKNOWN_DEPENDENCY")

        dependency = self.create(draft("Dependency"))["ticket_id"]
        document_path = self.root / ".agent" / "tickets" / f"{dependent}.md"
        document_path.write_text(
            document_path.read_text(encoding="utf-8").replace("- TICKET-999", f"- {dependency}"),
            encoding="utf-8",
        )
        with self.assertRaises(WorkflowError) as caught:
            self.service.ready(dependent, "project-manager")
        self.assertEqual(caught.exception.code, "DEPENDENCY_NOT_DONE")

        self.move_to_review(dependency)
        self.service.approve(dependency, "reviewer")
        self.assertEqual(self.service.ready(dependent, "project-manager")["status"], "READY")

    def test_event_failure_rolls_back_state_transition(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.move_to_testing(ticket_id)
        connection = sqlite3.connect(self.service.database_path)
        try:
            connection.execute(
                """CREATE TRIGGER reject_test_pass
                   BEFORE INSERT ON ticket_events
                   WHEN NEW.event = 'TEST_PASSED'
                   BEGIN SELECT RAISE(ABORT, 'simulated event failure'); END"""
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(sqlite3.IntegrityError):
            self.service.test_pass(ticket_id, "tester", "Would otherwise pass")
        self.assertEqual(self.service.get(ticket_id)["status"], "TESTING")
        self.assertNotIn("TEST_PASSED", [event["event"] for event in self.service.history(ticket_id)])

    def test_json_cli_errors_are_machine_readable(self) -> None:
        stderr = io.StringIO()
        with working_directory(self.root), contextlib.redirect_stderr(stderr):
            result = main(["--json", "show", "bad-id"], schema_path=self.service.schema_path)
        self.assertEqual(result, 1)
        self.assertIn('"code": "INVALID_TICKET_ID"', stderr.getvalue())

    def test_json_cli_usage_errors_are_machine_readable(self) -> None:
        stderr = io.StringIO()
        with working_directory(self.root), contextlib.redirect_stderr(stderr):
            result = main(["--json", "show"], schema_path=self.service.schema_path)
        self.assertEqual(result, 2)
        self.assertIn('"code": "USAGE_ERROR"', stderr.getvalue())

    def test_list_filters_status_and_assignee(self) -> None:
        first = self.create()["ticket_id"]
        second = self.service.create(self.write_draft(draft("Second"), "second.md"), "human")["ticket_id"]
        self.service.ready(first, "project-manager")
        self.service.start(first, "developer")
        self.assertEqual([item["ticket_id"] for item in self.service.list("DEVELOPMENT")], [first])
        self.assertEqual([item["ticket_id"] for item in self.service.list(assigned_to="developer")], [first])
        self.assertEqual(self.service.get(second)["status"], "BACKLOG")

    def test_migrate_legacy_state_into_agent_directory_without_deleting_source(self) -> None:
        ticket_id = self.create()["ticket_id"]
        self.service.ready(ticket_id, "project-manager")
        legacy_data = self.root / "data"
        legacy_tickets = self.root / "tickets"
        legacy_data.mkdir()
        shutil.move(self.service.database_path, legacy_data / "workflow.db")
        shutil.move(self.service.tickets_path, legacy_tickets)
        (self.service.workflow_path / ".gitignore").unlink()
        self.service.workflow_path.rmdir()

        result = self.service.migrate_legacy()

        self.assertEqual(result["ticket_count"], 1)
        self.assertTrue((legacy_data / "workflow.db").exists())
        self.assertTrue((legacy_tickets / f"{ticket_id}.md").exists())
        self.assertTrue((self.root / ".agent" / ".gitignore").exists())
        self.assertEqual(self.service.get(ticket_id)["status"], "READY")

    def test_cli_uses_current_project_for_identical_ticket_ids(self) -> None:
        with tempfile.TemporaryDirectory() as other_directory:
            other_root = Path(other_directory)
            other_service = WorkflowService(other_root, self.service.schema_path)
            other_service.init()
            first_id = self.create()["ticket_id"]
            (other_root / "draft.md").write_text(draft("Other project"), encoding="utf-8")
            second_id = other_service.create(other_root / "draft.md", "project-manager")["ticket_id"]
            self.assertEqual(first_id, second_id)
            self.service.ready(first_id, "project-manager")

            command = [str(PROJECT_ROOT / "scripts" / "tickets"), "--json", "list"]
            first = subprocess.run(command, cwd=self.root, check=True, text=True, capture_output=True)
            second = subprocess.run(command, cwd=other_root, check=True, text=True, capture_output=True)

            self.assertIn('"status": "READY"', first.stdout)
            self.assertIn('"status": "BACKLOG"', second.stdout)

            subprocess.run(
                [str(PROJECT_ROOT / "scripts" / "tickets"), "start", first_id, "--agent", "developer"],
                cwd=self.root,
                check=True,
                text=True,
                capture_output=True,
            )
            self.assertEqual(self.service.get(first_id)["status"], "DEVELOPMENT")
            self.assertEqual(other_service.get(second_id)["status"], "BACKLOG")

    def test_cli_init_creates_workflow_state_in_current_directory(self) -> None:
        with tempfile.TemporaryDirectory() as project_directory:
            project_root = Path(project_directory)
            result = subprocess.run(
                [str(PROJECT_ROOT / "scripts" / "tickets"), "init"],
                cwd=project_root,
                check=True,
                text=True,
                capture_output=True,
            )

            expected_database = project_root / ".agent" / "workflow.db"
            self.assertTrue(expected_database.exists())
            self.assertTrue((project_root / ".agent" / "tickets").is_dir())
            self.assertTrue((project_root / ".agent" / ".gitignore").exists())
            self.assertIn(str(expected_database), result.stdout)


if __name__ == "__main__":
    unittest.main()
