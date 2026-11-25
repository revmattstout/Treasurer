import io

from treasurer.db import connect
from conftest import sample


def upload(client, url, name, data, **form):
    form["files"] = (io.BytesIO(data), name)
    return client.post(url, data=form, content_type="multipart/form-data", follow_redirects=True)


def load_samples(client):
    upload(client, "/upload/bank", "bank.csv", sample("bank_statement_sample.csv"), account="Checking")
    upload(client, "/upload/giving", "gifts.csv", sample("giving_sample_tithely.csv"), platform="Tithe.ly")


def test_pages_render_empty(client):
    for url in ["/", "/upload", "/transactions", "/donations", "/receipts", "/settings", "/imports"]:
        assert client.get(url).status_code == 200, url


def test_import_categorize_and_report(client, app):
    r = upload(client, "/upload/bank", "bank.csv", sample("bank_statement_sample.csv"), account="Checking")
    assert b"imported 24 transactions" in r.data
    # Re-upload is a no-op
    r = upload(client, "/upload/bank", "bank.csv", sample("bank_statement_sample.csv"), account="Checking")
    assert b"imported 0 transactions" in r.data and b"24 already imported" in r.data
    upload(client, "/upload/giving", "gifts.csv", sample("giving_sample_tithely.csv"), platform="Tithe.ly")

    conn = connect(app.config["DATABASE"])
    cat = dict(conn.execute("SELECT description, category FROM transactions WHERE date LIKE '2026-09%'").fetchall())
    assert cat["TITHE.LY PAYOUT 090326"] == "Online Giving Payout"
    assert cat["PUBLIX #1234 VALDOSTA GA"] == "Food & Fellowship"
    assert cat["GEORGIA POWER BILL PAY"] == "Facilities & Utilities"
    assert cat["SOUTH GEORGIA CONFERENCE UMC"] == "Church & Conference Support"
    assert cat["GUSTO PAYROLL"] == "Staff & Payroll"
    assert cat["CHECK 1042"] is None

    from treasurer.reports import build_report
    from treasurer.db import set_setting
    set_setting(conn, "fiscal_year_start_month", "7")
    conn.commit()
    rep = build_report(conn, "2026-09")
    # Online gifts in Sept (gross): 250 + 75 + 100 + 500 + 100 + 150 = 1175
    assert rep["giving"]["total"] == 1175.0
    assert rep["giving"]["gifts"] == 6
    funds = {f["name"]: (f["amount"], f["ytd"]) for f in rep["giving"]["by_fund"]}
    assert funds["General Fund"] == (450.0, 550.0)  # YTD includes the August gift
    assert funds["Missions"] == (500.0, 500.0)
    # The payout deposit is not counted again as income
    names = [l["name"] for l in rep["other_income"]]
    assert "Online Giving Payout" not in names
    other = {l["name"]: l["amount"] for l in rep["other_income"]}
    assert other["Church & Conference Support"] == 1500.0
    assert other["Donations - Cash & Check"] == 350.0
    # Transfer from savings is excluded entirely
    assert rep["transfers_net"] == 500.0
    exp = {l["name"]: l["amount"] for l in rep["expenses"]}
    assert exp["Food & Fellowship"] == round(86.42 + 62.18 + 112.05, 2)
    fees_sept = round(7.55 + 2.48 + 3.20 + 14.80 + 3.20 + 4.65, 2)
    assert exp["Bank & Processing Fees"] == round(12.00 + fees_sept, 2)
    assert exp["Uncategorized withdrawals"] == 125.0
    assert rep["summary"]["income"] == round(1175 + 1500 + 350, 2)
    assert rep["bank"]["ending_balance"] == 12105.74
    assert rep["bank"]["beginning_balance"] == 12000.00
    # Fiscal year starting July: YTD includes the late-August gift
    assert rep["summary"]["ytd_income"] == round(rep["summary"]["income"] + 100, 2)
    assert len(rep["uncategorized"]) == 1

    page = client.get("/reports/2026-09")
    assert page.status_code == 200
    assert b"Report: September 2026" in page.data
    assert b'class="report-logo" src="/static/logo.png"' in page.data
    assert client.get("/static/logo.png").status_code == 200
    csv_resp = client.get("/reports/2026-09.csv")
    assert b"Income: online giving,General Fund,450.0" in csv_resp.data and b"GUSTO PAYROLL" in csv_resp.data


