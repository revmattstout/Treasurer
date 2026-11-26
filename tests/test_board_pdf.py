import io

import pdfplumber
import pytest

from treasurer import ai, checks
from treasurer.board_pdf import MAX_PAGES, build_board_pdf
from treasurer.db import connect
from conftest import sample
from test_mailbox import PNG


def pdf_text(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return len(doc.pages), "\n".join(p.extract_text() or "" for p in doc.pages)


@pytest.fixture
def loaded(app, client, monkeypatch):
    for url, f in [("/upload/bank", "bank_statement_sample.csv"), ("/upload/giving", "planning_center_sample.csv"),
                   ("/upload/giving", "giving_sample_tithely.csv")]:
        client.post(url, data={"files": (io.BytesIO(sample(f)), f)}, content_type="multipart/form-data")
    conn = connect(app.config["DATABASE"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(ai, "read_checks", lambda d, m: ai.CheckPhoto(checks=[
        ai.CheckItem(payer="Riley Patel", amount=400, date="2026-10-18", check_number="1", memo="rent"),
        ai.CheckItem(payer="Hannah Checkwriter", amount=25, date="2026-10-19", check_number="2", memo=None)]))
    checks.add_batch(conn, app.config["RECEIPTS_DIR"], "c.png", PNG, "2026-10-20", "uploaded")
    return conn


def test_board_pdf_fits_and_lists_every_category(app, loaded):
    pdf, pages = build_board_pdf(loaded, "2026-10", "VSU Wesley Foundation")
    n, text = pdf_text(pdf)
    assert n == pages <= MAX_PAGES
    for (name,) in loaded.execute("SELECT name FROM categories WHERE kind IN ('income','expense')"):
        assert name.replace("&", "&") in text, name
    assert "Treasurer's Report: October 2026" in text and "Total expenses" in text


def test_board_pdf_has_no_individuals_or_purchases(app, loaded):
    pdf, _ = build_board_pdf(loaded, "2026-10", "VSU Wesley Foundation")
    _, text = pdf_text(pdf)
    names = {r[0] for r in loaded.execute("SELECT donor FROM donations")} | {"Riley Patel", "Hannah Checkwriter"}
    vendors = {"PUBLIX", "GUSTO", "CHICK-FIL-A", "WALMART", "SOUTHEASTERN RETREAT", "GEORGIA POWER"}
    for word in names | vendors:
        assert word not in text, word


def test_board_pdf_always_fits_two_pages(app, loaded):
    for i in range(45):
        loaded.execute("INSERT INTO categories(name, kind, monthly_budget) VALUES (?, 'expense', 100)",
                       (f"Extra expense category with a long name {i}",))
    for i in range(20):
        loaded.execute("INSERT INTO donations(date, gross, fee, net, donor, fund, kind, fingerprint) "
                       "VALUES ('2026-10-05', 10, 0, 10, 'X', ?, 'gift', ?)", (f"Special Fund {i}", f"s{i}"))
    loaded.execute("INSERT OR REPLACE INTO narratives(month, text) VALUES ('2026-10', ?)", ("Long summary. " * 600,))
    loaded.commit()
    pdf, pages = build_board_pdf(loaded, "2026-10", "VSU Wesley Foundation")
    n, text = pdf_text(pdf)
    assert n == pages <= MAX_PAGES
    assert "Extra expense category with a long name 44" in text


def test_board_pdf_route(client, loaded):
    r = client.get("/reports/2026-10/board.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    assert "Board report (PDF)" in client.get("/reports/2026-10").data.decode()
