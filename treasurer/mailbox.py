"""Pull receipts, bank statements and giving exports from an email inbox.

Email attachments to the treasurer address and they're imported like web
uploads. Configuration comes from environment variables only, so the
password is never stored in code or in the database:

  TREASURER_EMAIL_ADDRESS          the inbox, e.g. vsuwesleyfinance@gmail.com
  TREASURER_EMAIL_APP_PASSWORD     a Gmail *app password* (not the account password)
  TREASURER_EMAIL_ALLOWED_SENDERS  comma-separated addresses allowed to send files
  TREASURER_EMAIL_IMAP_HOST        default imap.gmail.com
  TREASURER_EMAIL_POLL_MINUTES     if set, check automatically every N minutes
  TREASURER_EMAIL_REQUIRE_AUTH     default "1": only accept mail whose sender
                                   passed DMARC/DKIM checks (blocks forged From:)

How attachments are sorted:
  * OFX / QFX / QBO files                          -> bank statement
  * CSV files                                      -> giving export if the subject,
                                                      filename or columns look like
                                                      giving (fund, donor, fee...),
                                                      otherwise bank statement
  * PDFs whose subject/filename says "statement"   -> bank statement
  * other PDFs and all photos                      -> receipts
The email's subject and first line of text become the receipt's "purpose".

Run once from the command line (for cron):   python -m treasurer.mailbox
Keep checking every 10 minutes:              python -m treasurer.mailbox --watch 10
"""
import email
import email.policy
import hashlib
import imaplib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from email.utils import parseaddr, parsedate_to_datetime

from . import ai, services
from .importers import ImportError_, decode

LOOKBACK_DAYS = 45
IMAP_TIMEOUT_SECONDS = 60
MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024
_lock = threading.Lock()

GIVING_WORDS = ("giving", "donation", "gift", "tithe", "offering", "pushpay", "givelify",
                "vanco", "subsplash", "planning center", "contribution")
STATEMENT_WORDS = ("statement", "bank", "checking", "savings")
GIVING_COLUMNS = ("fund", "donor", "fee", "designation", "envelope", "giver", "first name", "net")
IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/heic": ".heic", "image/webp": ".webp",
             "image/gif": ".gif"}


class MailboxError(RuntimeError):
    pass


@dataclass
class Summary:
    emails: int = 0
    receipts: int = 0
    statements: int = 0
    giving: int = 0
    checks: int = 0
    rejected: int = 0
    errors: list = field(default_factory=list)

    def message(self):
        if not self.emails:
            return "No new email."
        parts = [f"Checked {self.emails} new email(s)"]
        for n, label in ((self.receipts, "receipt"), (self.checks, "check photo"),
                         (self.statements, "bank statement"),
                         (self.giving, "giving file")):
            if n:
                parts.append(f"{n} {label}{'s' if n != 1 else ''}")
        if self.rejected:
            parts.append(f"{self.rejected} ignored (sender not allowed)")
        return ", ".join(parts) + "."


def _config():
    return {
        "address": os.environ.get("TREASURER_EMAIL_ADDRESS", "").strip(),
        "password": os.environ.get("TREASURER_EMAIL_APP_PASSWORD", "").replace(" ", ""),
        "host": os.environ.get("TREASURER_EMAIL_IMAP_HOST", "imap.gmail.com").strip(),
        "allowed": {a.strip().lower() for a in os.environ.get("TREASURER_EMAIL_ALLOWED_SENDERS", "").split(",")
                    if a.strip()},
        "require_auth": os.environ.get("TREASURER_EMAIL_REQUIRE_AUTH", "1") != "0",
    }


def config_status():
    c = _config()
    return {"address": c["address"], "configured": bool(c["address"] and c["password"] and c["allowed"]),
            "missing": [name for name, ok in (
                ("TREASURER_EMAIL_ADDRESS", c["address"]),
                ("TREASURER_EMAIL_APP_PASSWORD", c["password"]),
                ("TREASURER_EMAIL_ALLOWED_SENDERS", c["allowed"])) if not ok],
            "allowed": sorted(c["allowed"]),
            "poll_minutes": os.environ.get("TREASURER_EMAIL_POLL_MINUTES")}


# ---------------------------------------------------------------- classification

def _has(text, words):
    t = (text or "").lower()
    return any(w in t for w in words)


def classify(filename, subject, data):
    """Return 'bank', 'giving', 'receipt', 'check' or None (ignore)."""
    name = filename.lower()
    ext = os.path.splitext(name)[1]
    hint = f"{subject} {name}"
    if ext in (".ofx", ".qfx", ".qbo"):
        return "bank"
    if ext in (".csv", ".tsv", ".txt"):
        if _has(hint, GIVING_WORDS):
            return "giving"
        if _has(hint, STATEMENT_WORDS):
            return "bank"
        head = decode(data[:4000]).lower()
        header_lines = head.splitlines()[:25]
        if any(_has(line, GIVING_COLUMNS) for line in header_lines):
            return "giving"
        return "bank"
    if ext in (".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif"):
        subj = (subject or "").lower()
        if re.search(r"\b(checks?|cheques?|deposit(s|ed|ing)?)\b", subj) and "statement" not in subj:
            return "check"
        if ext == ".pdf" and _has(hint, ("statement",)):
            return "bank"
        return "receipt"
    return None


