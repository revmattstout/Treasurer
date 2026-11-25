"""Parsers for bank statements (CSV / OFX / QFX) and online giving exports (CSV).

Column names differ between banks and giving platforms, so the CSV parsers
look for any of several common header names rather than one fixed layout.
"""
import csv
import hashlib
import io
import re
from collections import Counter
from datetime import date, datetime


class ImportError_(ValueError):
    """Raised when a file can't be understood."""


# ---------------------------------------------------------------- helpers

DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y/%m/%d",
    "%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p",
]


def parse_date(value):
    if isinstance(value, date):
        return value.isoformat()
    s = (value or "").strip()
    if not s:
        raise ValueError("empty date")
    # Strip timezone suffixes like "Z", "+00:00", " EST"
    s2 = re.sub(r"(Z|[+-]\d\d:?\d\d| [A-Z]{2,4})$", "", s).strip()
    for cand in (s2, s2[:19], s2[:10]):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(cand, fmt).date().isoformat()
            except ValueError:
                pass
    raise ValueError(f"unrecognized date: {value!r}")


def parse_amount(value):
    """Parse '$1,234.56', '(12.00)', '-5', '12.00 CR', '' -> float or None."""
    s = (value or "").strip()
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    up = s.upper()
    if up.endswith("CR"):
        s = s[:-2]
    elif up.endswith("DR"):
        neg, s = True, s[:-2]
    s = s.replace("$", "").replace(",", "").replace(" ", "")
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        raise ValueError(f"unrecognized amount: {value!r}")
    return round(-v if neg else v, 2)


def _norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def _find(headers, *candidates):
    """Return the index of the first header matching any candidate name."""
    normed = [_norm(h) for h in headers]
    for c in candidates:
        c = _norm(c)
        if c in normed:
            return normed.index(c)
    return None


def _read_csv(text):
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return list(csv.reader(io.StringIO(text), dialect))


def _locate_header(rows, required_groups):
    """Find the header row (banks often put account info above it)."""
    for i, row in enumerate(rows[:25]):
        if all(_find(row, *grp) is not None for grp in required_groups):
            return i
    return None


def fingerprint(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:32]


def _with_occurrence(records, key_fn, prefix):
    """Give identical rows in one file distinct fingerprints (two $5 coffees
    on the same day are both real), while re-importing the same file still
    produces the same fingerprints and is skipped."""
    seen = Counter()
    for r in records:
        k = key_fn(r)
        seen[k] += 1
        r["fingerprint"] = fingerprint(prefix, *k, seen[k])
    return records


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- bank CSV

DATE_COLS = ("Date", "Posting Date", "Posted Date", "Transaction Date", "Trans Date",
             "Post Date", "Effective Date", "Trans. Date")
DESC_COLS = ("Description", "Transaction Description", "Payee", "Name", "Details",
             "Memo", "Merchant", "Transaction")
AMOUNT_COLS = ("Amount", "Transaction Amount", "Amount ($)", "Net Amount")
DEBIT_COLS = ("Debit", "Debits", "Withdrawal", "Withdrawals", "Withdrawal Amount",
              "Debit Amount", "Money Out", "Payments")
CREDIT_COLS = ("Credit", "Credits", "Deposit", "Deposits", "Deposit Amount",
               "Credit Amount", "Money In")
BALANCE_COLS = ("Balance", "Running Balance", "Ledger Balance", "Available Balance")
EXTRA_DESC_COLS = ("Memo", "Extended Description", "Notes")
TYPE_COLS = ("Type", "Transaction Type", "CR/DR", "Debit/Credit")


