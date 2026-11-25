import csv
import hmac
import io
import os
import re

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template,
                   request, send_from_directory, session, url_for)

from . import ai
from .categorize import apply_rules, rule_pattern_from_description
from .db import get_db, get_setting, set_setting
from .importers import ImportError_, parse_bank_file, parse_date, parse_amount, parse_giving_csv, decode
from .receipts import auto_match, auto_match_all, candidates, mime_for, store_file
from .reports import available_months, build_report, report_for_ai

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

@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = current_app.config.get("APP_PASSWORD") or ""
        if hmac.compare_digest(request.form.get("password", ""), pw):
            session["ok"] = True
            nxt = request.args.get("next") or ""
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("main.index"))
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
    return render_template("upload.html")


def _record_import(conn, kind, filename, added, skipped, note=None):
    cur = conn.execute("INSERT INTO imports(kind, filename, rows_added, rows_skipped, note) VALUES (?,?,?,?,?)",
                       (kind, filename, added, skipped, note))
    return cur.lastrowid


@bp.route("/upload/bank", methods=["POST"])
def upload_bank():
    conn = get_db()
    account = request.form.get("account", "").strip() or None
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    for f in files:
        data = f.read()
        try:
            if f.filename.lower().endswith(".pdf"):
                if not ai.enabled():
                    raise ImportError_("PDF statements need the Claude integration (set ANTHROPIC_API_KEY), "
                                       "or download a CSV/OFX export from online banking instead.")
                stmt = ai.read_pdf_statement(data)
                from .importers import _with_occurrence
                records = [{"date": parse_date(t.date), "amount": round(t.amount, 2),
                            "description": t.description, "balance": t.balance,
                            "account": account or stmt.account_last4} for t in stmt.transactions]
                if stmt.ending_balance is not None and records:
                    max(records, key=lambda r: r["date"])["balance"] = stmt.ending_balance
                _with_occurrence(records, lambda r: (r["date"], r["amount"], r["description"].upper()), "bank")
                note = "read from PDF by Claude — please spot-check against the statement"
            else:
                records = parse_bank_file(f.filename, data, account)
                note = None
        except (ImportError_, ai.AIError, ValueError) as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        import_id = _record_import(conn, "bank", f.filename, 0, 0, note)
        added = 0
        for r in records:
            cur = conn.execute(
                "INSERT OR IGNORE INTO transactions(date, amount, description, balance, account, import_id, fingerprint) "
                "VALUES (?,?,?,?,?,?,?)",
                (r["date"], r["amount"], r["description"], r["balance"], r["account"], import_id, r["fingerprint"]))
            added += cur.rowcount
        conn.execute("UPDATE imports SET rows_added=?, rows_skipped=? WHERE id=?",
                     (added, len(records) - added, import_id))
        conn.commit()
        categorized = apply_rules(conn)
        matched = auto_match_all(conn)
        msg = f"{f.filename}: imported {added} transactions"
        if len(records) - added:
            msg += f" ({len(records) - added} already imported, skipped)"
        msg += f"; {categorized} auto-categorized"
        if matched:
            msg += f"; {matched} receipts matched"
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
            records = parse_giving_csv(decode(f.read()), platform)
        except ImportError_ as e:
            flash(f"{f.filename}: {e}", "error")
            continue
        import_id = _record_import(conn, "giving", f.filename, 0, 0)
        added = 0
        for r in records:
            cur = conn.execute(
                "INSERT OR IGNORE INTO donations(date, gross, fee, net, donor, fund, method, platform, import_id, fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["date"], r["gross"], r["fee"], r["net"], r["donor"], r["fund"], r["method"],
                 r["platform"], import_id, r["fingerprint"]))
            added += cur.rowcount
        conn.execute("UPDATE imports SET rows_added=?, rows_skipped=? WHERE id=?",
                     (added, len(records) - added, import_id))
        conn.commit()
        flash(f"{f.filename}: imported {added} gifts"
              + (f" ({len(records) - added} already imported, skipped)" if len(records) - added else "") + ".",
              "success")
    return redirect(url_for("main.donations"))


