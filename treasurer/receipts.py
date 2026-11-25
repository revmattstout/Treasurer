"""Receipt storage and matching receipts to bank transactions."""
import os
import uuid
from datetime import date, timedelta

ALLOWED = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
    ".heic": "image/heic",
}


def mime_for(filename):
    return ALLOWED.get(os.path.splitext(filename.lower())[1])


def store_file(folder, filename, data):
    ext = os.path.splitext(filename.lower())[1]
    stored = f"{uuid.uuid4().hex}{ext}"
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, stored), "wb") as f:
        f.write(data)
    return stored


def candidates(conn, amount, on_date, days=10, limit=10):
    """Unmatched withdrawals with the same amount near the receipt date.

    Card charges usually post 0-5 days after the purchase, so the window
    leans forward from the receipt date.
    """
    if amount is None:
        return []
    params = [round(-abs(amount), 2)]
    sql = ("SELECT t.* FROM transactions t LEFT JOIN receipts r ON r.transaction_id = t.id "
           "WHERE r.id IS NULL AND ROUND(t.amount, 2) = ?")
    if on_date:
        d = date.fromisoformat(on_date)
        sql += " AND t.date BETWEEN ? AND ?"
        params += [(d - timedelta(days=3)).isoformat(), (d + timedelta(days=days)).isoformat()]
        sql += " ORDER BY ABS(julianday(t.date) - julianday(?)) LIMIT ?"
        params += [on_date, limit]
    else:
        sql += " ORDER BY t.date DESC LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def auto_match(conn, receipt_id):
    """Link a receipt to a transaction when there's exactly one clear match."""
    r = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
    if not r or r["transaction_id"] or r["amount"] is None:
        return None
    cands = candidates(conn, r["amount"], r["date"])
    if len(cands) == 1 or (len(cands) > 1 and r["date"] and cands[0]["date"] == r["date"]
                           and cands[1]["date"] != r["date"]):
        tx = cands[0]
        conn.execute("UPDATE receipts SET transaction_id=? WHERE id=?", (tx["id"], receipt_id))
        # A receipt's category is a good signal if the bank line wasn't categorized
        if r["category"] and tx["category"] is None:
            conn.execute("UPDATE transactions SET category=?, category_source='manual' WHERE id=?",
                         (r["category"], tx["id"]))
        conn.commit()
        return tx["id"]
    return None


def auto_match_all(conn):
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM receipts WHERE transaction_id IS NULL AND amount IS NOT NULL")]
    return sum(1 for i in ids if auto_match(conn, i))
