# -*- coding: utf-8 -*-
"""Самопроверка импорта. Каждый тест — это сценарий, который реально случается
при работе с выгрузками кредитора, а не проверка синтаксиса.

Запуск:  py test_importer.py
Прогон ничего не требует, кроме стандартной библиотеки.
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from importer import connect, init_db, import_file, case_activity  # noqa: E402

HEADER = "loan_number,borrower_name,principal,rate,maturity_date,status\n"
PASSED, FAILED = 0, 0


def check(name, condition, detail=""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def write_csv(tmp, name, body):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(HEADER + body)
    return p


def run():
    tmp = tempfile.mkdtemp(prefix="loantape_")
    db = os.path.join(tmp, "t.db")
    conn = connect(db)
    init_db(conn)
    conn.execute("INSERT INTO lenders (lender_id, name) VALUES (1, 'Acme Test Lender')")
    conn.execute("INSERT INTO lenders (lender_id, name) VALUES (2, 'Second Lender')")
    conn.commit()

    print("\n1. Первый импорт создаёт кредиты")
    f1 = write_csv(tmp, "tape_day1.csv",
                   "10041,Ivanov A,250000,9.49,2027-03-01,current\n"
                   "10042,Petrova B,180000,8.99,2026-11-15,current\n")
    s1 = import_file(conn, 1, f1, verbose=False)
    check("создано два кредита", s1["inserted"] == 2, s1)
    check("исправлений нет", s1["updated"] == 0)

    print("\n2. Тот же файл повторно — партия даже не создаётся")
    s2 = import_file(conn, 1, f1, verbose=False)
    check("импорт распознан как дубль файла", s2.get("skipped_identical_file") is True, s2)
    batches = conn.execute("SELECT COUNT(*) c FROM import_batches").fetchone()["c"]
    check("партия не добавилась", batches == 1, f"партий={batches}")

    print("\n3. Те же данные в другом порядке строк и с лишними пробелами")
    f3 = write_csv(tmp, "tape_day1_reordered.csv",
                   "10042, Petrova B ,180000,8.99,2026-11-15,current\n"
                   "10041,Ivanov A,250000,9.49,2027-03-01,current\n")
    s3 = import_file(conn, 1, f3, verbose=False)
    check("оба кредита признаны неизменившимися", s3["unchanged"] == 2, s3)
    check("ничего не исправлено", s3["updated"] == 0, s3)
    hist = conn.execute("SELECT COUNT(*) c FROM loan_field_history").fetchone()["c"]
    check("история пуста — шум не записан", hist == 0, f"записей={hist}")

    print("\n4. Кредитор исправил ставку — это correction, а не no-op")
    f4 = write_csv(tmp, "tape_day2.csv",
                   "10041,Ivanov A,250000,8.99,2027-03-01,current\n"
                   "10042,Petrova B,180000,8.99,2026-11-15,current\n")
    s4 = import_file(conn, 1, f4, verbose=False)
    check("одно исправление", s4["updated"] == 1, s4)
    check("второй кредит не тронут", s4["unchanged"] == 1, s4)
    h = conn.execute(
        "SELECT field, old_value, new_value FROM loan_field_history").fetchall()
    check("в историю попало ровно одно поле", len(h) == 1, [dict(x) for x in h])
    check("записано rate 9.49 -> 8.99",
          bool(h) and h[0]["field"] == "rate" and h[0]["old_value"] == "9.49"
          and h[0]["new_value"] == "8.99", [dict(x) for x in h])

    print("\n5. Битые строки уходят в карантин, импорт не падает")
    f5 = write_csv(tmp, "tape_day3.csv",
                   "10041,Ivanov A,250000,8.99,2027-03-01,current\n"
                   ",NoNumber C,1000,5,2027-01-01,current\n"
                   "10043,Sidorov D,not-a-number,7.5,2028-01-01,current\n"
                   "10044,Kim E,90000,150,2028-01-01,current\n"
                   "10045,Lee F,120000,7.25,01/02/2028,current\n"
                   "10046,Good G,75000,6.5,2029-06-30,current\n")
    s5 = import_file(conn, 1, f5, verbose=False)
    check("в карантине четыре строки", s5["quarantined"] == 4, s5)
    check("годная строка всё равно импортирована", s5["inserted"] == 1, s5)
    q = conn.execute("SELECT reason FROM quarantined_rows ORDER BY line_number").fetchall()
    reasons = [r["reason"] for r in q]
    check("причина указана для каждой", all(reasons), reasons)
    check("пустой loan_number пойман", any("loan_number" in r for r in reasons), reasons)
    check("нечисловой principal пойман", any("principal" in r for r in reasons), reasons)
    check("ставка вне диапазона поймана", any("rate вне диапазона" in r for r in reasons), reasons)
    check("дата в чужом формате поймана", any("maturity_date" in r for r in reasons), reasons)

    print("\n6. Дубль внутри одного файла фиксируется, а не молча теряется")
    f6 = write_csv(tmp, "tape_dup.csv",
                   "10050,Dup H,10000,5.0,2027-01-01,current\n"
                   "10050,Dup H,10000,5.0,2027-01-01,arrears\n")
    s6 = import_file(conn, 1, f6, verbose=False)
    check("дубль в файле замечен", s6["duplicates_in_file"] == 1, s6)
    ev = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE event_type='row.duplicate_in_file'").fetchone()["c"]
    check("событие о дубле записано", ev == 1, f"событий={ev}")
    st = conn.execute("SELECT status FROM loans WHERE lender_id=1 AND loan_number='10050'"
                      ).fetchone()["status"]
    check("победила последняя строка файла", st == "arrears", st)

    print("\n7. Изоляция арендаторов: тот же номер кредита у другого кредитора")
    f7 = write_csv(tmp, "tape_lender2.csv",
                   "10041,Different Borrower,999,1.0,2030-01-01,current\n")
    s7 = import_file(conn, 2, f7, verbose=False)
    check("создан отдельный кредит", s7["inserted"] == 1, s7)
    n = conn.execute("SELECT COUNT(*) c FROM loans WHERE loan_number='10041'").fetchone()["c"]
    check("номер существует у обоих кредиторов независимо", n == 2, f"строк={n}")
    borrower = conn.execute(
        "SELECT borrower_name FROM loans WHERE lender_id=1 AND loan_number='10041'"
    ).fetchone()["borrower_name"]
    check("данные первого кредитора не перезаписаны", borrower == "Ivanov A", borrower)

    print("\n8. Лента дела строится из журнала, а не из текущей записи")
    loan, events = case_activity(conn, 1, "10041")
    types = [e["event_type"] for e in events]
    check("есть событие создания", "loan.created" in types, types)
    check("есть событие исправления", "loan.corrected" in types, types)
    check("события упорядочены", events == sorted(events, key=lambda e: e["event_id"]))

    print("\n9. Журнал только добавляется")
    before = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    import_file(conn, 1, f4, verbose=False)   # повтор уже импортированного файла
    after = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    check("повтор файла не добавил событий и не удалил старые", after == before,
          f"было {before}, стало {after}")

    print(f"\nитого: {PASSED} прошло, {FAILED} провалено")
    conn.close()
    return FAILED == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
