import csv
import hmac
import io
import os
import re
import time

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, send_from_directory, session, url_for)

from . import ai, services
from .categorize import apply_rules, rule_pattern_from_description
from .db import get_db, get_setting, reclassify_rent, set_setting
from .importers import ImportError_, parse_date, parse_amount
from .receipts import auto_match, candidates
from .reports import available_months, build_report, report_csv_text, report_for_ai

bp = Blueprint("main", __name__)
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


@bp.app_context_processor
def inject_globals():
    conn = get_db()
    return {"org_name": get_setting(conn, "org_name", "VSU Wesley Foundation"),
            "ai_enabled": ai.enabled(),
            "login_required": bool(current_app.config.get("APP_PASSWORD"))}


def _categories(conn):
    return conn.execute(
        "SELECT * FROM categories ORDER BY CASE kind WHEN 'income' THEN 0 WHEN 'payout' THEN 1 "
        "WHEN 'transfer' THEN 2 ELSE 3 END, name").fetchall()


def _back():
    b = request.form.get("back") or ""
    return b if b.startswith("/") and not b.startswith("//") else None


def _check_month(month):
    if not MONTH_RE.match(month or ""):
        abort(404)
    return month


# ---------------------------------------------------------------- auth

_failed_logins = {}  # ip -> list of failure timestamps
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_FAILURES = 10


def _too_many_failures(ip):
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    _failed_logins[ip] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if _too_many_failures(ip):
            flash("Too many incorrect attempts. Wait 15 minutes and try again.", "error")
            return render_template("login.html"), 429
        pw = current_app.config.get("APP_PASSWORD") or ""
        if pw and hmac.compare_digest(request.form.get("password", "").encode(), pw.encode()):
            _failed_logins.pop(ip, None)
            session.clear()
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.index"))
        _failed_logins.setdefault(ip, []).append(time.time())
        flash("Incorrect password.", "error")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("main.login"))


# ---------------------------------------------------------------- dashboard

@bp.route("/")
def index():
    conn = get_db()
    months = available_months(conn)
    latest = build_report(conn, months[0]) if months else None
    counts = {
        "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
        "uncategorized": conn.execute("SELECT COUNT(*) FROM transactions WHERE category IS NULL").fetchone()[0],
        "donations": conn.execute("SELECT COUNT(*) FROM donations").fetchone()[0],
        "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
        "unmatched_receipts": conn.execute("SELECT COUNT(*) FROM receipts WHERE transaction_id IS NULL").fetchone()[0],
    }
    imports = conn.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 8").fetchall()
    return render_template("index.html", months=months, latest=latest, counts=counts, imports=imports)


# ---------------------------------------------------------------- uploads

@bp.route("/upload")
def upload():
    from . import mailbox
    conn = get_db()
    return render_template("upload.html", mail=mailbox.config_status(),
                           mail_log=conn.execute("SELECT * FROM email_log ORDER BY id DESC LIMIT 15").fetchall())


@bp.route("/upload/bank", methods=["POST"])
def upload_bank():
    conn = get_db()
    account = request.form.get("account", "").strip() or None
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    for f in files:
        try:
            res = services.import_bank(conn, f.filename, f.read(), account)
        except (ImportError_, ai.AIError, ValueError) as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        msg = f"{f.filename}: imported {res['added']} transactions"
        if res["skipped"]:
            msg += f" ({res['skipped']} already imported, skipped)"
        msg += f"; {res['categorized']} auto-categorized"
        if res["matched"]:
            msg += f"; {res['matched']} receipts matched"
        if res.get("checks_matched"):
            msg += f"; {res['checks_matched']} checks matched to deposits"
        flash(msg + ".", "success")
    return redirect(url_for("main.transactions", filter="uncategorized"))