def test_manual_category_with_rule(client, app):
    load_samples(client)
    conn = connect(app.config["DATABASE"])
    tx = conn.execute("SELECT id FROM transactions WHERE description='SOUTHEASTERN RETREAT CENTER'").fetchone()
    client.post(f"/transactions/{tx['id']}/category",
                data={"category": "Retreats & Travel", "remember": "on"}, follow_redirects=True)
    row = conn.execute("SELECT category, category_source FROM transactions WHERE id=?", (tx["id"],)).fetchone()
    assert tuple(row) == ("Retreats & Travel", "manual")
    assert conn.execute("SELECT COUNT(*) FROM rules WHERE pattern='SOUTHEASTERN RETREAT CENTER'").fetchone()[0] == 1


def test_receipt_upload_and_auto_match(client, app):
    load_samples(client)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    upload(client, "/upload/receipts", "publix.png", png)
    conn = connect(app.config["DATABASE"])
    rid = conn.execute("SELECT id FROM receipts").fetchone()["id"]
    # Without AI the details are entered by hand; saving triggers matching
    client.post(f"/receipts/{rid}", data={"vendor": "Publix", "date": "2026-09-23",
                                          "amount": "112.05", "category": "Food & Fellowship"},
                follow_redirects=True)
    r = conn.execute("SELECT r.transaction_id, t.description, t.date FROM receipts r "
                     "JOIN transactions t ON t.id = r.transaction_id").fetchone()
    assert r["description"] == "PUBLIX #1234 VALDOSTA GA" and r["date"] == "2026-09-24"
    from treasurer.reports import build_report
    rep = build_report(conn, "2026-09")
    assert rep["receipts"]["receipted_count"] == 1
    assert all(m["id"] != r["transaction_id"] for m in rep["receipts"]["missing"])
    assert client.get(f"/receipts/{rid}/file").status_code == 200


def test_multiple_accounts_and_undo(client, app):
    load_samples(client)
    upload(client, "/upload/bank", "savings.ofx", sample("bank_statement_sample.ofx"), account="Savings")
    conn = connect(app.config["DATABASE"])
    from treasurer.reports import build_report
    rep = build_report(conn, "2026-10")
    assert rep["bank"]["ending_balance"] == round(9815.40 + 8203.12, 2)
    assert {a["name"] for a in rep["bank"]["accounts"]} == {"Checking", "Savings"}
    assert {l["name"]: l["amount"] for l in rep["other_income"]}["Interest"] == 3.12
    imp = conn.execute("SELECT id FROM imports WHERE filename='savings.ofx'").fetchone()["id"]
    client.post(f"/imports/{imp}/undo", follow_redirects=True)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account='Savings'").fetchone()[0] == 0


def test_pdf_requires_ai(client):
    r = upload(client, "/upload/bank", "statement.pdf", b"%PDF-1.4")
    assert b"PDF statements need the Claude integration" in r.data


def test_budget_variance(client, app):
    load_samples(client)
    client.post("/settings", data={"action": "budgets", "budget_Food & Fellowship": "200"})
    conn = connect(app.config["DATABASE"])
    from treasurer.reports import build_report
    line = next(l for l in build_report(conn, "2026-09")["expenses"] if l["name"] == "Food & Fellowship")
    assert line["budget"] == 200 and line["variance"] == round(200 - 260.65, 2)


def test_password_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setenv("TREASURER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_PASSWORD", "hunter2")
    from treasurer import create_app
    app = create_app({"SECRET_KEY": "x"})
    c = app.test_client()
    assert c.get("/").status_code == 302
    page = c.get("/login").data.decode()
    with c.session_transaction() as s:
        token = s["csrf"]
    # Forms must carry the real token (macros need the template context)
    assert f'name="csrf" value="{token}"' in page
    assert c.post("/login", data={"password": "hunter2"}).status_code == 400  # no CSRF token
    assert c.post("/login", data={"password": "nope", "csrf": token}).status_code == 200
    r = c.post("/login", data={"password": "hunter2", "csrf": token})
    assert r.status_code == 302
    assert c.get("/").status_code == 200
