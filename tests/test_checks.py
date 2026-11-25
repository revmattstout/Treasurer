import io

import pytest

from treasurer import ai, checks, mailbox
from treasurer.db import connect
from treasurer.reports import build_report
from conftest import sample
from test_mailbox import FakeIMAP, PNG, make_email

C = ai.CheckItem


@pytest.fixture
def fake_reader(monkeypatch):
    """Simulate Claude reading checks: returns whatever the test queues."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    queue = []
    monkeypatch.setattr(ai, "read_checks", lambda data, mime: ai.CheckPhoto(checks=queue.pop(0)))
    return queue


def upload_bank(client):
    client.post("/upload/bank", data={"files": (io.BytesIO(sample("bank_statement_sample.csv")), "b.csv")},
                content_type="multipart/form-data")


def add(app, conn, received, **kw):
    return checks.add_batch(conn, app.config["RECEIPTS_DIR"], "checks.jpg", PNG, received, "uploaded", **kw)


def test_batch_matches_one_deposit_and_names_donors(app, client, fake_reader):
    upload_bank(client)
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="Mary Smith", amount=200, date="2026-09-07", check_number="1041", memo="Wesley"),
                        C(payer="First UMC Valdosta", amount=150, date="2026-09-08", check_number="88", memo=None)])
    r = add(app, conn, "2026-09-09")
    assert r == {"batch_id": r["batch_id"], "checks": 2, "matched": 2, "error": None}
    tx = conn.execute("SELECT DISTINCT t.description, t.amount FROM checks c "
                      "JOIN transactions t ON t.id=c.transaction_id").fetchall()
    assert [tuple(t) for t in tx] == [("MOBILE DEPOSIT", 350.0)]
    rep = build_report(conn, "2026-09")
    assert {d["name"]: d["amount"] for d in rep["checks"]["by_donor"]} == {"Mary Smith": 200.0,
                                                                           "First UMC Valdosta": 150.0}
    assert rep["checks"]["gift_total"] == 350.0 and rep["checks"]["donor_count"] == 2
    other = {l["name"]: l["amount"] for l in rep["other_income"]}
    assert other["Donations - Cash & Check"] == 350.0  # same total, now with names
    page = client.get("/reports/2026-09").data.decode()
    assert "Gifts by check" in page and "Mary Smith" in page
    assert "Mary Smith" in client.get("/reports/2026-09.csv").data.decode()


def test_mixed_rent_and_gift_deposit_is_split(app, client, fake_reader):
    upload_bank(client)
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="Riley Patel", amount=400, date="2026-10-18", check_number="2001", memo="October rent"),
                        C(payer="Ann Lee", amount=25, date="2026-10-19", check_number="310", memo=None)])
    add(app, conn, "2026-10-20")
    rep = build_report(conn, "2026-10")
    other = {l["name"]: l["amount"] for l in rep["other_income"]}
    assert other["Student Housing Rent"] == 400.0
    assert other["Donations - Cash & Check"] == 25.0
    assert rep["summary"]["income"] == 425.0  # total unchanged, just split correctly
    riley = next(p for p in rep["rent"]["by_payer"] if p["name"] == "Riley Patel")
    assert riley["how"] == "check" and riley["amount"] == 400.0
    assert [d["name"] for d in rep["checks"]["by_donor"]] == ["Ann Lee"]


def test_photo_before_statement_matches_on_import(app, client, fake_reader):
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="Bob Jones", amount=425, date="2026-10-19", check_number="5", memo=None)])
    assert add(app, conn, "2026-10-20")["matched"] == 0
    r = client.post("/upload/bank", data={"files": (io.BytesIO(sample("bank_statement_sample.csv")), "b.csv")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"1 checks matched to deposits" in r.data
    assert conn.execute("SELECT transaction_id FROM checks").fetchone()[0] is not None


def test_does_not_split_deposit_in_other_category(app, client, fake_reader):
    upload_bank(client)
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="South Georgia Conference", amount=1500, date="2026-09-15", check_number="9", memo=None)])
    add(app, conn, "2026-09-17")
    rep = build_report(conn, "2026-09")
    other = {l["name"]: l["amount"] for l in rep["other_income"]}
    assert other["Church & Conference Support"] == 1500.0
    assert "Donations - Cash & Check" not in other or other["Donations - Cash & Check"] == 350.0


def test_edit_add_and_manual_match(app, client, fake_reader):
    upload_bank(client)
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="Mary Smyth", amount=20, date=None, check_number=None, memo=None)])
    b = add(app, conn, "2026-09-09")
    assert b["matched"] == 0  # misread amount: no $20 deposit
    cid = conn.execute("SELECT id FROM checks").fetchone()[0]
    client.post(f"/checks/{cid}", data={"payer": "Mary Smith", "amount": "200.00", "kind": "gift"})
    client.post(f"/checks/batch/{b['batch_id']}/add", data={"payer": "Tom Ray", "amount": "150", "kind": "gift"})
    # Saving re-runs matching per check; two checks = 350 deposit isn't found per-check, so match by hand
    tx = conn.execute("SELECT id FROM transactions WHERE amount=350").fetchone()[0]
    client.post(f"/checks/batch/{b['batch_id']}/match", data={"transaction_id": tx})
    assert {r[0] for r in conn.execute("SELECT transaction_id FROM checks")} == {tx}
    page = client.get("/checks").data.decode()
    assert "Mary Smith" in page and "✓ matched" in page
    assert "2 checks" in client.get("/transactions?q=MOBILE").data.decode()


def test_email_with_deposit_subject(app, fake_reader, monkeypatch):
    monkeypatch.setenv("TREASURER_EMAIL_ADDRESS", "treasurer@example.com")
    monkeypatch.setenv("TREASURER_EMAIL_APP_PASSWORD", "x")
    monkeypatch.setenv("TREASURER_EMAIL_ALLOWED_SENDERS", "matt@gmail.com")
    assert mailbox.classify("IMG_1.jpg", "Deposit today", b"") == "check"
    assert mailbox.classify("IMG_1.jpg", "checks for the bank", b"") == "check"
    assert mailbox.classify("scan.pdf", "October checking statement", b"") == "bank"
    conn = connect(app.config["DATABASE"])
    fake_reader.append([C(payer="Mary Smith", amount=60, date="2026-11-01", check_number="1", memo=None)])
    msgs = [make_email("matt@gmail.com", "Deposit", [("IMG_2.png", PNG, "image", "png")])]
    s = mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=FakeIMAP(msgs))
    assert s.checks == 1 and s.receipts == 0
    assert conn.execute("SELECT payer FROM checks").fetchone()[0] == "Mary Smith"
    assert "photo of checks, 1 check read" in conn.execute("SELECT detail FROM email_log").fetchone()[0]


def test_receipt_that_is_really_a_check_is_rerouted(app, client, fake_reader, monkeypatch):
    monkeypatch.setattr(ai, "read_receipt", lambda data, mime, cats: ai.ReceiptInfo(
        document_type="check", vendor=None, date=None, total=None, category=None, purpose=None))
    fake_reader.append([C(payer="Ann Lee", amount=25, date=None, check_number=None, memo=None)])
    r = client.post("/upload/receipts", data={"files": (io.BytesIO(PNG), "photo.png")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"looks like a photo of checks" in r.data
    conn = connect(app.config["DATABASE"])
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert conn.execute("SELECT payer FROM checks").fetchone()[0] == "Ann Lee"


def test_without_claude_checks_entered_by_hand(app, client):
    r = client.post("/upload/checks", data={"files": (io.BytesIO(PNG), "c.png"), "deposited": "2026-09-09"},
                    content_type="multipart/form-data", follow_redirects=True)
    assert b"read 0 check(s)" in r.data and b"enter the checks by hand" in r.data
