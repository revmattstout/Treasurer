import imaplib
from email.message import EmailMessage

import pytest

from treasurer import mailbox
from treasurer.db import connect
from conftest import sample

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20_000


def make_email(sender, subject, attachments, body="", auth="dmarc=pass (p=NONE) header.from=gmail.com", mid=None):
    m = EmailMessage()
    m["From"] = f"Matt <{sender}>"
    m["To"] = "treasurer@example.com"
    m["Subject"] = subject
    m["Message-ID"] = mid or f"<{abs(hash((sender, subject, body)))}@example.com>"
    if auth:
        m["Authentication-Results"] = f"mx.google.com; {auth}"
    m.set_content(body or "See attached.")
    for name, data, maintype, subtype in attachments:
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages, fail_login=False):
        self.messages = messages
        self.fail_login = fail_login
        self.seen = set()

    def __call__(self, host, timeout=None):
        assert timeout  # connections must never wait forever
        return self

    def login(self, user, pw):
        if self.fail_login:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, box):
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.messages)))]

    def fetch(self, num, what):
        raw = self.messages[int(num) - 1]
        if "HEADER.FIELDS" in what:
            hdr = b"".join(l + b"\r\n" for l in raw.split(b"\n") if l.lower().startswith(b"message-id"))
            return "OK", [(b"1", hdr)]
        return "OK", [(b"1", raw)]

    def store(self, num, flags, value):
        self.seen.add(int(num))

    def logout(self):
        pass


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TREASURER_EMAIL_ADDRESS", "treasurer@example.com")
    monkeypatch.setenv("TREASURER_EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("TREASURER_EMAIL_ALLOWED_SENDERS", "matt@gmail.com, helper@valdosta.edu")


def test_classify():
    assert mailbox.classify("export.ofx", "", b"") == "bank"
    assert mailbox.classify("photo.JPG", "", b"") == "receipt"
    assert mailbox.classify("scan.pdf", "Publix receipt", b"") == "receipt"
    assert mailbox.classify("scan.pdf", "October bank statement", b"") == "bank"
    assert mailbox.classify("export.csv", "Tithe.ly gifts", b"") == "giving"
    assert mailbox.classify("export.csv", "", b"Date,Description,Amount\n") == "bank"
    assert mailbox.classify("export.csv", "", sample("giving_sample_tithely.csv")) == "giving"
    assert mailbox.classify("notes.docx", "", b"") is None


def test_not_configured(app):
    conn = connect(app.config["DATABASE"])
    with pytest.raises(mailbox.MailboxError, match="TREASURER_EMAIL_APP_PASSWORD"):
        mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=FakeIMAP([]))


def test_imports_all_kinds(app, configured):
    conn = connect(app.config["DATABASE"])
    msgs = [
        make_email("matt@gmail.com", "September bank statement - checking",
                   [("statement.csv", sample("bank_statement_sample.csv"), "text", "csv")]),
        make_email("matt@gmail.com", "Tithe.ly giving",
                   [("gifts.csv", sample("giving_sample_tithely.csv"), "text", "csv")]),
        make_email("matt@gmail.com", "Fwd: Receipt", [("IMG_0001.png", PNG, "image", "png")],
                   body="Groceries for Tuesday dinner\n\nSent from my iPhone"),
    ]
    fake = FakeIMAP(msgs)
    s = mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=fake)
    assert (s.emails, s.statements, s.giving, s.receipts, s.errors) == (3, 1, 1, 1, [])
    assert fake.seen == {1, 2, 3}
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE account='Checking'").fetchone()[0] == 24
    assert conn.execute("SELECT platform FROM donations LIMIT 1").fetchone()[0] == "Tithe.ly"
    r = conn.execute("SELECT purpose FROM receipts").fetchone()
    assert r["purpose"] == "Groceries for Tuesday dinner"
    assert "emailed by matt@gmail.com" in conn.execute("SELECT note FROM imports WHERE kind='bank'").fetchone()[0]

    # Checking again doesn't re-import anything
    s2 = mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=fake)
    assert s2.emails == 0
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_rejects_unknown_and_forged_senders(app, configured):
    conn = connect(app.config["DATABASE"])
    msgs = [
        make_email("stranger@example.com", "receipt", [("a.png", PNG, "image", "png")]),
        make_email("matt@gmail.com", "receipt", [("b.png", PNG, "image", "png")], auth="dmarc=fail", mid="<f@x>"),
        make_email("helper@valdosta.edu", "receipt", [("c.png", PNG, "image", "png")],
                   auth="dkim=pass header.i=@valdosta.edu; dmarc=none", mid="<ok@x>"),
    ]
    s = mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=FakeIMAP(msgs))
    assert s.rejected == 2 and s.receipts == 1
    statuses = dict(conn.execute("SELECT sender, status FROM email_log").fetchall())
    assert statuses == {"stranger@example.com": "rejected", "matt@gmail.com": "rejected",
                        "helper@valdosta.edu": "imported"}


def test_bad_login_message(app, configured):
    conn = connect(app.config["DATABASE"])
    with pytest.raises(mailbox.MailboxError, match="app password"):
        mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"], imap_factory=FakeIMAP([], fail_login=True))


def test_upload_page_shows_email_setup(client, configured):
    page = client.get("/upload").data.decode()
    assert "Check email now" in page and "treasurer@example.com" in page
