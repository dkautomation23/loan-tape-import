# -*- coding: utf-8 -*-
"""Idempotent loan tape import with an audit trail.

The problem: a lender exports a file of loans and sends it again - tomorrow, next
week, sometimes twice in one day, sometimes with a field corrected, sometimes the
same data in a different row order. The import has to be safe on every repeat.

Three levels of detection, cheapest first:

  1. File hash. The same file byte for byte - no batch is created at all.
  2. Row hash. The loan exists and no significant field changed - the record is
     left alone, only the "seen in batch N" marker moves.
  3. Field comparison. Something changed - update, write a loan.corrected event,
     and record one history row per changed field.

The design decision worth arguing about: on a re-export with a corrected field we
do NOT no-op. We record a correction and overwrite. Treating the first export as
truth is simpler, but it means a rate the lender fixed never reaches the system.
For a book that collections are run against, a silently stale value is worse than
an extra row in the log.

    py importer.py demo
    py importer.py import <lender_id> <file.csv>
"""
import csv
import hashlib
import io
import json
import os
import sqlite3

# This module deliberately leaves sys.stdout alone: the caller decides how to
# print. A wrapper here would break tests that install their own.

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "loan_tape.db")

# Fields whose change counts as a change to the loan.
# The order is fixed because it goes into the row hash.
SIGNIFICANT = ["borrower_name", "principal", "rate", "maturity_date", "status"]
REQUIRED = ["loan_number"]


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def row_hash(row: dict) -> str:
    """Hash over the significant fields. Values are trimmed and the order is
    fixed, so reordering columns in the file is not a data change."""
    parts = [f"{f}={(row.get(f) or '').strip()}" for f in SIGNIFICANT]
    return sha256_text("\x1f".join(parts))


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn):
    with open(os.path.join(HERE, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()


def log_event(conn, lender_id, event_type, payload, loan_id=None, batch_id=None):
    conn.execute(
        "INSERT INTO events (lender_id, loan_id, batch_id, event_type, payload) "
        "VALUES (?,?,?,?,?)",
        (lender_id, loan_id, batch_id, event_type, json.dumps(payload, ensure_ascii=False)))


def validate(row: dict, line_no: int):
    """Returns a quarantine reason or None. The rule: a row is either fully
    usable or held whole - a partially imported loan is worse than a missing
    one, because it looks real."""
    for f in REQUIRED:
        if not (row.get(f) or "").strip():
            return f"required field {f} is empty"
    principal = (row.get("principal") or "").strip()
    if principal:
        try:
            if float(principal.replace(",", "")) < 0:
                return "principal is negative"
        except ValueError:
            return f"principal is not a number: {principal!r}"
    rate = (row.get("rate") or "").strip()
    if rate:
        try:
            r = float(rate.replace("%", ""))
            if not 0 <= r <= 100:
                return f"rate outside 0..100: {rate!r}"
        except ValueError:
            return f"rate is not a number: {rate!r}"
    md = (row.get("maturity_date") or "").strip()
    if md and not (len(md) == 10 and md[4] == "-" and md[7] == "-"):
        return f"maturity_date is not YYYY-MM-DD: {md!r}"
    return None


def import_file(conn, lender_id: int, path: str, verbose=True):
    """Returns a dict of counters. Calling it again on the same file is safe
    and changes nothing."""
    raw = open(path, "rb").read()
    file_hash = hashlib.sha256(raw).hexdigest()
    filename = os.path.basename(path)

    prev = conn.execute(
        "SELECT batch_id, finished_at FROM import_batches "
        "WHERE lender_id=? AND file_sha256=?", (lender_id, file_hash)).fetchone()
    if prev and prev["finished_at"]:
        if verbose:
            print(f"  file already imported as batch {prev['batch_id']} - nothing to do")
        return {"skipped_identical_file": True, "batch_id": prev["batch_id"],
                "inserted": 0, "updated": 0, "unchanged": 0, "quarantined": 0}

    cur = conn.execute(
        "INSERT INTO import_batches (lender_id, filename, file_sha256) VALUES (?,?,?)",
        (lender_id, filename, file_hash))
    batch_id = cur.lastrowid
    log_event(conn, lender_id, "batch.started",
              {"filename": filename, "file_sha256": file_hash[:16]}, batch_id=batch_id)

    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "quarantined": 0,
             "duplicates_in_file": 0}
    seen_in_file = {}

    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v if v is not None else "") for k, v in row.items()}
        reason = validate(row, line_no)
        if reason:
            conn.execute(
                "INSERT INTO quarantined_rows (lender_id, batch_id, line_number, raw_line, reason) "
                "VALUES (?,?,?,?,?)",
                (lender_id, batch_id, line_no, json.dumps(row, ensure_ascii=False), reason))
            log_event(conn, lender_id, "row.quarantined",
                      {"line": line_no, "reason": reason}, batch_id=batch_id)
            stats["quarantined"] += 1
            continue

        loan_number = row["loan_number"].strip()
        rhash = row_hash(row)

        # duplicate inside one file: the last row wins, the fact is recorded
        if loan_number in seen_in_file:
            stats["duplicates_in_file"] += 1
            log_event(conn, lender_id, "row.duplicate_in_file",
                      {"loan_number": loan_number, "line": line_no,
                       "first_seen_line": seen_in_file[loan_number]}, batch_id=batch_id)
        seen_in_file[loan_number] = line_no

        existing = conn.execute(
            "SELECT * FROM loans WHERE lender_id=? AND loan_number=?",
            (lender_id, loan_number)).fetchone()

        if existing is None:
            cur = conn.execute(
                "INSERT INTO loans (lender_id, loan_number, borrower_name, principal, rate,"
                " maturity_date, status, row_sha256, first_seen_batch, last_seen_batch) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lender_id, loan_number, row.get("borrower_name"), row.get("principal"),
                 row.get("rate"), row.get("maturity_date"), row.get("status"),
                 rhash, batch_id, batch_id))
            loan_id = cur.lastrowid
            log_event(conn, lender_id, "loan.created",
                      {"loan_number": loan_number,
                       "values": {f: row.get(f) for f in SIGNIFICANT}},
                      loan_id=loan_id, batch_id=batch_id)
            stats["inserted"] += 1
            continue

        if existing["row_sha256"] == rhash:
            conn.execute("UPDATE loans SET last_seen_batch=? WHERE loan_id=?",
                         (batch_id, existing["loan_id"]))
            stats["unchanged"] += 1
            continue

        changed = {}
        for f in SIGNIFICANT:
            old = existing[f]
            new = (row.get(f) or "").strip() or None
            old_n = (old or "").strip() or None
            if old_n != new:
                changed[f] = {"old": old_n, "new": new}
                conn.execute(
                    "INSERT INTO loan_field_history (lender_id, loan_id, batch_id, field,"
                    " old_value, new_value) VALUES (?,?,?,?,?,?)",
                    (lender_id, existing["loan_id"], batch_id, f, old_n, new))
        conn.execute(
            "UPDATE loans SET borrower_name=?, principal=?, rate=?, maturity_date=?,"
            " status=?, row_sha256=?, last_seen_batch=?, updated_at=datetime('now') "
            "WHERE loan_id=?",
            (row.get("borrower_name"), row.get("principal"), row.get("rate"),
             row.get("maturity_date"), row.get("status"), rhash, batch_id,
             existing["loan_id"]))
        log_event(conn, lender_id, "loan.corrected",
                  {"loan_number": loan_number, "changed": changed},
                  loan_id=existing["loan_id"], batch_id=batch_id)
        stats["updated"] += 1

    conn.execute(
        "UPDATE import_batches SET row_count=?, inserted=?, updated=?, unchanged=?,"
        " quarantined=?, finished_at=datetime('now') WHERE batch_id=?",
        (stats["inserted"] + stats["updated"] + stats["unchanged"] + stats["quarantined"],
         stats["inserted"], stats["updated"], stats["unchanged"], stats["quarantined"],
         batch_id))
    log_event(conn, lender_id, "batch.finished", stats, batch_id=batch_id)
    conn.commit()
    stats["batch_id"] = batch_id
    if verbose:
        print(f"  batch {batch_id}: created {stats['inserted']}, corrected {stats['updated']},"
              f" unchanged {stats['unchanged']}, quarantined {stats['quarantined']}")
    return stats


def case_activity(conn, lender_id: int, loan_number: str):
    """Activity feed for one loan - what the case screen shows.
    Built FROM THE EVENT LOG, not from the current state of the record."""
    loan = conn.execute("SELECT * FROM loans WHERE lender_id=? AND loan_number=?",
                        (lender_id, loan_number)).fetchone()
    if not loan:
        return None, []
    rows = conn.execute(
        "SELECT * FROM events WHERE lender_id=? AND loan_id=? ORDER BY event_id",
        (lender_id, loan["loan_id"])).fetchall()
    return loan, rows
