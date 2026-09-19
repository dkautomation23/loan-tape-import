# -*- coding: utf-8 -*-
"""Self-checks for the import. Each test is a scenario that actually happens
when handling lender exports, not a syntax check.

Run:  py test_importer.py
Nothing beyond the standard library is required.
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

    print("\n1. The first import creates loans")
    f1 = write_csv(tmp, "tape_day1.csv",
                   "10041,Ivanov A,250000,9.49,2027-03-01,current\n"
                   "10042,Petrova B,180000,8.99,2026-11-15,current\n")
    s1 = import_file(conn, 1, f1, verbose=False)
    check("two loans created", s1["inserted"] == 2, s1)
    check("no corrections", s1["updated"] == 0)

    print("\n2. The same file again - no batch is created at all")
    s2 = import_file(conn, 1, f1, verbose=False)
    check("recognised as a duplicate file", s2.get("skipped_identical_file") is True, s2)
    batches = conn.execute("SELECT COUNT(*) c FROM import_batches").fetchone()["c"]
    check("no batch was added", batches == 1, f"batches={batches}")

    print("\n3. The same data, rows reordered, with extra whitespace")
    f3 = write_csv(tmp, "tape_day1_reordered.csv",
                   "10042, Petrova B ,180000,8.99,2026-11-15,current\n"
                   "10041,Ivanov A,250000,9.49,2027-03-01,current\n")
    s3 = import_file(conn, 1, f3, verbose=False)
    check("both loans seen as unchanged", s3["unchanged"] == 2, s3)
    check("nothing was corrected", s3["updated"] == 0, s3)
    hist = conn.execute("SELECT COUNT(*) c FROM loan_field_history").fetchone()["c"]
    check("history is empty - no noise recorded", hist == 0, f"rows={hist}")

    print("\n4. The lender corrected a rate - a correction, not a no-op")
    f4 = write_csv(tmp, "tape_day2.csv",
                   "10041,Ivanov A,250000,8.99,2027-03-01,current\n"
                   "10042,Petrova B,180000,8.99,2026-11-15,current\n")
    s4 = import_file(conn, 1, f4, verbose=False)
    check("one correction", s4["updated"] == 1, s4)
    check("the second loan untouched", s4["unchanged"] == 1, s4)
    h = conn.execute(
        "SELECT field, old_value, new_value FROM loan_field_history").fetchall()
    check("exactly one field in history", len(h) == 1, [dict(x) for x in h])
    check("rate 9.49 -> 8.99 recorded",
          bool(h) and h[0]["field"] == "rate" and h[0]["old_value"] == "9.49"
          and h[0]["new_value"] == "8.99", [dict(x) for x in h])

    print("\n5. Broken rows go to quarantine, the import does not die")
    f5 = write_csv(tmp, "tape_day3.csv",
                   "10041,Ivanov A,250000,8.99,2027-03-01,current\n"
                   ",NoNumber C,1000,5,2027-01-01,current\n"
                   "10043,Sidorov D,not-a-number,7.5,2028-01-01,current\n"
                   "10044,Kim E,90000,150,2028-01-01,current\n"
                   "10045,Lee F,120000,7.25,01/02/2028,current\n"
                   "10046,Good G,75000,6.5,2029-06-30,current\n")
    s5 = import_file(conn, 1, f5, verbose=False)
    check("four rows quarantined", s5["quarantined"] == 4, s5)
    check("the valid row still imported", s5["inserted"] == 1, s5)
    q = conn.execute("SELECT reason FROM quarantined_rows ORDER BY line_number").fetchall()
    reasons = [r["reason"] for r in q]
    check("a reason is given for each", all(reasons), reasons)
    check("empty loan_number caught", any("loan_number" in r for r in reasons), reasons)
    check("non-numeric principal caught", any("principal" in r for r in reasons), reasons)
    check("rate out of range caught", any("rate outside" in r for r in reasons), reasons)
    check("foreign date format caught", any("maturity_date" in r for r in reasons), reasons)

    print("\n6. A duplicate inside one file is recorded, not silently dropped")
    f6 = write_csv(tmp, "tape_dup.csv",
                   "10050,Dup H,10000,5.0,2027-01-01,current\n"
                   "10050,Dup H,10000,5.0,2027-01-01,arrears\n")
    s6 = import_file(conn, 1, f6, verbose=False)
    check("duplicate in file noticed", s6["duplicates_in_file"] == 1, s6)
    ev = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE event_type='row.duplicate_in_file'").fetchone()["c"]
    check("duplicate event recorded", ev == 1, f"events={ev}")
    st = conn.execute("SELECT status FROM loans WHERE lender_id=1 AND loan_number='10050'"
                      ).fetchone()["status"]
    check("the last row in the file won", st == "arrears", st)

    print("\n7. Tenant isolation: the same loan number at another lender")
    f7 = write_csv(tmp, "tape_lender2.csv",
                   "10041,Different Borrower,999,1.0,2030-01-01,current\n")
    s7 = import_file(conn, 2, f7, verbose=False)
    check("a separate loan was created", s7["inserted"] == 1, s7)
    n = conn.execute("SELECT COUNT(*) c FROM loans WHERE loan_number='10041'").fetchone()["c"]
    check("the number exists at both lenders independently", n == 2, f"rows={n}")
    borrower = conn.execute(
        "SELECT borrower_name FROM loans WHERE lender_id=1 AND loan_number='10041'"
    ).fetchone()["borrower_name"]
    check("the first lender's data was not overwritten", borrower == "Ivanov A", borrower)

    print("\n8. The case feed is built from the log, not from the current record")
    loan, events = case_activity(conn, 1, "10041")
    types = [e["event_type"] for e in events]
    check("a creation event exists", "loan.created" in types, types)
    check("a correction event exists", "loan.corrected" in types, types)
    check("events are ordered", events == sorted(events, key=lambda e: e["event_id"]))

    print("\n9. The log is append-only")
    before = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    import_file(conn, 1, f4, verbose=False)   # re-importing an already imported file
    after = conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
    check("the repeat added no events and removed none", after == before,
          f"was {before}, now {after}")

    print(f"\ntotal: {PASSED} passed, {FAILED} failed")
    conn.close()
    return FAILED == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
