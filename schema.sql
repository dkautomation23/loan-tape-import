-- Идемпотентный импорт кредитного реестра (loan tape) с аудитом.
-- Пишется под Postgres; для мгновенного прогона совместимо с SQLite.
-- Различия, где они есть, отмечены комментарием POSTGRES.

-- ---------------------------------------------------------------------------
-- Арендаторы. Всё изолируется по lender_id, включая уникальные ключи.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lenders (
    lender_id   INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    -- POSTGRES: created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Партия импорта. Одна строка на загруженный файл.
-- file_sha256 позволяет отличить «тот же самый файл» от «файла с теми же
-- данными»: первый случай — no-op целиком, и это самая дешёвая проверка.
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
-- Кредиты. Бизнес-ключ — (lender_id, loan_number): так их называет кредитор,
-- и именно по нему строка «та же самая» при повторной выгрузке.
-- row_sha256 — хэш значимых полей, отвечает на вопрос «изменилось ли что-то».
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
-- Журнал событий, только добавление. Ничего не обновляется и не удаляется:
-- это то, что показывают аудитору, и то, из чего строится лента активности.
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
-- Карантин. Строка, которую не удалось разобрать, НЕ теряется и НЕ ломает
-- импорт: она откладывается с причиной. Импорт, падающий на 4000-й строке из
-- 5000, оставляет данные в половинном состоянии — это худший исход из всех.
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
-- История значений. Каждое изменившееся поле пишется отдельной строкой:
-- «в партии 7 ставка по кредиту 10041 стала 8.99 вместо 9.49».
-- Именно это спрашивает аудитор, и именно это неудобно доставать из jsonb.
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
