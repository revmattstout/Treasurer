"""Email the monthly treasurer's report on a schedule.

On the third Wednesday of each month, the report for the previous month is
emailed to the recipients set on the Settings page (by default
revmattstout@gmail.com). Statements for the prior month have usually
arrived and been uploaded by then.

Mail goes out through the same account used for email import
(TREASURER_EMAIL_ADDRESS + TREASURER_EMAIL_APP_PASSWORD), via Gmail's SMTP
server unless TREASURER_SMTP_HOST / TREASURER_SMTP_PORT say otherwise.

Sending happens:
  * automatically while the app is running (checked every 15 minutes), and
    catches up later in the month if the app was off on the day itself;
  * from the command line, for cron / Task Scheduler (run it daily; it only
    sends when a report is due):        python -m treasurer.mailer
  * on demand:                          python -m treasurer.mailer --month 2026-10 --now
    or the "Email this report" button on a report page.
"""
import os
import smtplib
import sqlite3
import ssl
import threading
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from . import ai
from .db import get_setting
from .reports import build_report, report_csv_text, report_for_ai, shift_month

DEFAULT_RECIPIENTS = "revmattstout@gmail.com"
MAX_ATTEMPTS = 3
CHECK_INTERVAL_SECONDS = 15 * 60


class MailerError(RuntimeError):
    pass


def third_wednesday(year, month):
    first = date(year, month, 1)
    # weekday(): Monday=0 ... Wednesday=2
    first_wed = first + timedelta(days=(2 - first.weekday()) % 7)
    return first_wed + timedelta(days=14)


def _now():
    tzname = os.environ.get("TREASURER_TIMEZONE", "America/New_York")
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname))
    except Exception:  # tz database missing (some Windows installs): use local time
        return datetime.now()


def recipients(conn):
    raw = get_setting(conn, "report_recipients", DEFAULT_RECIPIENTS) or ""
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def schedule_enabled(conn):
    return get_setting(conn, "report_schedule_enabled", "1") == "1"


def send_hour(conn):
    try:
        return min(max(int(get_setting(conn, "report_send_hour", "8")), 0), 23)
    except ValueError:
        return 8


def due_month(conn, now=None):
    """The report month that should be emailed now, or None.

    Due from the third Wednesday (at the send hour) until the end of that
    month, for the previous month's report, unless already sent or it has
    failed MAX_ATTEMPTS times.
    """
    now = now or _now()
    if not schedule_enabled(conn) or not recipients(conn):
        return None
    send_day = third_wednesday(now.year, now.month)
    if now.date() < send_day or (now.date() == send_day and now.hour < send_hour(conn)):
        return None
    month = shift_month(f"{now.year:04d}-{now.month:02d}", -1)
    row = conn.execute(
        "SELECT status, attempts, updated_at < datetime('now', '-1 hour') AS stale "
        "FROM report_sends WHERE month=? AND scheduled=1", (month,)).fetchone()
    if row and (row["status"] == "sent" or row["attempts"] >= MAX_ATTEMPTS
                or (row["status"] == "sending" and not row["stale"])):
        return None
    return month


