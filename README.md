# Agent Workflow

A reusable local ticket workflow for coordinating human and agent work. It uses Markdown for ticket requirements, SQLite for workflow state and event history, and one CLI for all state mutations. Each application keeps its own workflow state under `.agent/`; this engine repository contains only the executable, schema, and role contracts.

This iteration deliberately has no Herdr integration, autonomous orchestration, web UI, arbitrary SQL interface, or multi-project abstraction.

## Requirements

- Python 3.10 or newer
- No third-party packages

## Install And Initialize

Make the executable available on `PATH`, for example:

```sh
ln -s /path/to/agent-workflow/scripts/tickets ~/.local/bin/tickets
```

Then run initialization from the application repository root:

```sh
cd /path/to/application
tickets init
```

This creates `.agent/workflow.db` and `.agent/tickets/` in the application. The executable's installation directory is used only to load code and the bundled schema; it is never used as the application project directory. A generated `.agent/.gitignore` excludes the database and its SQLite sidecar files while allowing canonical ticket documents to be version-controlled:

```gitignore
workflow.db
workflow.db-shm
workflow.db-wal
```

Every `tickets` command must be run from the application repository root.

### Migrate A Legacy Project

Projects using the former top-level `data/workflow.db` and `tickets/` layout can be migrated explicitly:

```sh
cd /path/to/application
tickets migrate
```

Migration validates the legacy schema and ticket documents, installs the new `.agent/` directory atomically, and retains the legacy files as a backup. It refuses to overwrite an existing `.agent/` directory or migrate mismatched database and document state.

## Authority Model

- SQLite is authoritative for status, assignment, priority, attempts, timestamps, blocking state, and event history.
- `.agent/tickets/TICKET-NNN.md` is authoritative for title, objective, requirements, acceptance criteria, constraints, and dependencies.
- The CLI is the only supported way to mutate workflow state.
- Agents must not execute SQL against `workflow.db` or alter workflow state by editing Markdown.
- Ticket files use stable paths and are not moved when status changes.

### Provisional Dependency Storage

Dependencies are parsed from the current Markdown document when `ready` runs. This is intentionally provisional for manual operation. It means a Markdown edit can alter the dependency graph without adding an event to SQLite.

Before automated orchestration is introduced, dependencies should move to audited structured state, likely a `ticket_dependencies` table managed through the CLI. That future change prevents agents from silently changing the workflow graph. This iteration does not add that abstraction prematurely.

## Ticket Draft Format

Create a draft outside `.agent/tickets/` without an ID:

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

The CLI validates the document, allocates the next ID, writes the canonical ticket under `.agent/tickets/`, inserts its `BACKLOG` state, and records a `CREATED` event.

```sh
tickets create --agent project-manager --file /path/to/draft.md
tickets create --agent human --priority HIGH --file /path/to/draft.md
```

Required sections are `Objective`, `Requirements`, `Acceptance Criteria`, `Constraints`, and `Dependencies`. Objective, requirements, and acceptance criteria cannot be empty. Use `None` where constraints or dependencies do not apply.

## Lifecycle

```text
BACKLOG -> READY -> DEVELOPMENT -> TESTING -> REVIEW -> DONE
                           ^           |          |
                           +-----------+----------+
                              fail or reject
```

Any nonterminal state can move to `BLOCKED`. Unblocking restores the exact prior state and assignment using `blocked_from_status`; it does not restart the lifecycle at `READY`. `DONE` tickets cannot be blocked, reopened, or deleted through this CLI.

`attempt_count` counts entries into development: initial start, test failure, and review rejection. `started_at` records the first development start. `completed_at` is set only by approval.

## Commands

Read operations:

```sh
tickets list
tickets list --status READY
tickets list --assigned-to developer
tickets show TICKET-001
tickets history TICKET-001
```

Lifecycle mutations:

```sh
tickets ready TICKET-001 --agent project-manager
tickets start TICKET-001 --agent developer
tickets submit TICKET-001 --agent developer --message "Implementation notes"
tickets test-pass TICKET-001 --agent tester --message "Test evidence"
tickets test-fail TICKET-001 --agent tester --message "Failure evidence"
tickets approve TICKET-001 --agent reviewer --message "Review notes"
tickets reject TICKET-001 --agent reviewer --message "Requested changes"
tickets block TICKET-001 --agent developer --message "Blocking reason"
tickets unblock TICKET-001 --agent project-manager --message "Resolution"
```

Messages are mandatory for `test-fail`, `reject`, and `block`. They are optional but recommended for successful handoffs.

Actors are exactly `project-manager`, `developer`, `tester`, `reviewer`, and `human`. `assigned_to` contains only the logical work roles `developer`, `tester`, and `reviewer`, never a terminal identity or person.

### JSON Output

Put `--json` before the subcommand:

```sh
tickets --json list --status READY
tickets --json show TICKET-001
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
