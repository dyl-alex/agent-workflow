# Agent Workflow

A small local ticket workflow for coordinating human and agent work. It uses Markdown for ticket requirements, SQLite for workflow state and event history, and one CLI for all state mutations.

This iteration deliberately has no Herdr integration, autonomous orchestration, web UI, arbitrary SQL interface, or multi-project abstraction.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Initialize

Run commands from this directory:

```sh
scripts/tickets init
```

This creates `data/workflow.db`. The database and its SQLite sidecar files are ignored by Git. Ticket documents, schema, source, tests, and agent contracts can be version-controlled independently.

## Authority Model

- SQLite is authoritative for status, assignment, priority, attempts, timestamps, blocking state, and event history.
- `tickets/TICKET-NNN.md` is authoritative for title, objective, requirements, acceptance criteria, constraints, and dependencies.
- The CLI is the only supported way to mutate workflow state.
- Agents must not execute SQL against `workflow.db` or alter workflow state by editing Markdown.
- Ticket files use stable paths and are not moved when status changes.

### Provisional Dependency Storage

Dependencies are parsed from the current Markdown document when `ready` runs. This is intentionally provisional for manual operation. It means a Markdown edit can alter the dependency graph without adding an event to SQLite.

Before automated orchestration is introduced, dependencies should move to audited structured state, likely a `ticket_dependencies` table managed through the CLI. That future change prevents agents from silently changing the workflow graph. This iteration does not add that abstraction prematurely.

## Ticket Draft Format

Create a draft outside `tickets/` without an ID:

```markdown
# Add Login Form

## Objective

Implement the initial login interface.

## Requirements

- Email input
- Password input
- Submit button
- API error handling

## Acceptance Criteria

- Valid credentials work.
- Invalid credentials show an error.
- Successful authentication redirects appropriately.
- Existing tests pass.
- New behavior has appropriate automated tests.

## Constraints

- Follow existing authentication conventions.

## Dependencies

None
```

Dependencies must be `None` or a bullet list containing only ticket IDs:

```markdown
## Dependencies

- TICKET-001
- TICKET-004
```

The CLI validates the document, allocates the next ID, writes the canonical ticket under `tickets/`, inserts its `BACKLOG` state, and records a `CREATED` event.

```sh
scripts/tickets create --agent project-manager --file /path/to/draft.md
scripts/tickets create --agent human --priority HIGH --file /path/to/draft.md
```

Required sections are `Objective`, `Requirements`, `Acceptance Criteria`, `Constraints`, and `Dependencies`. Objective, requirements, and acceptance criteria cannot be empty. Use `None` where constraints or dependencies do not apply.

## Lifecycle

```text
BACKLOG -> READY -> DEVELOPMENT -> TESTING -> REVIEW -> DONE
                           ^           |          |
                           +-----------+----------+
                              fail or reject
```

Any nonterminal state can move to `BLOCKED`. Unblocking restores the exact prior state using `blocked_from_status`. `DONE` tickets cannot be blocked, reopened, or deleted through this CLI.

`attempt_count` counts entries into development: initial start, test failure, and review rejection. `started_at` records the first development start. `completed_at` is set only by approval.

## Commands

Read operations:

```sh
scripts/tickets list
scripts/tickets list --status READY
scripts/tickets list --assigned-to developer
scripts/tickets show TICKET-001
scripts/tickets history TICKET-001
```

Lifecycle mutations:

```sh
scripts/tickets ready TICKET-001 --agent project-manager
scripts/tickets start TICKET-001 --agent developer
scripts/tickets submit TICKET-001 --agent developer --message "Implementation notes"
scripts/tickets test-pass TICKET-001 --agent tester --message "Test evidence"
scripts/tickets test-fail TICKET-001 --agent tester --message "Failure evidence"
scripts/tickets approve TICKET-001 --agent reviewer --message "Review notes"
scripts/tickets reject TICKET-001 --agent reviewer --message "Requested changes"
scripts/tickets block TICKET-001 --agent developer --message "Blocking reason"
scripts/tickets unblock TICKET-001 --agent project-manager --message "Resolution"
```

Messages are mandatory for `test-fail`, `reject`, and `block`. They are optional but recommended for successful handoffs.

Actors are exactly `project-manager`, `developer`, `tester`, `reviewer`, and `human`. `assigned_to` contains only the logical work roles `developer`, `tester`, and `reviewer`, never a terminal identity or person.

### JSON Output

Put `--json` before the subcommand:

```sh
scripts/tickets --json list --status READY
scripts/tickets --json show TICKET-001
```

Success exits with status 0. Workflow and validation failures exit with status 1. CLI syntax failures exit with status 2. JSON failures have this shape:

```json
{
  "ok": false,
  "error": {
    "code": "INVALID_TRANSITION",
    "message": "Cannot move TICKET-001 from TESTING to DONE",
    "ticket_id": "TICKET-001",
    "current_status": "TESTING",
    "target_status": "DONE"
  }
}
```

## Transaction Model

Every state transition begins an immediate SQLite transaction, validates current state and actor authorization, updates the ticket, inserts an event, and commits. Any failure rolls back the update and event together. Foreign keys and a finite busy timeout are enabled for every connection.

Creation spans SQLite and the filesystem, which cannot participate in one truly atomic transaction. The implementation writes a same-directory temporary file, flushes it, atomically renames it, and commits SQLite while cleaning up on reported failures. A process or machine crash at the narrow filesystem/database boundary can still leave an orphan file. Normal state transitions touch only SQLite and do not have this limitation.

## Tests

```sh
python3 -m unittest -v
```

Tests use isolated temporary databases and cover initialization, creation, transitions, authorization, events, failures and rejections, blocking, dependency checks, malformed IDs, JSON errors, and transaction rollback.