@bp.route("/upload/giving", methods=["POST"])
def upload_giving():
    conn = get_db()
    platform = request.form.get("platform", "").strip() or None
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    for f in files:
        try:
            res = services.import_giving(conn, f.filename, f.read(), platform)
        except ImportError_ as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        flash(f"{f.filename}: imported {res['added'] - res['rent']} gifts"
              + (f" and {res['rent']} rent payments" if res["rent"] else "")
              + (f" ({res['skipped']} already imported, skipped)" if res["skipped"] else "") + ".",
              "success")
    return redirect(url_for("main.donations"))


@bp.route("/upload/receipts", methods=["POST"])
def upload_receipts():
    conn = get_db()
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    use_ai = request.form.get("read_with_ai", "on") == "on"
    added = read = matched = 0
    for f in files:
        try:
            res = services.add_receipt_or_check(conn, current_app.config["RECEIPTS_DIR"], f.filename, f.read(),
                                                use_ai, source="uploaded")
        except ImportError_ as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        if res.get("is_check"):
            flash(f"{f.filename} looks like a photo of checks, so it was added under Checks instead.", "success")
            continue
        added += 1
        read += res["read"]
        matched += res["matched"]
        if res["error"]:
            flash(f"{f.filename}: couldn't read automatically ({res['error']}). Fill it in by hand below.", "error")
    if added:
        msg = f"Uploaded {added} receipt(s)"
        if read:
            msg += f"; Claude read {read}"
        msg += f"; {matched} matched to bank transactions."
        flash(msg, "success")
    return redirect(url_for("main.receipts", filter="unmatched"))


@bp.route("/upload/checks", methods=["POST"])
def upload_checks():
    from .checks import add_batch
    conn = get_db()
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    try:
        deposited = parse_date(request.form["deposited"]) if request.form.get("deposited") else None
    except ValueError:
        deposited = None
    for f in files:
        try:
            r = add_batch(conn, current_app.config["RECEIPTS_DIR"], f.filename, f.read(), deposited, "uploaded")
        except ValueError as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        msg = f"{f.filename}: read {r['checks']} check(s)"
        msg += "; matched to a bank deposit." if r["matched"] else "; the deposit isn't on a bank statement yet."
        flash(msg, "success")
        if r["error"]:
            flash(f"{f.filename}: {r['error']}", "error")
    return redirect(url_for("main.checks_page"))


@bp.route("/checks")
def checks_page():
    from .checks import deposit_candidates
    conn = get_db()
    flt = request.args.get("filter") or ""
    sql = "SELECT * FROM check_batches"
    if flt == "unmatched":
        sql += (" WHERE id IN (SELECT batch_id FROM checks WHERE transaction_id IS NULL) "
                "OR id NOT IN (SELECT batch_id FROM checks)")
    batches = conn.execute(sql + " ORDER BY received_date DESC, id DESC LIMIT 200").fetchall()
    items = {}
    for c in conn.execute(
            "SELECT c.*, t.date AS tx_date, t.description AS tx_description, t.amount AS tx_amount "
            "FROM checks c LEFT JOIN transactions t ON t.id = c.transaction_id ORDER BY c.id"):
        items.setdefault(c["batch_id"], []).append(c)
    cands = {b["id"]: deposit_candidates(conn, b["id"]) for b in batches
             if any(c["transaction_id"] is None for c in items.get(b["id"], []))}
    return render_template("checks.html", batches=batches, items=items, cands=cands, flt=flt)


@bp.route("/checks/<int:check_id>", methods=["POST"])
def update_check(check_id):
    from .checks import match_batch
    conn = get_db()
    f = request.form
    row = conn.execute("SELECT batch_id FROM checks WHERE id=?", (check_id,)).fetchone()
    if not row:
        abort(404)
    if f.get("delete"):
        conn.execute("DELETE FROM checks WHERE id=?", (check_id,))
        conn.commit()
        flash("Check removed.", "success")
        return redirect(_back() or url_for("main.checks_page"))
    try:
        amt = parse_amount(f.get("amount"))
        d = parse_date(f["check_date"]) if f.get("check_date") else None
    except ValueError as e:
        flash(str(e), "error")
        return redirect(_back() or url_for("main.checks_page"))
    kind = f.get("kind") if f.get("kind") in ("gift", "rent", "other") else "gift"
    conn.execute("UPDATE checks SET payer=?, amount=?, check_date=?, check_number=?, memo=?, kind=? WHERE id=?",
                 (f.get("payer") or None, abs(amt) if amt is not None else None, d,
                  f.get("check_number") or None, f.get("memo") or None, kind, check_id))
    conn.commit()
    match_batch(conn, row["batch_id"])
    flash("Check saved.", "success")
    return redirect(_back() or url_for("main.checks_page"))


