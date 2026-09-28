PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY
        CHECK (
            substr(ticket_id, 1, 7) = 'TICKET-'
            AND length(ticket_id) >= 10
            AND substr(ticket_id, 8) NOT GLOB '*[^0-9]*'
        ),
    status TEXT NOT NULL
        CHECK (status IN (
            'BACKLOG', 'READY', 'DEVELOPMENT', 'TESTING',
            'REVIEW', 'DONE', 'BLOCKED'
        )),
    assigned_to TEXT
        CHECK (
            assigned_to IS NULL OR assigned_to IN (
                'project-manager', 'developer', 'tester', 'reviewer'
            )
        ),
    priority TEXT NOT NULL DEFAULT 'NORMAL'
        CHECK (priority IN ('LOW', 'NORMAL', 'HIGH', 'URGENT')),
    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),
    blocked_from_status TEXT
        CHECK (
            blocked_from_status IS NULL OR blocked_from_status IN (
                'BACKLOG', 'READY', 'DEVELOPMENT', 'TESTING', 'REVIEW'
            )
        ),
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    CHECK (
        (status = 'BLOCKED' AND blocked_from_status IS NOT NULL)
        OR (status <> 'BLOCKED' AND blocked_from_status IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS ticket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL REFERENCES tickets(ticket_id),
    agent TEXT NOT NULL
        CHECK (agent IN (
            'project-manager', 'developer', 'tester', 'reviewer', 'human'
        )),
    event TEXT NOT NULL,
    message TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ticket_events_ticket_id_id
    ON ticket_events(ticket_id, id);