@bp.route("/upload/receipts", methods=["POST"])
def upload_receipts():
    conn = get_db()
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("Choose at least one file.", "error")
        return redirect(url_for("main.upload"))
    cats = [c["name"] for c in _categories(conn) if c["kind"] == "expense"]
    use_ai = ai.enabled() and request.form.get("read_with_ai", "on") == "on"
    added = read = matched = 0
    for f in files:
        mime = mime_for(f.filename)
        if not mime:
            flash(f"{f.filename}: unsupported file type (use JPG, PNG, PDF, WEBP or HEIC).", "error")
            continue
        data = f.read()
        stored = store_file(current_app.config["RECEIPTS_DIR"], f.filename, data)
        fields = {"date": None, "amount": None, "vendor": None, "category": None, "purpose": None}
        if use_ai and mime != "image/heic":
            try:
                info = ai.read_receipt(data, mime, cats)
                fields.update(vendor=info.vendor, amount=abs(info.total) if info.total is not None else None,
                              category=info.category, purpose=info.purpose)
                try:
                    fields["date"] = parse_date(info.date) if info.date else None
                except ValueError:
                    pass
                read += 1
            except ai.AIError as e:
                flash(f"{f.filename}: couldn't read automatically ({e}). Fill it in by hand below.", "error")
        cur = conn.execute(
            "INSERT INTO receipts(original_name, stored_name, date, amount, vendor, category, purpose) "
            "VALUES (?,?,?,?,?,?,?)",
            (f.filename, stored, fields["date"], fields["amount"], fields["vendor"], fields["category"],
             fields["purpose"]))
        conn.commit()
        added += 1
        if auto_match(conn, cur.lastrowid):
            matched += 1
    if added:
        msg = f"Uploaded {added} receipt(s)"
        if read:
            msg += f"; Claude read {read}"
        msg += f"; {matched} matched to bank transactions."
        flash(msg, "success")
    return redirect(url_for("main.receipts", filter="unmatched"))


# ---------------------------------------------------------------- transactions

@bp.route("/transactions")
def transactions():
    conn = get_db()
    month = request.args.get("month") or ""
    flt = request.args.get("filter") or ""
    q = request.args.get("q", "").strip()
    sql = ("SELECT t.*, r.id AS receipt_id FROM transactions t "
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
    sql, params = "SELECT * FROM donations", []
    if MONTH_RE.match(month):
        sql += " WHERE substr(date,1,7)=?"
        params.append(month)
    rows = conn.execute(sql + " ORDER BY date DESC, id DESC LIMIT 2000", params).fetchall()
    return render_template("donations.html", rows=rows, months=available_months(conn), month=month)


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
    return render_template("report.html", r=rep, months=available_months(conn), narrative=narrative)


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


@bp.route("/reports/<month>.csv")
def report_csv(month):
    conn = get_db()
    _check_month(month)
    rep = build_report(conn, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{get_setting(conn, 'org_name')} — Monthly Report", month])
    w.writerow([])
    w.writerow(["Section", "Line", "This month", "Prior month", "Fiscal YTD", "Monthly budget"])
    for f in rep["giving"]["by_fund"]:
        w.writerow(["Income: online giving", f["name"], f["amount"], f["prior"], f["ytd"], ""])
    for l in rep["other_income"]:
        w.writerow(["Income: other", l["name"], l["amount"], l["prior"], l["ytd"], l["budget"] or ""])
    for l in rep["expenses"]:
        w.writerow(["Expense", l["name"], l["amount"], l["prior"], l["ytd"], l["budget"] or ""])
    s = rep["summary"]
    w.writerow(["Total", "Income", s["income"], s["prior_income"], s["ytd_income"], ""])
    w.writerow(["Total", "Expenses", s["expense"], s["prior_expense"], s["ytd_expense"], ""])
    w.writerow(["Total", "Net", s["net"], s["prior_net"], s["ytd_net"], ""])
    w.writerow([])
    w.writerow(["Transactions"])
    w.writerow(["Date", "Description", "Amount", "Category", "Receipt", "Notes"])
    for t in conn.execute(
            "SELECT t.*, r.id rid FROM transactions t LEFT JOIN receipts r ON r.transaction_id=t.id "
            "WHERE substr(t.date,1,7)=? ORDER BY t.date, t.id", (month,)):
        w.writerow([t["date"], t["description"], t["amount"], t["category"] or "Uncategorized",
                    "yes" if t["rid"] else "", t["notes"] or ""])
    return Response(buf.getvalue(), mimetype="text/csv",
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
    rules = conn.execute("SELECT * FROM rules ORDER BY category, pattern").fetchall()
    st = {k: get_setting(conn, k) for k in ("org_name", "fiscal_year_start_month", "receipt_required_over")}
    return render_template("settings.html", categories=_categories(conn), rules=rules, st=st)


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