@bp.route("/checks/batch/<int:batch_id>/add", methods=["POST"])
def add_check_line(batch_id):
    from .checks import match_batch
    conn = get_db()
    f = request.form
    try:
        amt = parse_amount(f.get("amount"))
    except ValueError as e:
        flash(str(e), "error")
        return redirect(_back() or url_for("main.checks_page"))
    kind = f.get("kind") if f.get("kind") in ("gift", "rent", "other") else "gift"
    conn.execute("INSERT INTO checks(batch_id, payer, amount, check_number, memo, kind) VALUES (?,?,?,?,?,?)",
                 (batch_id, f.get("payer") or None, abs(amt) if amt is not None else None,
                  f.get("check_number") or None, f.get("memo") or None, kind))
    conn.commit()
    match_batch(conn, batch_id)
    flash("Check added.", "success")
    return redirect(_back() or url_for("main.checks_page"))


@bp.route("/checks/batch/<int:batch_id>/match", methods=["POST"])
def match_check_batch(batch_id):
    conn = get_db()
    tx_id = request.form.get("transaction_id", type=int)
    if tx_id:
        from .checks import _link
        tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
        ids = [r[0] for r in conn.execute("SELECT id FROM checks WHERE batch_id=?", (batch_id,))]
        if tx and ids:
            _link(conn, ids, tx)
            flash("Matched to the deposit.", "success")
    else:
        conn.execute("UPDATE checks SET transaction_id=NULL WHERE batch_id=?", (batch_id,))
        flash("Unmatched.", "success")
    conn.commit()
    return redirect(_back() or url_for("main.checks_page"))


@bp.route("/checks/batch/<int:batch_id>/file")
def check_file(batch_id):
    r = get_db().execute("SELECT stored_name FROM check_batches WHERE id=?", (batch_id,)).fetchone()
    if not r:
        abort(404)
    return send_from_directory(current_app.config["RECEIPTS_DIR"], r["stored_name"])


@bp.route("/checks/batch/<int:batch_id>/delete", methods=["POST"])
def delete_check_batch(batch_id):
    conn = get_db()
    r = conn.execute("SELECT stored_name FROM check_batches WHERE id=?", (batch_id,)).fetchone()
    if r:
        try:
            os.remove(os.path.join(current_app.config["RECEIPTS_DIR"], r["stored_name"]))
        except FileNotFoundError:
            pass
        conn.execute("DELETE FROM checks WHERE batch_id=?", (batch_id,))
        conn.execute("DELETE FROM check_batches WHERE id=?", (batch_id,))
        conn.commit()
        flash("Check photo deleted.", "success")
    return redirect(url_for("main.checks_page"))


@bp.route("/upload/check-email", methods=["POST"])
def check_email():
    from . import mailbox
    conn = get_db()
    try:
        summary = mailbox.fetch_and_import(conn, current_app.config["RECEIPTS_DIR"])
    except mailbox.MailboxError as e:
        flash(str(e), "error")
        return redirect(url_for("main.upload"))
    flash(summary.message(), "success" if not summary.errors else "error")
    for err in summary.errors[:10]:
        flash(err, "error")
    return redirect(url_for("main.upload"))


# ---------------------------------------------------------------- transactions

