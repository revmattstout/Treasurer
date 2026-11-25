"""Background jobs in their own process: checking the email inbox and
sending the monthly report email.

On Render this runs next to the web server (see render.yaml) so that a slow
or stuck job (a large attachment, an unresponsive mail server, a long
Claude call) can never stop the website from answering.

    python -m treasurer.jobs
"""
import logging
import os
import time

from . import create_app
from .db import connect

log = logging.getLogger("treasurer.jobs")

# Give the web server a head start after a deploy before doing heavy work.
STARTUP_DELAY_SECONDS = 60
REPORT_CHECK_SECONDS = 15 * 60


def run_forever():
    from . import mailbox, mailer
    app = create_app({"EMAIL_POLLER": False})
    poll_minutes = os.environ.get("TREASURER_EMAIL_POLL_MINUTES")
    email_every = max(float(poll_minutes), 1.0) * 60 if poll_minutes else None
    log.info("Background jobs started (email every %s s, report check every %s s)",
             email_every, REPORT_CHECK_SECONDS)
    time.sleep(STARTUP_DELAY_SECONDS)
    next_email = next_report = 0.0
    while True:
        now = time.monotonic()
        if email_every and mailbox.config_status()["configured"] and now >= next_email:
            next_email = now + email_every
            conn = connect(app.config["DATABASE"])
            try:
                s = mailbox.fetch_and_import(conn, app.config["RECEIPTS_DIR"])
                if s.emails:
                    log.info("Email check: %s", s.message())
                for e in s.errors:
                    log.warning("Email import error: %s", e)
            except mailbox.MailboxError as e:
                log.warning("Email check failed: %s", e)
            except Exception:
                log.exception("Email check crashed")
            finally:
                conn.close()
        if mailer.configured() and now >= next_report:
            next_report = now + REPORT_CHECK_SECONDS
            conn = connect(app.config["DATABASE"])
            try:
                month = mailer.send_if_due(app, conn)
                if month:
                    log.info("Emailed the %s treasurer's report", month)
            except mailer.MailerError as e:
                log.warning("Scheduled report email failed: %s", e)
            except Exception:
                log.exception("Report scheduler crashed")
            finally:
                conn.close()
        time.sleep(30)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s jobs %(levelname)s %(message)s")
    run_forever()