def parse_bank_csv(text, account=None):
    rows = _read_csv(text)
    hi = _locate_header(rows, [DATE_COLS, DESC_COLS])
    if hi is None:
        raise ImportError_(
            "Couldn't find Date and Description columns. Check that this is a "
            "bank statement CSV export with a header row."
        )
    headers = rows[hi]
    i_date = _find(headers, *DATE_COLS)
    i_desc = _find(headers, *DESC_COLS)
    i_amt = _find(headers, *AMOUNT_COLS)
    i_debit = _find(headers, *DEBIT_COLS)
    i_credit = _find(headers, *CREDIT_COLS)
    i_bal = _find(headers, *BALANCE_COLS)
    i_type = _find(headers, *TYPE_COLS)
    i_extra = next((i for i in (_find(headers, c) for c in EXTRA_DESC_COLS)
                    if i is not None and i != i_desc), None)
    if i_amt is None and i_debit is None and i_credit is None:
        raise ImportError_("Couldn't find an Amount column (or Debit/Credit columns).")

    out = []
    for row in rows[hi + 1:]:
        if not any(c.strip() for c in row):
            continue
        get = lambda i: row[i] if i is not None and i < len(row) else ""
        try:
            d = parse_date(get(i_date))
        except ValueError:
            continue  # footer lines, totals, etc.
        if i_amt is not None and get(i_amt).strip():
            amt = parse_amount(get(i_amt))
            # Some banks give unsigned amounts plus a Debit/Credit type column
            t = get(i_type).strip().upper()
            if amt is not None and amt > 0 and t in ("DEBIT", "DR", "D", "WITHDRAWAL"):
                amt = -amt
        else:
            debit = parse_amount(get(i_debit)) or 0.0
            credit = parse_amount(get(i_credit)) or 0.0
            amt = round(abs(credit) - abs(debit), 2)
        if amt is None:
            continue
        desc = " ".join(get(i_desc).split())
        extra = " ".join(get(i_extra).split())
        if extra and extra.upper() not in desc.upper():
            desc = f"{desc} — {extra}" if desc else extra
        bal = None
        if i_bal is not None:
            try:
                bal = parse_amount(get(i_bal))
            except ValueError:
                bal = None
        out.append({"date": d, "amount": amt, "description": desc or "(no description)",
                    "balance": bal, "account": account})
    if not out:
        raise ImportError_("No transactions found in this file.")
    return _with_occurrence(out, lambda r: (r["date"], r["amount"], r["description"].upper()), "bank")


# ---------------------------------------------------------------- OFX / QFX

def _ofx_tag(block, tag):
    m = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_ofx(text, account=None):
    blocks = re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|</BANKTRANLIST>)",
                        text, re.IGNORECASE | re.DOTALL)
    if not blocks:
        raise ImportError_("No transactions found in this OFX/QFX file.")
    acct = account or _ofx_tag(text, "ACCTID")[-4:] or None
    out = []
    for b in blocks:
        raw_date = _ofx_tag(b, "DTPOSTED")[:8]
        try:
            d = datetime.strptime(raw_date, "%Y%m%d").date().isoformat()
            amt = parse_amount(_ofx_tag(b, "TRNAMT"))
        except ValueError:
            continue
        name, memo = _ofx_tag(b, "NAME"), _ofx_tag(b, "MEMO")
        desc = name if not memo or memo.upper() in name.upper() else f"{name} — {memo}" if name else memo
        fitid = _ofx_tag(b, "FITID")
        rec = {"date": d, "amount": amt, "description": desc or "(no description)",
               "balance": None, "account": acct}
        if fitid:
            rec["fingerprint"] = fingerprint("ofx", acct, fitid)
        out.append(rec)
    # Fall back to content fingerprints for any rows without a FITID
    no_id = [r for r in out if "fingerprint" not in r]
    _with_occurrence(no_id, lambda r: (r["date"], r["amount"], r["description"].upper()), "bank")
    # Ending balance goes on the last transaction so reports can show it
    bal = _ofx_tag(text.split("<LEDGERBAL>")[-1], "BALAMT") if "<LEDGERBAL>" in text.upper() else ""
    if bal and out:
        latest = max(out, key=lambda r: r["date"])
        try:
            latest["balance"] = parse_amount(bal)
        except ValueError:
            pass
    return out


