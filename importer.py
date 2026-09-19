# -*- coding: utf-8 -*-
"""Идемпотентный импорт кредитного реестра с аудитом.

Задача, которую это решает: кредитор выгружает файл со списком кредитов и
присылает его снова — завтра, через неделю, иногда дважды за день, иногда с
исправленным полем, иногда с теми же данными и другим порядком строк.
Импорт обязан быть безопасным при любом числе повторов.

Три уровня проверки, от самого дешёвого к самому дорогому:

  1. Хэш файла. Тот же байт-в-байт файл — партия не создаётся вовсе.
  2. Хэш строки. Кредит есть, значимые поля не изменились — запись не трогаем,
     обновляем только отметку «видели в партии N».
  3. Сравнение по полям. Что-то изменилось — обновляем, пишем событие
     loan.corrected и по строке в историю на каждое изменившееся поле.

Проектное решение, которое стоит обсуждать отдельно: при повторной выгрузке с
исправленным полем мы НЕ делаем no-op, а записываем correction event и
перезаписываем значение. Альтернатива — считать первую выгрузку истиной и
игнорировать расхождения — проще, но означает, что исправленная кредитором
ставка никогда не доедет до системы. Для реестра, по которому ведут взыскание,
это хуже, чем лишняя запись в журнале.

Запуск:
    py importer.py demo          — создать БД, прогнать три сценария, показать журнал
    py importer.py import <lender_id> <file.csv>
"""
import csv
import hashlib
import io
import json
import os
import sqlite3

# Библиотека намеренно не трогает sys.stdout: вызывающий код сам решает,
# как выводить. Обёртка здесь ломала бы тесты, которые ставят свою.

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "loan_tape.db")

# Поля, изменение которых считается изменением кредита.
# Порядок фиксирован: он участвует в хэше строки.
SIGNIFICANT = ["borrower_name", "principal", "rate", "maturity_date", "status"]
REQUIRED = ["loan_number"]


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def row_hash(row: dict) -> str:
    """Хэш значимых полей. Пробелы по краям срезаны, порядок фиксирован,
    поэтому перестановка колонок в файле не считается изменением данных."""
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
    """Возвращает причину карантина или None. Правило: строка либо
    полностью пригодна, либо откладывается целиком — частично импортированный
    кредит хуже отсутствующего, потому что выглядит настоящим."""
    for f in REQUIRED:
        if not (row.get(f) or "").strip():
            return f"обязательное поле {f} пустое"
    principal = (row.get("principal") or "").strip()
    if principal:
        try:
            if float(principal.replace(",", "")) < 0:
                return "principal отрицательный"
        except ValueError:
            return f"principal не число: {principal!r}"
    rate = (row.get("rate") or "").strip()
    if rate:
        try:
            r = float(rate.replace("%", ""))
            if not 0 <= r <= 100:
                return f"rate вне диапазона 0..100: {rate!r}"
        except ValueError:
            return f"rate не число: {rate!r}"
    md = (row.get("maturity_date") or "").strip()
    if md and not (len(md) == 10 and md[4] == "-" and md[7] == "-"):
        return f"maturity_date не в формате YYYY-MM-DD: {md!r}"
    return None


def import_file(conn, lender_id: int, path: str, verbose=True):
    """Возвращает словарь со счётчиками. Повторный вызов на том же файле
    безопасен и ничего не меняет."""
    raw = open(path, "rb").read()
    file_hash = hashlib.sha256(raw).hexdigest()
    filename = os.path.basename(path)

    prev = conn.execute(
        "SELECT batch_id, finished_at FROM import_batches "
        "WHERE lender_id=? AND file_sha256=?", (lender_id, file_hash)).fetchone()
    if prev and prev["finished_at"]:
        if verbose:
            print(f"  файл уже импортирован партией {prev['batch_id']} — ничего не делаем")
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

        # дубль внутри одного файла: последняя строка выигрывает, факт фиксируется
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
        print(f"  партия {batch_id}: создано {stats['inserted']}, исправлено {stats['updated']},"
              f" без изменений {stats['unchanged']}, в карантине {stats['quarantined']}")
    return stats


def case_activity(conn, lender_id: int, loan_number: str):
    """Лента активности по одному кредиту — то, что видно на экране дела.
    Строится ИЗ ЖУРНАЛА, а не из текущего состояния записи."""
    loan = conn.execute("SELECT * FROM loans WHERE lender_id=? AND loan_number=?",
                        (lender_id, loan_number)).fetchone()
    if not loan:
        return None, []
    rows = conn.execute(
        "SELECT * FROM events WHERE lender_id=? AND loan_id=? ORDER BY event_id",
        (lender_id, loan["loan_id"])).fetchall()
    return loan, rows