@bp.route("/transactions")
def transactions():
    conn = get_db()
    month = request.args.get("month") or ""
    flt = request.args.get("filter") or ""
    q = request.args.get("q", "").strip()
    sql = ("SELECT t.*, r.id AS receipt_id, "
           "(SELECT COUNT(*) FROM checks c WHERE c.transaction_id = t.id) AS check_count FROM transactions t "
           "LEFT JOIN receipts r ON r.transaction_id = t.id WHERE 1=1")
    params = []
    if MONTH_RE.match(month):
        sql += " AND substr(t.date,1,7)=?"
        params.append(month)
    if flt == "uncategorized":
        sql += " AND t.category IS NULL"
    elif flt == "no_receipt":
        sql += " AND r.id IS NULL AND t.amount < 0"
    if q:
        sql += " AND t.description LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY t.date DESC, t.id DESC LIMIT 1000"
    rows = conn.execute(sql, params).fetchall()
    return render_template("transactions.html", rows=rows, categories=_categories(conn),
                           months=available_months(conn), month=month, flt=flt, q=q)


@bp.route("/transactions/<int:tx_id>/category", methods=["POST"])
def set_category(tx_id):
    conn = get_db()
    cat = request.form.get("category") or None
    tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        abort(404)
    conn.execute("UPDATE transactions SET category=?, category_source=? WHERE id=?",
                 (cat, "manual" if cat else None, tx_id))
    if cat and request.form.get("remember"):
        pattern = rule_pattern_from_description(tx["description"])
        conn.execute("INSERT INTO rules(pattern, category, priority) VALUES (?,?,?)", (pattern, cat, 10))
        conn.commit()
        n = apply_rules(conn)
        flash(f"Saved rule “{pattern}” → {cat}; applied to {n} more transaction(s).", "success")
    conn.commit()
    return redirect(_back() or url_for("main.transactions"))


@bp.route("/transactions/<int:tx_id>/notes", methods=["POST"])
def set_notes(tx_id):
    conn = get_db()
    conn.execute("UPDATE transactions SET notes=? WHERE id=?", (request.form.get("notes") or None, tx_id))
    conn.commit()
    return redirect(_back() or url_for("main.transactions"))


@bp.route("/transactions/apply-rules", methods=["POST"])
def run_rules():
    n = apply_rules(get_db(), only_uncategorized=not request.form.get("all"))
    flash(f"Rules categorized {n} transaction(s).", "success")
    return redirect(_back() or url_for("main.transactions"))


@bp.route("/transactions/suggest", methods=["POST"])
def suggest():
    conn = get_db()
    if not ai.enabled():
        flash("Set ANTHROPIC_API_KEY to use Claude suggestions.", "error")
        return redirect(url_for("main.transactions"))
    rows = conn.execute("SELECT id, date, description, amount FROM transactions WHERE category IS NULL "
                        "ORDER BY date DESC LIMIT 200").fetchall()
    if not rows:
        flash("Nothing to categorize.", "success")
        return redirect(url_for("main.transactions"))
    cats = _categories(conn)
    income = [c["name"] for c in cats if c["kind"] in ("income", "payout", "transfer")]
    expense = [c["name"] for c in cats if c["kind"] in ("expense", "transfer")]
    try:
        result = ai.suggest_categories([dict(r) for r in rows], income, expense)
    except ai.AIError as e:
        flash(str(e), "error")
        return redirect(url_for("main.transactions"))
    for tx_id, cat in result.items():
        conn.execute("UPDATE transactions SET category=?, category_source='ai' WHERE id=? AND category IS NULL",
                     (cat, tx_id))
    conn.commit()
    flash(f"Claude suggested categories for {len(result)} of {len(rows)} transactions. "
          "They're marked “AI” — please review them.", "success")
    return redirect(_back() or url_for("main.transactions"))


# ---------------------------------------------------------------- donations

