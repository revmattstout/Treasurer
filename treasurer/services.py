"""Import operations shared by web uploads and the email inbox."""
from . import ai
from .db import is_rent_fund, rent_patterns
from .categorize import apply_rules
from .importers import (ImportError_, _with_occurrence, decode, parse_bank_file, parse_date,
                        parse_giving_csv)
from .receipts import auto_match, auto_match_all, mime_for, store_file


def _record_import(conn, kind, filename, note=None):
    cur = conn.execute("INSERT INTO imports(kind, filename, note) VALUES (?,?,?)", (kind, filename, note))
    return cur.lastrowid


def import_bank(conn, filename, data, account=None, source_note=None):
    """Import a bank statement. Raises ImportError_ / ai.AIError / ValueError."""
    note = None
    if filename.lower().endswith(".pdf"):
        if not ai.enabled():
            raise ImportError_("PDF statements need the Claude integration (set ANTHROPIC_API_KEY), "
                               "or download a CSV/OFX export from online banking instead.")
        stmt = ai.read_pdf_statement(data)
        records = [{"date": parse_date(t.date), "amount": round(t.amount, 2),
                    "description": t.description, "balance": t.balance,
                    "account": account or stmt.account_last4} for t in stmt.transactions]
        if stmt.ending_balance is not None and records:
            max(records, key=lambda r: r["date"])["balance"] = stmt.ending_balance
        _with_occurrence(records, lambda r: (r["date"], r["amount"], r["description"].upper()), "bank")
        note = "read from PDF by Claude — please spot-check against the statement"
    else:
        records = parse_bank_file(filename, data, account)
    note = "; ".join(x for x in (source_note, note) if x) or None
    import_id = _record_import(conn, "bank", filename, note)
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
    from .checks import match_all as match_checks
    return {"added": added, "skipped": len(records) - added,
            "categorized": apply_rules(conn), "matched": auto_match_all(conn),
            "checks_matched": match_checks(conn)}


def import_giving(conn, filename, data, platform=None, source_note=None):
    """Import an online giving CSV. Raises ImportError_."""
    records = parse_giving_csv(decode(data), platform)
    import_id = _record_import(conn, "giving", filename, source_note)
    pats = rent_patterns(conn)
    added = rent = 0
    for r in records:
        kind = "rent" if is_rent_fund(r["fund"], pats) else "gift"
        cur = conn.execute(
            "INSERT OR IGNORE INTO donations(date, gross, fee, net, donor, fund, method, platform, kind, import_id, fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (r["date"], r["gross"], r["fee"], r["net"], r["donor"], r["fund"], r["method"],
             r["platform"], kind, import_id, r["fingerprint"]))
        added += cur.rowcount
        rent += cur.rowcount if kind == "rent" else 0
    conn.execute("UPDATE imports SET rows_added=?, rows_skipped=? WHERE id=?",
                 (added, len(records) - added, import_id))
    conn.commit()
    return {"added": added, "skipped": len(records) - added, "rent": rent}


def add_receipt_or_check(conn, receipts_dir, filename, data, use_ai=True, purpose=None,
                         received_date=None, source=None):
    """Add a photo as a receipt, unless Claude sees it's a photo of checks."""
    res = add_receipt(conn, receipts_dir, filename, data, use_ai, purpose)
    if res.get("is_check"):
        from .checks import add_batch
        b = add_batch(conn, receipts_dir, filename, data, received_date, source, note=purpose)
        return {**b, "is_check": True}
    return res


def add_receipt(conn, receipts_dir, filename, data, use_ai=True, purpose=None):
    """Store a receipt, read it with Claude if enabled, and try to match it.

    Raises ImportError_ for unsupported file types. Returns a dict with the
    receipt id, whether Claude read it, any reading error, and whether it
    matched; or {"is_check": True} (nothing stored) if it's a photo of checks.
    """
    mime = mime_for(filename)
    if not mime:
        raise ImportError_("unsupported file type (use JPG, PNG, PDF, WEBP or HEIC)")
    fields = {"date": None, "amount": None, "vendor": None, "category": None, "purpose": purpose}
    read, error = False, None
    if use_ai and ai.enabled() and mime != "image/heic":
        cats = [r["name"] for r in conn.execute("SELECT name FROM categories WHERE kind='expense' ORDER BY name")]
        try:
            info = ai.read_receipt(data, mime, cats)
            if info.document_type == "check":
                return {"is_check": True}
            fields.update(vendor=info.vendor, amount=abs(info.total) if info.total is not None else None,
                          category=info.category, purpose=purpose or info.purpose)
            try:
                fields["date"] = parse_date(info.date) if info.date else None
            except ValueError:
                pass
            read = True
        except ai.AIError as e:
            error = str(e)
    stored = store_file(receipts_dir, filename, data)
    cur = conn.execute(
        "INSERT INTO receipts(original_name, stored_name, date, amount, vendor, category, purpose) "
        "VALUES (?,?,?,?,?,?,?)",
        (filename, stored, fields["date"], fields["amount"], fields["vendor"], fields["category"],
         fields["purpose"]))
    conn.commit()
    rid = cur.lastrowid
    return {"id": rid, "read": read, "error": error, "matched": bool(auto_match(conn, rid))}