def parse_bank_file(filename, data: bytes, account=None):
    name = filename.lower()
    text = decode(data)
    if name.endswith((".ofx", ".qfx", ".qbo")) or "<OFX>" in text[:2000].upper():
        return parse_ofx(text, account)
    if name.endswith((".csv", ".txt", ".tsv")):
        return parse_bank_csv(text, account)
    raise ImportError_("Unsupported bank file type. Upload CSV, OFX/QFX, or PDF.")


# ---------------------------------------------------------------- giving CSV

G_DATE = ("Date", "Gift Date", "Donation Date", "Transaction Date", "Received At",
          "Received Date", "Created At", "Date Given", "Payment Date", "Created")
G_GROSS = ("Gross", "Gross Amount", "Amount", "Gift Amount", "Donation Amount", "Total",
           "Total Amount", "Amount Given")
G_FEE = ("Fee", "Fees", "Processing Fee", "Processing Fees", "Fee Amount", "Transaction Fee",
         "Platform Fee", "Fee Covered")
G_NET = ("Net", "Net Amount", "Net Deposit", "Deposit Amount", "Payout Amount")
G_NAME = ("Donor", "Donor Name", "Name", "Full Name", "Giver", "Giver Name", "Payer Name",
          "Person Name", "Person", "From", "Contributor")
G_FIRST = ("First Name", "First", "Donor First Name")
G_LAST = ("Last Name", "Last", "Donor Last Name")
G_FUND = ("Fund", "Fund Name", "Designation", "Envelope", "Category", "Campaign",
          "Giving Type", "Gift Type", "Purpose", "Item Title")
G_METHOD = ("Method", "Payment Method", "Payment Type", "Source", "Card Type", "Type")
G_STATUS = ("Status", "Payment Status", "Transaction Status")

SKIP_STATUSES = {"FAILED", "DECLINED", "PENDING", "CANCELED", "CANCELLED", "VOIDED", "DENIED"}


def parse_giving_csv(text, platform=None):
    rows = _read_csv(text)
    hi = _locate_header(rows, [G_DATE, G_GROSS])
    if hi is None:
        raise ImportError_(
            "Couldn't find Date and Amount columns. Upload the donations/transactions "
            "CSV export from your giving platform."
        )
    h = rows[hi]
    i_date, i_gross = _find(h, *G_DATE), _find(h, *G_GROSS)
    i_fee, i_net = _find(h, *G_FEE), _find(h, *G_NET)
    i_name, i_first, i_last = _find(h, *G_NAME), _find(h, *G_FIRST), _find(h, *G_LAST)
    i_fund, i_method, i_status = _find(h, *G_FUND), _find(h, *G_METHOD), _find(h, *G_STATUS)
    if i_net == i_gross:
        i_net = None

    out = []
    for row in rows[hi + 1:]:
        if not any(c.strip() for c in row):
            continue
        get = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        if get(i_status).upper() in SKIP_STATUSES:
            continue
        try:
            d = parse_date(get(i_date))
            gross = parse_amount(get(i_gross))
        except ValueError:
            continue
        if gross is None:
            continue
        try:
            fee = abs(parse_amount(get(i_fee)) or 0.0)
            net = parse_amount(get(i_net))
        except ValueError:
            fee, net = 0.0, None
        if gross < 0:  # refund: the fee generally isn't returned
            fee = 0.0 if net is None else fee
        if net is None:
            net = round(gross - fee, 2)
        elif i_fee is None:
            fee = round(gross - net, 2)
        donor = get(i_name) or " ".join(x for x in (get(i_first), get(i_last)) if x)
        out.append({
            "date": d, "gross": round(gross, 2), "fee": round(fee, 2), "net": round(net, 2),
            "donor": donor or "Anonymous", "fund": get(i_fund) or "General Fund",
            "method": get(i_method) or None, "platform": platform,
        })
    if not out:
        raise ImportError_("No gifts found in this file.")
    return _with_occurrence(out, lambda r: (r["date"], r["gross"], r["donor"].upper(), r["fund"].upper()),
                            f"gift:{platform or ''}")