@bp.route("/donations")
def donations():
    conn = get_db()
    month = request.args.get("month") or ""
    kind = request.args.get("kind") if request.args.get("kind") in ("gift", "rent") else ""
    sql, params = "SELECT * FROM donations WHERE 1=1", []
    if MONTH_RE.match(month):
        sql += " AND substr(date,1,7)=?"
        params.append(month)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    rows = conn.execute(sql + " ORDER BY date DESC, id DESC LIMIT 2000", params).fetchall()
    return render_template("donations.html", rows=rows, months=available_months(conn), month=month, kind=kind)


# ---------------------------------------------------------------- receipts

@bp.route("/receipts")
def receipts():
    conn = get_db()
    flt = request.args.get("filter") or ""
    sql = ("SELECT r.*, t.date AS tx_date, t.description AS tx_description, t.amount AS tx_amount "
           "FROM receipts r LEFT JOIN transactions t ON t.id = r.transaction_id")
    if flt == "unmatched":
        sql += " WHERE r.transaction_id IS NULL"
    rows = conn.execute(sql + " ORDER BY COALESCE(r.date, r.uploaded_at) DESC, r.id DESC").fetchall()
    cands = {r["id"]: candidates(conn, r["amount"], r["date"]) for r in rows if not r["transaction_id"]}
    return render_template("receipts.html", rows=rows, cands=cands, flt=flt,
                           categories=[c["name"] for c in _categories(conn) if c["kind"] == "expense"])


@bp.route("/receipts/<int:rid>/file")
def receipt_file(rid):
    r = get_db().execute("SELECT stored_name FROM receipts WHERE id=?", (rid,)).fetchone()
    if not r:
        abort(404)
    return send_from_directory(current_app.config["RECEIPTS_DIR"], r["stored_name"])


@bp.route("/receipts/<int:rid>", methods=["POST"])
def update_receipt(rid):
    conn = get_db()
    f = request.form
    try:
        d = parse_date(f["date"]) if f.get("date") else None
        amt = abs(parse_amount(f.get("amount")) or 0) or None
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("main.receipts"))
    conn.execute("UPDATE receipts SET date=?, amount=?, vendor=?, category=?, purpose=? WHERE id=?",
                 (d, amt, f.get("vendor") or None, f.get("category") or None, f.get("purpose") or None, rid))
    conn.commit()
    if auto_match(conn, rid):
        flash("Receipt saved and matched to a bank transaction.", "success")
    else:
        flash("Receipt saved.", "success")
    return redirect(_back() or url_for("main.receipts"))


@bp.route("/receipts/<int:rid>/match", methods=["POST"])
def match_receipt(rid):
    conn = get_db()
    tx_id = request.form.get("transaction_id", type=int)
    if tx_id:
        conn.execute("UPDATE receipts SET transaction_id=NULL WHERE transaction_id=?", (tx_id,))
    conn.execute("UPDATE receipts SET transaction_id=? WHERE id=?", (tx_id, rid))
    r = conn.execute("SELECT category FROM receipts WHERE id=?", (rid,)).fetchone()
    if tx_id and r and r["category"]:
        conn.execute("UPDATE transactions SET category=?, category_source='manual' "
                     "WHERE id=? AND category IS NULL", (r["category"], tx_id))
    conn.commit()
    flash("Receipt matched." if tx_id else "Receipt unmatched.", "success")
    return redirect(_back() or url_for("main.receipts"))


@bp.route("/receipts/<int:rid>/delete", methods=["POST"])
def delete_receipt(rid):
    conn = get_db()
    r = conn.execute("SELECT stored_name FROM receipts WHERE id=?", (rid,)).fetchone()
    if r:
        try:
            os.remove(os.path.join(current_app.config["RECEIPTS_DIR"], r["stored_name"]))
        except FileNotFoundError:
            pass
        conn.execute("DELETE FROM receipts WHERE id=?", (rid,))
        conn.commit()
        flash("Receipt deleted.", "success")
    return redirect(url_for("main.receipts"))


# ---------------------------------------------------------------- reports

@bp.route("/reports")
def reports():
    months = available_months(get_db())
    if months:
        return redirect(url_for("main.report", month=months[0]))
    flash("Upload a bank statement or giving records first.", "error")
    return redirect(url_for("main.upload"))


