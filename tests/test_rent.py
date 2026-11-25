import io
import sqlite3

from treasurer.db import connect, init_db, is_rent_fund
from treasurer.reports import build_report
from conftest import sample


def load(client):
    client.post("/upload/giving", data={"files": (io.BytesIO(sample("planning_center_sample.csv")), "pc.csv"),
                                        "platform": "Planning Center"},
                content_type="multipart/form-data", follow_redirects=True)


def test_is_rent_fund():
    pats = ["Rent"]
    assert is_rent_fund("Rent", pats) and is_rent_fund("rent - Fall 2026", pats)
    assert is_rent_fund("Housing Rent", pats)
    assert not is_rent_fund("Parent Ministry Fund", pats)
    assert not is_rent_fund("General Fund", pats)


def test_rent_is_separate_from_giving(app, client):
    r = client.post("/upload/giving", data={"files": (io.BytesIO(sample("planning_center_sample.csv")), "pc.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"imported 3 gifts and 5 rent payments" in r.data
    conn = connect(app.config["DATABASE"])
    rep = build_report(conn, "2026-10")
    # Gifts only: 100 + 50 + 40 (the "Parent Ministry Fund" is not rent)
    assert rep["giving"]["total"] == 190.0
    assert rep["giving"]["gifts"] == 3 and rep["giving"]["donors"] == 3
    assert {f["name"] for f in rep["giving"]["by_fund"]} == {"General Fund", "Missions", "Parent Ministry Fund"}
    # Rent is its own income line
    other = {l["name"]: l["amount"] for l in rep["other_income"]}
    assert other["Student Housing Rent"] == 1800.0
    assert rep["rent"]["total"] == 1800.0
    assert rep["rent"]["payers"] == 4 and rep["rent"]["payments"] == 5
    riley = next(p for p in rep["rent"]["by_payer"] if p["name"] == "Riley Patel")
    assert riley["payments"] == 2 and riley["amount"] == 450.0
    assert rep["summary"]["income"] == 1990.0
    # Fees cover both gifts and rent; reconciliation uses the combined net
    exp = {l["name"]: l["amount"] for l in rep["expenses"]}
    assert exp["Bank & Processing Fees"] == round(13.35 + 3.60 + 3.20 + 3.60 + 1.75 + 6.83 + 6.83 + 1.46, 2)
    assert rep["giving"]["net"] == round(1990.0 - exp["Bank & Processing Fees"], 2)

    page = client.get("/reports/2026-10").data.decode()
    assert "Student housing rent" in page and "Riley Patel" in page
    assert "Rent" in client.get("/donations?kind=rent").data.decode()


def test_rent_check_deposit_counts(app, client):
    load(client)
    conn = connect(app.config["DATABASE"])
    conn.execute("INSERT INTO transactions(date, amount, description, category, fingerprint) "
                 "VALUES ('2026-10-05', 450, 'MOBILE DEPOSIT', 'Student Housing Rent', 'x1')")
    conn.commit()
    rep = build_report(conn, "2026-10")
    assert rep["rent"]["total"] == 2250.0 and rep["rent"]["online_total"] == 1800.0


def test_changing_rent_funds_reclassifies(app, client):
    load(client)
    client.post("/settings", data={"action": "rent_funds", "rent_funds": "Rent, Missions"})
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT kind FROM donations WHERE fund='Missions'").fetchone()[0] == "rent"
    client.post("/settings", data={"action": "rent_funds", "rent_funds": "Rent"})
    assert conn.execute("SELECT kind FROM donations WHERE fund='Missions'").fetchone()[0] == "gift"


def test_upgrade_existing_database(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE donations (id INTEGER PRIMARY KEY, date TEXT, gross REAL, fee REAL, net REAL,
            donor TEXT, fund TEXT, method TEXT, platform TEXT, import_id INTEGER, fingerprint TEXT UNIQUE);
        INSERT INTO donations VALUES (1,'2026-09-01',450,0,450,'A','Rent',NULL,NULL,NULL,'f1');
        INSERT INTO donations VALUES (2,'2026-09-02',20,0,20,'B','General Fund',NULL,NULL,NULL,'f2');
    """)
    old.commit()
    old.close()
    conn = connect(path)
    init_db(conn)
    assert dict(conn.execute("SELECT fund, kind FROM donations").fetchall()) == {"Rent": "rent", "General Fund": "gift"}
    assert conn.execute("SELECT kind FROM categories WHERE name='Student Housing Rent'").fetchone()[0] == "income"
