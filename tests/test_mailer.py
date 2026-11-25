import email
import io
from datetime import date, datetime

import pytest

from treasurer import mailer
from treasurer.db import connect, set_setting
from conftest import sample


class FakeSMTP:
    sent = []
    fail = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        pass

    def login(self, user, pw):
        assert pw == "abcdefghijklmnop"

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise mailer.smtplib.SMTPServerDisconnected("connection lost")
        FakeSMTP.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.sent, FakeSMTP.fail = [], False


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setenv("TREASURER_EMAIL_ADDRESS", "treasurer@example.com")
    monkeypatch.setenv("TREASURER_EMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")


def load(client):
    client.post("/upload/bank", data={"files": (io.BytesIO(sample("bank_statement_sample.csv")), "b.csv")},
                content_type="multipart/form-data")
    client.post("/upload/giving", data={"files": (io.BytesIO(sample("giving_sample_tithely.csv")), "g.csv")},
                content_type="multipart/form-data")


@pytest.mark.parametrize("y,m,expected", [
    (2026, 11, date(2026, 11, 18)),  # Nov 1 2026 is a Sunday
    (2026, 12, date(2026, 12, 16)),
    (2027, 1, date(2027, 1, 20)),
    (2026, 4, date(2026, 4, 15)),    # Apr 1 2026 is a Wednesday
])
def test_third_wednesday(y, m, expected):
    d = mailer.third_wednesday(y, m)
    assert d == expected and d.weekday() == 2 and 15 <= d.day <= 21


def test_due_month_window(app):
    conn = connect(app.config["DATABASE"])
    assert set_setting(conn, "report_send_hour", "8") is None
    assert mailer.due_month(conn, datetime(2026, 11, 17, 12)) is None      # Tuesday before
    assert mailer.due_month(conn, datetime(2026, 11, 18, 7)) is None       # before 8 AM
    assert mailer.due_month(conn, datetime(2026, 11, 18, 8)) == "2026-10"  # third Wednesday, 8 AM
    assert mailer.due_month(conn, datetime(2026, 11, 27, 9)) == "2026-10"  # catches up later
    assert mailer.due_month(conn, datetime(2027, 1, 20, 9)) == "2026-12"   # across the year
    set_setting(conn, "report_schedule_enabled", "0")
    assert mailer.due_month(conn, datetime(2026, 11, 18, 9)) is None


def test_next_send(app):
    conn = connect(app.config["DATABASE"])
    assert mailer.next_send(conn, datetime(2026, 11, 2, 9)) == (date(2026, 11, 18), "2026-10")
    assert mailer.next_send(conn, datetime(2026, 11, 18, 9)) == (date(2026, 11, 18), "2026-10")


def test_scheduled_send_once(app, client, smtp_env):
    load(client)
    conn = connect(app.config["DATABASE"])
    now = datetime(2026, 11, 18, 9)
    assert mailer.send_if_due(app, conn, now, smtp_factory=FakeSMTP) == "2026-10"
    assert mailer.send_if_due(app, conn, now, smtp_factory=FakeSMTP) is None  # not twice
    assert len(FakeSMTP.sent) == 1
    msg = FakeSMTP.sent[0]
    assert msg["To"] == "revmattstout@gmail.com"
    assert msg["Subject"] == "VSU Wesley Foundation: Treasurer's report for October 2026"
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    html = parsed.get_body(preferencelist=("html",)).get_content()
    assert "Treasurer's Report: October 2026" in html and "$1,075.00" in html
    names = [p.get_filename() for p in parsed.iter_attachments()]
    assert names == ["wesley-report-2026-10.csv", "wesley-report-2026-10.html"]
    row = conn.execute("SELECT status, scheduled FROM report_sends").fetchone()
    assert tuple(row) == ("sent", 1)


def test_failed_send_retries_then_stops(app, client, smtp_env):
    load(client)
    conn = connect(app.config["DATABASE"])
    FakeSMTP.fail = True
    now = datetime(2026, 11, 18, 9)
    for _ in range(mailer.MAX_ATTEMPTS):
        with pytest.raises(mailer.MailerError):
            mailer.send_if_due(app, conn, now, smtp_factory=FakeSMTP)
    assert mailer.due_month(conn, now) is None  # gives up after MAX_ATTEMPTS
    row = conn.execute("SELECT status, attempts, error FROM report_sends").fetchone()
    assert row["status"] == "failed" and row["attempts"] == 3 and "connection lost" in row["error"]


def test_no_data_month_still_sends_reminder(app, smtp_env):
    conn = connect(app.config["DATABASE"])
    mailer.send_report(app, conn, "2026-10", smtp_factory=FakeSMTP)
    assert FakeSMTP.sent[0]["Subject"].endswith("(no data yet)")


def test_not_configured(app, monkeypatch):
    monkeypatch.delenv("TREASURER_EMAIL_APP_PASSWORD", raising=False)
    conn = connect(app.config["DATABASE"])
    assert mailer.send_if_due(app, conn, datetime(2026, 11, 18, 9)) is None
    with pytest.raises(mailer.MailerError, match="app password"):
        mailer.send_report(app, conn, "2026-10", smtp_factory=FakeSMTP)


def test_settings_and_manual_send(app, client, smtp_env, monkeypatch):
    load(client)
    page = client.get("/settings").data.decode()
    assert "revmattstout@gmail.com" in page and "third Wednesday" in page
    r = client.post("/settings", data={"action": "report_email", "report_recipients": "a@example.com; nope",
                                       "enabled": "on", "report_send_hour": "9"}, follow_redirects=True)
    assert b"Not a valid email address: nope" in r.data
    client.post("/settings", data={"action": "report_email", "report_recipients": "a@example.com, b@example.org",
                                   "enabled": "on", "report_send_hour": "9"})
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer, "_smtp_send", lambda msg, f=None: FakeSMTP.sent.append(msg))
    r = client.post("/reports/2026-09/email", follow_redirects=True)
    assert b"Emailed the 2026-09 report to a@example.com, b@example.org" in r.data
    assert FakeSMTP.sent[0]["To"] == "a@example.com, b@example.org"