@bp.route("/reports/<month>")
def report(month):
    conn = get_db()
    _check_month(month)
    rep = build_report(conn, month)
    narrative = conn.execute("SELECT * FROM narratives WHERE month=?", (month,)).fetchone()
    return render_template("report.html", r=rep, months=available_months(conn), narrative=narrative,
                           rent_funds=get_setting(conn, "rent_funds", "Rent"))


@bp.route("/reports/<month>/narrative", methods=["POST"])
def report_narrative(month):
    conn = get_db()
    _check_month(month)
    if request.form.get("text") is not None:
        text = request.form["text"].strip()
        if text:
            conn.execute("INSERT INTO narratives(month, text) VALUES (?,?) ON CONFLICT(month) "
                         "DO UPDATE SET text=excluded.text, created_at=datetime('now')", (month, text))
        else:
            conn.execute("DELETE FROM narratives WHERE month=?", (month,))
        conn.commit()
        flash("Narrative saved.", "success")
        return redirect(url_for("main.report", month=month))
    if not ai.enabled():
        flash("Set ANTHROPIC_API_KEY to have Claude draft a narrative, or write one yourself.", "error")
        return redirect(url_for("main.report", month=month))
    try:
        text = ai.write_narrative(report_for_ai(build_report(conn, month)), get_setting(conn, "org_name"))
    except ai.AIError as e:
        flash(str(e), "error")
        return redirect(url_for("main.report", month=month))
    conn.execute("INSERT INTO narratives(month, text) VALUES (?,?) ON CONFLICT(month) "
                 "DO UPDATE SET text=excluded.text, created_at=datetime('now')", (month, text))
    conn.commit()
    flash("Claude drafted a narrative. Review and edit it before sharing.", "success")
    return redirect(url_for("main.report", month=month))


@bp.route("/reports/<month>/email", methods=["POST"])
def report_email(month):
    from . import mailer
    conn = get_db()
    _check_month(month)
    try:
        mailer.send_report(current_app._get_current_object(), conn, month)
    except mailer.MailerError as e:
        flash(f"Couldn't email the report: {e}", "error")
    else:
        flash(f"Emailed the {month} report to {', '.join(mailer.recipients(conn))}.", "success")
    return redirect(url_for("main.report", month=month))