def _account_hint(subject):
    s = (subject or "").lower()
    m = re.search(r"account\s*[:\-]\s*([\w &'-]{2,40})", subject or "", re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if "savings" in s:
        return "Savings"
    if "checking" in s:
        return "Checking"
    return None


def _clean_subject(subject):
    s = re.sub(r"^\s*((re|fwd?|fw)\s*:\s*)+", "", subject or "", flags=re.IGNORECASE)
    return re.sub(r"\breceipts?\b[:\s-]*", "", s, flags=re.IGNORECASE).strip(" -:")


def _purpose(msg, subject):
    body = ""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        try:
            body = part.get_content()
        except (LookupError, UnicodeDecodeError):
            body = ""
    first = next((ln.strip() for ln in body.splitlines()
                  if ln.strip() and not ln.startswith(">") and not ln.lower().startswith(("sent from", "--"))), "")
    bits = [b for b in (_clean_subject(subject), first) if b]
    text = " — ".join(dict.fromkeys(bits))  # drop duplicates, keep order
    return text[:200] or None


def _received_date(msg):
    try:
        return parsedate_to_datetime(str(msg.get("Date"))).date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


def _check_desc(r):
    desc = f"photo of checks, {r['checks']} check{'s' if r['checks'] != 1 else ''} read"
    desc += ", matched to a bank deposit" if r["matched"] else ", deposit not found yet"
    if r["error"]:
        desc += f" ({r['error']})"
    return desc


def _attachments(msg):
    """Yield (filename, bytes) for every file in the message, including
    inline photos (phones often send pictures without a filename)."""
    n = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        filename = part.get_filename()
        if not filename:
            if ctype not in IMAGE_EXT and ctype != "application/pdf":
                continue
            n += 1
            filename = f"email-attachment-{n}{IMAGE_EXT.get(ctype, '.pdf')}"
        data = part.get_payload(decode=True)
        if not data or len(data) > MAX_ATTACHMENT_BYTES:
            continue
        # Skip tiny inline images such as email signature logos
        if ctype.startswith("image/") and len(data) < 15_000 and part.get("Content-ID"):
            continue
        yield os.path.basename(filename.replace("\\", "/")), data


def _sender_authenticated(msg, sender):
    """True if the receiving server (Gmail) recorded a DMARC pass, or a DKIM
    pass for the sender's domain. Stops someone forging an allowed From:."""
    domain = sender.rsplit("@", 1)[-1]
    for h in msg.get_all("Authentication-Results", []) or []:
        h = str(h).lower()
        if "dmarc=pass" in h:
            return True
        for m in re.finditer(r"dkim=pass[^;]*header\.(?:i|d)=@?([\w.-]+)", h):
            if m.group(1) == domain or m.group(1).endswith("." + domain):
                return True
    return False


# ---------------------------------------------------------------- processing

def process_message(conn, receipts_dir, raw, allowed, require_auth, summary):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip() or hashlib.sha256(raw).hexdigest()
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    subject = str(msg.get("Subject", "") or "")
    # Claim the message first: the UNIQUE message_id stops two workers (or a
    # double-click) from importing it twice.
    try:
        cur = conn.execute("INSERT INTO email_log(message_id, sender, subject, status) VALUES (?,?,?, 'processing')",
                           (message_id, sender, subject[:300]))
        conn.commit()
    except sqlite3.IntegrityError:
        return False  # already handled
    log_id = cur.lastrowid
    summary.emails += 1

    def finish(status, detail):
        conn.execute("UPDATE email_log SET status=?, detail=? WHERE id=?", (status, detail[:2000], log_id))
        conn.commit()

    if sender not in allowed:
        summary.rejected += 1
        finish("rejected", f"{sender or 'unknown sender'} is not in TREASURER_EMAIL_ALLOWED_SENDERS")
        return True
    if require_auth and not _sender_authenticated(msg, sender):
        summary.rejected += 1
        finish("rejected", "sender address could not be verified (no DMARC/DKIM pass); possible forgery")
        return True

    results = []
    purpose = _purpose(msg, subject)
    received = _received_date(msg)
    found = False
    for filename, data in _attachments(msg):
        found = True
        kind = classify(filename, subject, data)
        note = f"emailed by {sender}"
        try:
            if kind == "bank":
                r = services.import_bank(conn, filename, data, _account_hint(subject), source_note=note)
                summary.statements += 1
                results.append(f"{filename}: bank statement, {r['added']} new transactions")
            elif kind == "giving":
                platform = next((p for p in ("Tithe.ly", "Pushpay", "Givelify", "PayPal", "Planning Center",
                                             "Vanco", "Subsplash") if p.lower().replace(".", "") in
                                 (subject + filename).lower().replace(".", "")), None)
                r = services.import_giving(conn, filename, data, platform, source_note=note)
                summary.giving += 1
                results.append(f"{filename}: giving export, {r['added']} new gifts")
            elif kind == "check":
                from .checks import add_batch
                r = add_batch(conn, receipts_dir, filename, data, received, note, note=purpose)
                summary.checks += 1
                results.append(f"{filename}: {_check_desc(r)}")
            elif kind == "receipt":
                r = services.add_receipt_or_check(conn, receipts_dir, filename, data, use_ai=True,
                                                  purpose=purpose, received_date=received, source=note)
                if r.get("is_check"):
                    summary.checks += 1
                    results.append(f"{filename}: {_check_desc(r)}")
                    continue
                summary.receipts += 1
                desc = "receipt" + (", matched" if r["matched"] else ", not matched yet")
                if r["error"]:
                    desc += f" (couldn't read automatically: {r['error']})"
                results.append(f"{filename}: {desc}")
            else:
                results.append(f"{filename}: skipped (unsupported file type)")
        except (ImportError_, ai.AIError, ValueError) as e:
            summary.errors.append(f"{filename} (from {sender}): {e}")
            results.append(f"{filename}: error: {e}")
    if not found:
        finish("no_attachments", "no attachments found")
    else:
        finish("error" if any(": error:" in r for r in results) else "imported", "\n".join(results))
    return True


def fetch_and_import(conn, receipts_dir, imap_factory=imaplib.IMAP4_SSL):
    c = _config()
    st = config_status()
    if not st["configured"]:
        raise MailboxError("Email import isn't set up. Missing: " + ", ".join(st["missing"]) + ". See the README.")
    summary = Summary()
    if not _lock.acquire(blocking=False):
        raise MailboxError("Already checking email; try again in a moment.")
    try:
        try:
            imap = imap_factory(c["host"], timeout=IMAP_TIMEOUT_SECONDS)
            imap.login(c["address"], c["password"])
        except imaplib.IMAP4.error:
            raise MailboxError("Gmail rejected the login. Use a Gmail app password (Google Account → Security → "
                               "2-Step Verification → App passwords), not the regular account password.")
        except OSError as e:  # includes timeouts
            raise MailboxError(f"Couldn't connect to {c['host']}: {e or 'timed out'}")
        try:
            imap.select("INBOX")
            since = (date.today() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
            typ, data = imap.search(None, "SINCE", since)
            ids = data[0].split() if typ == "OK" and data and data[0] else []
            known = {r[0] for r in conn.execute("SELECT message_id FROM email_log")}
            for num in ids:
                # Peek at headers first so already-processed mail isn't downloaded again
                typ, hdr = imap.fetch(num, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                if typ == "OK" and hdr and isinstance(hdr[0], tuple):
                    mid = email.message_from_bytes(hdr[0][1]).get("Message-ID", "").strip()
                    if mid and mid in known:
                        continue
                typ, parts = imap.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                if process_message(conn, receipts_dir, parts[0][1], c["allowed"], c["require_auth"], summary):
                    imap.store(num, "+FLAGS", "\\Seen")
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    finally:
        _lock.release()
    return summary


# ---------------------------------------------------------------- background polling / CLI

def start_poller(app):
    minutes = os.environ.get("TREASURER_EMAIL_POLL_MINUTES")
    if not minutes or not config_status()["configured"]:
        return None
    interval = max(float(minutes), 1.0) * 60

    def loop():
        from .db import connect
        while True:
            try:
                conn = connect(app.config["DATABASE"])
                try:
                    s = fetch_and_import(conn, app.config["RECEIPTS_DIR"])
                    if s.emails:
                        app.logger.info("Email check: %s", s.message())
                finally:
                    conn.close()
            except MailboxError as e:
                app.logger.warning("Email check failed: %s", e)
            except Exception:
                app.logger.exception("Email check crashed")
            time.sleep(interval)

    t = threading.Thread(target=loop, name="email-poller", daemon=True)
    t.start()
    return t


def main(argv=None):
    import argparse
    from . import create_app
    from .db import connect
    p = argparse.ArgumentParser(description="Import attachments from the treasurer email inbox.")
    p.add_argument("--watch", type=float, metavar="MINUTES", help="keep running, checking every N minutes")
    args = p.parse_args(argv)
    app = create_app({"EMAIL_POLLER": False})
    while True:
        conn = connect(app.config["DATABASE"])
        try:
            s = fetch_and_import(conn, app.config["RECEIPTS_DIR"])
            print(s.message())
            for e in s.errors:
                print("  error:", e)
        except MailboxError as e:
            print("Error:", e)
            if not args.watch:
                return 1
        finally:
            conn.close()
        if not args.watch:
            return 0
        time.sleep(max(args.watch, 1) * 60)


if __name__ == "__main__":
    raise SystemExit(main())
