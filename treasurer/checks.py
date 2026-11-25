"""Photos of deposited checks: who each check is from, matched to the bank deposit.

The bank statement only says "MOBILE DEPOSIT $350.00". A photo of the checks
that went into that deposit tells us who gave. Each photo (a "batch") is
read by Claude into one row per check, then matched to a bank deposit:

  1. one deposit equal to the whole batch's total (several checks deposited
     together), or else
  2. one deposit per check with the same amount,

within a window from a few days before the earliest check date to ten days
after the photo was sent. Matching runs again whenever a bank statement is
imported, so the photo can arrive before or after the statement.

Checks whose memo mentions rent (or whose writer already pays rent online)
are counted as Student Housing Rent rather than gifts.
"""
from datetime import date, timedelta

from . import ai
from .db import RENT_CATEGORY, is_rent_fund, rent_patterns
from .receipts import mime_for, store_file

GIFT_CATEGORY = "Donations - Cash & Check"
# Deposits in these categories (or uncategorized) are split by their checks
SPLITTABLE = (None, GIFT_CATEGORY, RENT_CATEGORY)
WINDOW_BEFORE_DAYS = 5
WINDOW_AFTER_DAYS = 10


def guess_kind(conn, payer, memo):
    pats = rent_patterns(conn)
    if memo and is_rent_fund(memo, pats):
        return "rent"
    if payer and conn.execute(
            "SELECT 1 FROM donations WHERE kind='rent' AND lower(donor)=lower(?) LIMIT 1", (payer,)).fetchone():
        return "rent"
    return "gift"


def add_batch(conn, folder, filename, data, received_date=None, source=None, note=None, use_ai=True):
    """Store a check photo, read the checks on it, and try to match it.
    Returns dict(batch_id, checks, matched, error)."""
    mime = mime_for(filename)
    if not mime:
        raise ValueError("unsupported file type (use JPG, PNG, PDF, WEBP or HEIC)")
    stored = store_file(folder, filename, data)
    received_date = received_date or date.today().isoformat()
    items, error = [], None
    if use_ai and ai.enabled() and mime != "image/heic":
        try:
            items = ai.read_checks(data, mime).checks
        except ai.AIError as e:
            error = str(e)
    elif not ai.enabled():
        error = "Claude isn't turned on, so enter the checks by hand."
    cur = conn.execute(
        "INSERT INTO check_batches(original_name, stored_name, received_date, source, note, read_error) "
        "VALUES (?,?,?,?,?,?)", (filename, stored, received_date, source, note, error))
    batch_id = cur.lastrowid
    from .importers import parse_date
    for c in items:
        try:
            d = parse_date(c.date) if c.date else None
        except ValueError:
            d = None
        conn.execute(
            "INSERT INTO checks(batch_id, payer, amount, check_date, check_number, memo, kind) VALUES (?,?,?,?,?,?,?)",
            (batch_id, c.payer, round(c.amount, 2) if c.amount is not None else None, d, c.check_number,
             c.memo, guess_kind(conn, c.payer, c.memo)))
    conn.commit()
    matched = match_batch(conn, batch_id)
    return {"batch_id": batch_id, "checks": len(items), "matched": matched, "error": error}


def _window(conn, batch_id):
    b = conn.execute("SELECT received_date FROM check_batches WHERE id=?", (batch_id,)).fetchone()
    dates = [r[0] for r in conn.execute(
        "SELECT check_date FROM checks WHERE batch_id=? AND check_date IS NOT NULL", (batch_id,))]
    received = date.fromisoformat(b["received_date"])
    earliest = min([date.fromisoformat(d) for d in dates] + [received])
    # Checks are sometimes held a while before depositing, but not for months
    earliest = max(earliest, received - timedelta(days=60))
    return ((earliest - timedelta(days=WINDOW_BEFORE_DAYS)).isoformat(),
            (received + timedelta(days=WINDOW_AFTER_DAYS)).isoformat(), b["received_date"])


def _free_deposits(conn, amount, start, end, anchor):
    """Deposits of exactly this amount not already linked to other checks."""
    return conn.execute(
        "SELECT t.* FROM transactions t WHERE t.amount > 0 AND ROUND(t.amount, 2) = ROUND(?, 2) "
        "AND t.date BETWEEN ? AND ? AND NOT EXISTS (SELECT 1 FROM checks c WHERE c.transaction_id = t.id) "
        "ORDER BY ABS(julianday(t.date) - julianday(?)), t.id",
        (amount, start, end, anchor)).fetchall()


def _link(conn, check_ids, tx):
    conn.executemany("UPDATE checks SET transaction_id=? WHERE id=?", [(tx["id"], i) for i in check_ids])
    # Give an uncategorized deposit a sensible category (the report splits it by check)
    if tx["category"] is None:
        kinds = {r[0] for r in conn.execute(
            f"SELECT kind FROM checks WHERE id IN ({','.join('?' * len(check_ids))})", check_ids)}
        cat = RENT_CATEGORY if kinds == {"rent"} else GIFT_CATEGORY
        conn.execute("UPDATE transactions SET category=?, category_source='check' WHERE id=?", (cat, tx["id"]))


def match_batch(conn, batch_id):
    """Match a batch's unmatched checks to bank deposits. Returns number of checks matched."""
    rows = conn.execute("SELECT id, amount FROM checks WHERE batch_id=? AND transaction_id IS NULL "
                        "AND amount IS NOT NULL", (batch_id,)).fetchall()
    if not rows:
        return 0
    start, end, anchor = _window(conn, batch_id)
    matched = 0
    total = round(sum(r["amount"] for r in rows), 2)
    already = conn.execute("SELECT COUNT(*) FROM checks WHERE batch_id=? AND transaction_id IS NOT NULL",
                           (batch_id,)).fetchone()[0]
    deposits = _free_deposits(conn, total, start, end, anchor) if not already else []
    if deposits:
        _link(conn, [r["id"] for r in rows], deposits[0])
        matched = len(rows)
    elif len(rows) > 1 or already:
        for r in rows:
            d = _free_deposits(conn, r["amount"], start, end, anchor)
            if d:
                _link(conn, [r["id"]], d[0])
                matched += 1
    conn.commit()
    return matched


def match_all(conn):
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT batch_id FROM checks WHERE transaction_id IS NULL AND amount IS NOT NULL")]
    return sum(match_batch(conn, i) for i in ids)


def deposit_candidates(conn, batch_id, limit=15):
    """Deposits near this batch's date, for matching by hand."""
    start, end, anchor = _window(conn, batch_id)
    return conn.execute(
        "SELECT t.* FROM transactions t WHERE t.amount > 0 AND t.date BETWEEN ? AND ? "
        "ORDER BY ABS(julianday(t.date) - julianday(?)) LIMIT ?",
        (start, end, anchor, limit)).fetchall()


def allocation(conn, start, end):
    """How deposits with linked checks split between gifts and rent.

    Returns {transaction_id: {"gift": x, "rent": y, "other": z}} for deposits in
    the date range whose category is splittable. Any part of the deposit not
    covered by checks stays with the deposit's own category.
    """
    out = {}
    for r in conn.execute(
            "SELECT t.id, c.kind, SUM(c.amount) amt FROM transactions t JOIN checks c ON c.transaction_id = t.id "
            "WHERE t.date BETWEEN ? AND ? AND c.amount IS NOT NULL GROUP BY t.id, c.kind", (start, end)):
        out.setdefault(r["id"], {"gift": 0.0, "rent": 0.0, "other": 0.0})[r["kind"]] += r["amt"]
    return out