@bp.route("/reports/<month>.csv")
def report_csv(month):
    conn = get_db()
    _check_month(month)
    return Response(report_csv_text(conn, month), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=wesley-report-{month}.csv"})



# ---------------------------------------------------------------- settings

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "general":
            set_setting(conn, "org_name", request.form.get("org_name", "").strip() or "VSU Wesley Foundation")
            fy = request.form.get("fiscal_year_start_month", type=int) or 1
            set_setting(conn, "fiscal_year_start_month", str(min(max(fy, 1), 12)))
            thr = request.form.get("receipt_required_over", type=float)
            set_setting(conn, "receipt_required_over", str(thr if thr is not None else 25))
            flash("Settings saved.", "success")
        elif action == "report_email":
            raw = request.form.get("report_recipients", "")
            addrs = [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]
            bad = [a for a in addrs if not re.fullmatch(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+", a)]
            if bad:
                flash("Not a valid email address: " + ", ".join(bad), "error")
                return redirect(url_for("main.settings"))
            set_setting(conn, "report_recipients", ", ".join(addrs))
            set_setting(conn, "report_schedule_enabled", "1" if request.form.get("enabled") else "0")
            hour = request.form.get("report_send_hour", type=int)
            set_setting(conn, "report_send_hour", str(min(max(hour if hour is not None else 8, 0), 23)))
            flash("Report email settings saved.", "success")
        elif action == "rent_funds":
            set_setting(conn, "rent_funds", request.form.get("rent_funds", "").strip())
            reclassify_rent(conn)
            n = conn.execute("SELECT COUNT(*) FROM donations WHERE kind='rent'").fetchone()[0]
            flash(f"Saved. {n} online payment(s) are now counted as rent.", "success")
        elif action == "budgets":
            for c in _categories(conn):
                v = request.form.get(f"budget_{c['name']}", "").strip()
                try:
                    b = parse_amount(v) if v else None
                except ValueError:
                    b = None
                conn.execute("UPDATE categories SET monthly_budget=? WHERE name=?", (b, c["name"]))
            flash("Budgets saved.", "success")
        elif action == "add_category":
            name = request.form.get("name", "").strip()
            kind = request.form.get("kind")
            if name and kind in ("income", "expense", "payout", "transfer"):
                conn.execute("INSERT OR IGNORE INTO categories(name, kind) VALUES (?,?)", (name, kind))
                flash(f"Added category “{name}”.", "success")
        elif action == "rename_category":
            old, new = request.form.get("old"), request.form.get("new", "").strip()
            if old and new and old != new:
                conn.execute("UPDATE categories SET name=? WHERE name=?", (new, old))
                conn.execute("UPDATE receipts SET category=? WHERE category=?", (new, old))
                flash(f"Renamed “{old}” to “{new}”.", "success")
        elif action == "delete_category":
            name = request.form.get("name")
            used = conn.execute("SELECT COUNT(*) FROM transactions WHERE category=?", (name,)).fetchone()[0]
            if used:
                flash(f"“{name}” is used by {used} transaction(s); recategorize them first.", "error")
            else:
                conn.execute("DELETE FROM rules WHERE category=?", (name,))
                conn.execute("DELETE FROM categories WHERE name=?", (name,))
                flash(f"Deleted “{name}”.", "success")
        elif action == "add_rule":
            pattern = request.form.get("pattern", "").strip()
            cat = request.form.get("category")
            if pattern and cat:
                conn.execute("INSERT INTO rules(pattern, category, priority) VALUES (?,?,?)",
                             (pattern, cat, 10))
                conn.commit()
                n = apply_rules(conn)
                flash(f"Added rule; categorized {n} transaction(s).", "success")
        elif action == "delete_rule":
            conn.execute("DELETE FROM rules WHERE id=?", (request.form.get("id", type=int),))
            flash("Rule deleted.", "success")
        conn.commit()
        return redirect(url_for("main.settings"))
    from . import mailer
    rules = conn.execute("SELECT * FROM rules ORDER BY category, pattern").fetchall()
    st = {k: get_setting(conn, k) for k in ("org_name", "fiscal_year_start_month", "receipt_required_over",
                                            "report_recipients", "report_schedule_enabled", "report_send_hour",
                                            "rent_funds")}
    next_date, next_month = mailer.next_send(conn)
    sends = conn.execute("SELECT * FROM report_sends ORDER BY id DESC LIMIT 12").fetchall()
    return render_template("settings.html", categories=_categories(conn), rules=rules, st=st,
                           mail_ok=mailer.configured(), next_date=next_date, next_month=next_month, sends=sends)


# ---------------------------------------------------------------- import history

@bp.route("/imports")
def imports():
    rows = get_db().execute("SELECT * FROM imports ORDER BY id DESC").fetchall()
    return render_template("imports.html", rows=rows)


@bp.route("/imports/<int:import_id>/undo", methods=["POST"])
def undo_import(import_id):
    conn = get_db()
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if not imp:
        abort(404)
    if imp["kind"] == "bank":
        conn.execute("UPDATE receipts SET transaction_id=NULL WHERE transaction_id IN "
                     "(SELECT id FROM transactions WHERE import_id=?)", (import_id,))
        n = conn.execute("DELETE FROM transactions WHERE import_id=?", (import_id,)).rowcount
    else:
        n = conn.execute("DELETE FROM donations WHERE import_id=?", (import_id,)).rowcount
    conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
    conn.commit()
    flash(f"Removed {n} row(s) from “{imp['filename']}”.", "success")
    return redirect(url_for("main.imports"))
