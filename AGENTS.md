# Shared Agent Contract

This directory coordinates work through durable tickets rather than conversational memory. Read this file, your role contract under `agents/`, and the complete canonical ticket before acting.

## Sources of Truth

- SQLite is authoritative for workflow state and event history.
- The canonical Markdown ticket under the application's `.agent/tickets/` directory is authoritative for requested behavior and acceptance criteria.
- The CLI is the only supported workflow mutation interface.
- The application repository remains separate from this workflow database.

## Required Conduct

- Use only your own logical actor name with `--agent`.
- Run every `tickets` command from the application repository root.
- Check current state with `tickets show TICKET-NNN` before working.
- Work only when the ticket state is valid for your role.
- Record concise, factual handoff notes and evidence through the appropriate transition command.
- Re-read the ticket and state after any interruption; do not rely on chat history.
- Preserve ticket IDs and canonical filenames.
- Mark work blocked when progress requires unavailable information, access, infrastructure, or an unresolved external decision.
- Ask for human input when requirements conflict, acceptance criteria are not objectively interpretable, a destructive action is necessary, credentials or permissions are unavailable, or the safe choice changes product intent.

## Prohibited Conduct

- Do not modify `.agent/workflow.db` directly or execute arbitrary SQL against it.
- Do not impersonate another role to advance a ticket.
- Do not change status by editing or moving Markdown files.
- Do not delete tickets or event history.
- Do not work around a rejected transition.
- Do not materially change requirements after work starts without blocking and obtaining project-manager or human clarification.
- Do not infer approval, test success, or implementation completion from conversational history.

## Role Contracts

- Project manager: `agents/project-manager.md`
- Developer: `agents/developer.md`
- Tester: `agents/tester.md`
- Reviewer: `agents/reviewer.md`

`human` is a valid audited event actor for manual operation. It is not an assignable work role.
