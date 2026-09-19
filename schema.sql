-- Idempotent loan tape import with an audit trail.
-- Targets Postgres; runs unchanged on SQLite so the tests need no server.
-- Where the two differ, the Postgres form is noted with a POSTGRES comment.

-- ---------------------------------------------------------------------------
-- Tenants. Everything is isolated by lender_id, including the unique keys.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lenders (
    lender_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    -- POSTGRES: created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- One import batch per uploaded file.
-- file_sha256 separates "the same file" from "a file with the same data":
-- the first case is a whole-batch no-op, and it is the cheapest check there is.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id    INTEGER NOT NULL REFERENCES lenders(lender_id),
    filename     TEXT    NOT NULL,
    file_sha256  TEXT    NOT NULL,
    row_count    INTEGER NOT NULL DEFAULT 0,
    inserted     INTEGER NOT NULL DEFAULT 0,
    updated      INTEGER NOT NULL DEFAULT 0,
    unchanged    INTEGER NOT NULL DEFAULT 0,
    quarantined  INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    UNIQUE (lender_id, file_sha256)
);

-- ---------------------------------------------------------------------------
-- Loans. The business key is (lender_id, loan_number): that is what the lender
-- calls the loan, and it is what makes a row "the same row" on a re-export.
-- row_sha256 hashes the significant fields and answers "did anything change".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loans (
    loan_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id      INTEGER NOT NULL REFERENCES lenders(lender_id),
    loan_number    TEXT    NOT NULL,
    borrower_name  TEXT,
    principal      TEXT,          -- POSTGRES: numeric(14,2)
    rate           TEXT,          -- POSTGRES: numeric(6,4)
    maturity_date  TEXT,          -- POSTGRES: date
    status         TEXT,
    row_sha256     TEXT    NOT NULL,
    first_seen_batch INTEGER NOT NULL REFERENCES import_batches(batch_id),
    last_seen_batch  INTEGER NOT NULL REFERENCES import_batches(batch_id),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lender_id, loan_number)
);

-- ---------------------------------------------------------------------------
-- Append-only event log. Nothing here is updated or deleted: this is what an
-- auditor is shown, and what the activity feed is built from.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id   INTEGER NOT NULL REFERENCES lenders(lender_id),
    loan_id     INTEGER REFERENCES loans(loan_id),
    batch_id    INTEGER REFERENCES import_batches(batch_id),
    event_type  TEXT    NOT NULL,   -- loan.created | loan.corrected | loan.unchanged
                                    -- | row.quarantined | batch.started | batch.finished
    payload     TEXT    NOT NULL,   -- POSTGRES: jsonb
    occurred_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_loan   ON events (lender_id, loan_id, event_id);
CREATE INDEX IF NOT EXISTS idx_events_batch  ON events (batch_id, event_id);

-- ---------------------------------------------------------------------------
-- Quarantine. A row that will not parse is NOT lost and does NOT break the
-- import: it is held with a reason. An import that dies on row 4000 of 5000
-- leaves the data half-loaded, which is the worst outcome available.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantined_rows (
    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id     INTEGER NOT NULL REFERENCES lenders(lender_id),
    batch_id      INTEGER NOT NULL REFERENCES import_batches(batch_id),
    line_number   INTEGER NOT NULL,
    raw_line      TEXT    NOT NULL,
    reason        TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------------
-- Field history. Every changed field gets its own row: "in batch 7 the rate on
-- loan 10041 became 8.99 instead of 9.49". That is exactly what an auditor asks
-- for, and exactly what is awkward to dig back out of jsonb.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS loan_field_history (
    history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    lender_id   INTEGER NOT NULL REFERENCES lenders(lender_id),
    loan_id     INTEGER NOT NULL REFERENCES loans(loan_id),
    batch_id    INTEGER NOT NULL REFERENCES import_batches(batch_id),
    field       TEXT    NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    changed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_history_loan ON loan_field_history (lender_id, loan_id, history_id);