def _smtp_config():
    return {
        "address": os.environ.get("TREASURER_EMAIL_ADDRESS", "").strip(),
        "password": os.environ.get("TREASURER_EMAIL_APP_PASSWORD", "").replace(" ", ""),
        "host": os.environ.get("TREASURER_SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("TREASURER_SMTP_PORT", "587")),
    }


def configured():
    c = _smtp_config()
    return bool(c["address"] and c["password"])


def build_message(app, conn, month, to):
    org = get_setting(conn, "org_name", "VSU Wesley Foundation")
    rep = build_report(conn, month)
    narrative = conn.execute("SELECT text FROM narratives WHERE month=?", (month,)).fetchone()
    narrative_is_draft = False
    has_data = rep["bank"]["transaction_count"] or rep["giving"]["gifts"]
    if not narrative and has_data and ai.enabled():
        try:
            text = ai.write_narrative(report_for_ai(rep), org)
            conn.execute("INSERT OR IGNORE INTO narratives(month, text) VALUES (?,?)", (month, text))
            conn.commit()
            narrative = {"text": text}
            narrative_is_draft = True
        except ai.AIError:
            narrative = None

    env = app.jinja_env
    month_label = env.filters["month_name"](month)
    html = env.get_template("report_email.html").render(
        r=rep, org_name=org, narrative=narrative, narrative_is_draft=narrative_is_draft,
        has_data=has_data, month_label=month_label, app_url=os.environ.get("TREASURER_APP_URL", ""))

    s = rep["summary"]
    money = env.filters["money"]
    text = (f"{org} treasurer's report for {month_label}\n\n"
            + (f"Income:   {money(s['income'])}\nExpenses: {money(s['expense'])}\nNet:      {money(s['net'])}\n"
               f"Fiscal year to date net: {money(s['ytd_net'])}\n"
               if has_data else "No bank or giving data has been uploaded for this month yet.\n")
            + (f"\n{narrative['text']}\n" if narrative else "")
            + "\nThe full report is in this email (HTML) and attached as a spreadsheet (CSV).\n")

    msg = EmailMessage()
    msg["Subject"] = f"{org}: Treasurer's report for {month_label}" + ("" if has_data else " (no data yet)")
    msg["From"] = formataddr((f"{org} Treasurer", _smtp_config()["address"]))
    msg["To"] = ", ".join(to)
    msg["Message-ID"] = make_msgid(domain="treasurer.local")
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(report_csv_text(conn, month).encode("utf-8"), maintype="text", subtype="csv",
                       filename=f"wesley-report-{month}.csv")
    msg.add_attachment(html.encode("utf-8"), maintype="text", subtype="html",
                       filename=f"wesley-report-{month}.html")
    return msg


def _smtp_send(msg, smtp_factory=smtplib.SMTP):
    c = _smtp_config()
    if not (c["address"] and c["password"]):
        raise MailerError("Report email isn't set up: set TREASURER_EMAIL_ADDRESS and "
                          "TREASURER_EMAIL_APP_PASSWORD (a Gmail app password).")
    try:
        with smtp_factory(c["host"], c["port"], timeout=60) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(c["address"], c["password"])
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailerError("Gmail rejected the login. Use a Gmail app password, not the account password.")
    except (smtplib.SMTPException, OSError) as e:
        raise MailerError(f"Couldn't send email: {e}")


def send_report(app, conn, month, to=None, scheduled=False, smtp_factory=smtplib.SMTP):
    """Send one month's report. Records the attempt in report_sends."""
    to = to or recipients(conn)
    if not to:
        raise MailerError("No report recipients are set (Settings → Monthly report email).")
    if scheduled:
        # Claim the month so two processes can't both send it
        try:
            conn.execute("INSERT INTO report_sends(month, scheduled, status, attempts, recipients) "
                         "VALUES (?, 1, 'sending', 0, ?)", (month, ", ".join(to)))
        except sqlite3.IntegrityError:
            # Retry after a failure, or after a send that crashed partway (stale 'sending')
            cur = conn.execute(
                "UPDATE report_sends SET status='sending', recipients=?, updated_at=datetime('now') "
                "WHERE month=? AND scheduled=1 AND attempts < ? AND (status='failed' OR "
                "(status='sending' AND updated_at < datetime('now', '-1 hour')))",
                (", ".join(to), month, MAX_ATTEMPTS))
            if cur.rowcount == 0:
                conn.commit()
                return False
        conn.commit()
    try:
        _smtp_send(build_message(app, conn, month, to), smtp_factory)
    except Exception as e:
        err = str(e) if isinstance(e, MailerError) else f"{type(e).__name__}: {e}"
        if scheduled:
            conn.execute("UPDATE report_sends SET status='failed', attempts=attempts+1, error=?, "
                         "updated_at=datetime('now') WHERE month=? AND scheduled=1", (err[:1000], month))
        else:
            conn.execute("INSERT INTO report_sends(month, scheduled, status, attempts, recipients, error) "
                         "VALUES (?, 0, 'failed', 1, ?, ?)", (month, ", ".join(to), err[:1000]))
        conn.commit()
        raise MailerError(err) from e
    if scheduled:
        conn.execute("UPDATE report_sends SET status='sent', attempts=attempts+1, error=NULL, "
                     "updated_at=datetime('now') WHERE month=? AND scheduled=1", (month,))
    else:
        conn.execute("INSERT INTO report_sends(month, scheduled, status, attempts, recipients) "
                     "VALUES (?, 0, 'sent', 1, ?)", (month, ", ".join(to)))
    conn.commit()
    return True


def send_if_due(app, conn, now=None, smtp_factory=smtplib.SMTP):
    month = due_month(conn, now)
    if not month or not configured():
        return None
    send_report(app, conn, month, scheduled=True, smtp_factory=smtp_factory)
    return month


def next_send(conn, now=None):
    """(send date, report month) of the next scheduled email."""
    now = now or _now()
    this_month = f"{now.year:04d}-{now.month:02d}"
    due = due_month(conn, now)
    if due:
        return now.date(), due
    d = third_wednesday(now.year, now.month)
    if now.date() < d or (now.date() == d and now.hour < send_hour(conn)):
        return d, shift_month(this_month, -1)
    nxt = shift_month(this_month, 1)
    y, m = map(int, nxt.split("-"))
    return third_wednesday(y, m), this_month


# ---------------------------------------------------------------- background / CLI

def start_scheduler(app):
    def loop():
        from .db import connect
        while True:
            try:
                conn = connect(app.config["DATABASE"])
                try:
                    month = send_if_due(app, conn)
                    if month:
                        app.logger.info("Emailed the %s treasurer's report", month)
                finally:
                    conn.close()
            except MailerError as e:
                app.logger.warning("Scheduled report email failed: %s", e)
            except Exception:
                app.logger.exception("Report scheduler crashed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    t = threading.Thread(target=loop, name="report-mailer", daemon=True)
    t.start()
    return t


def main(argv=None):
    import argparse
    from . import create_app
    from .db import connect
    p = argparse.ArgumentParser(description="Email the monthly treasurer's report.")
    p.add_argument("--month", help="report month YYYY-MM (with --now)")
    p.add_argument("--now", action="store_true", help="send immediately instead of only when due")
    p.add_argument("--to", help="comma-separated recipients (default: the Settings page list)")
    args = p.parse_args(argv)
    app = create_app({"EMAIL_POLLER": False})
    conn = connect(app.config["DATABASE"])
    try:
        to = [a.strip() for a in args.to.split(",")] if args.to else None
        if args.now:
            month = args.month or shift_month(date.today().strftime("%Y-%m"), -1)
            send_report(app, conn, month, to=to)
            print(f"Sent the {month} report.")
        else:
            month = send_if_due(app, conn)
            print(f"Sent the {month} report." if month else "No report due right now.")
        return 0
    except MailerError as e:
        print("Error:", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
