"""Monthly financial report calculations.

How the numbers fit together
----------------------------
* Online gifts are counted from the giving-platform records (gross amount, by
  fund). The lump-sum payout the platform later deposits in the bank is
  categorized "Online Giving Payout" and *not* counted again as income.
  Platform processing fees show up as an expense.
* Other deposits (cash/check deposits, grants, conference support...) are
  counted from the bank statement by category.
* Expenses come from bank withdrawals by category. A positive transaction
  given an expense category (a refund) reduces that category.
* Transfers between the ministry's own accounts are left out of both.
"""
import csv
import io
from calendar import monthrange
from collections import defaultdict
from datetime import date

from .categorize import rule_pattern_from_description
from .db import RENT_CATEGORY, get_setting

UNCAT_IN = "Uncategorized deposits"
UNCAT_OUT = "Uncategorized withdrawals"
FEES_CATEGORY = "Bank & Processing Fees"


def month_bounds(month):
    y, m = map(int, month.split("-"))
    return date(y, m, 1).isoformat(), date(y, m, monthrange(y, m)[1]).isoformat()


def shift_month(month, delta):
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def fiscal_year_start(month, start_month):
    y, m = map(int, month.split("-"))
    fy = y if m >= start_month else y - 1
    return f"{fy:04d}-{start_month:02d}"


def months_between(first, last):
    out, cur = [], first
    while cur <= last:
        out.append(cur)
        cur = shift_month(cur, 1)
    return out


def available_months(conn):
    rows = conn.execute(
        "SELECT DISTINCT substr(date,1,7) m FROM transactions "
        "UNION SELECT DISTINCT substr(date,1,7) FROM donations ORDER BY m DESC"
    ).fetchall()
    return [r["m"] for r in rows]


def _kinds(conn):
    return {r["name"]: r["kind"] for r in conn.execute("SELECT name, kind FROM categories")}


def period_totals(conn, start, end):
    """Income and expense totals by line for an inclusive date range."""
    kinds = _kinds(conn)
    income = defaultdict(float)     # line -> amount
    giving_by_fund = defaultdict(float)
    expense = defaultdict(float)
    payouts = transfers = 0.0

    for r in conn.execute(
        "SELECT category, amount FROM transactions WHERE date BETWEEN ? AND ?", (start, end)
    ):
        cat, amt = r["category"], r["amount"]
        kind = kinds.get(cat)
        if kind == "payout":
            payouts += amt
        elif kind == "transfer":
            transfers += amt
        elif kind == "income":
            income[cat] += amt
        elif kind == "expense":
            expense[cat] += -amt
        elif amt >= 0:
            income[UNCAT_IN] += amt
        else:
            expense[UNCAT_OUT] += -amt

    online = conn.execute(
        "SELECT kind, fund, SUM(gross) gross, SUM(fee) fee, SUM(net) net, COUNT(*) n "
        "FROM donations WHERE date BETWEEN ? AND ? GROUP BY kind, fund",
        (start, end),
    ).fetchall()
    fees = net = 0.0
    n_gifts = n_rent = 0
    rent_total = 0.0
    for g in online:
        # Fees and payouts cover gifts and rent alike (same platform)
        fees += g["fee"]
        net += g["net"]
        if g["kind"] == "rent":
            rent_total += g["gross"]
            n_rent += g["n"]
        else:
            giving_by_fund[g["fund"]] += g["gross"]
            n_gifts += g["n"]
    if rent_total:
        income[RENT_CATEGORY] += rent_total
    if fees:
        expense[FEES_CATEGORY] += fees
    donors = conn.execute(
        "SELECT COUNT(DISTINCT donor) FROM donations WHERE date BETWEEN ? AND ? "
        "AND kind = 'gift' AND donor != 'Anonymous'", (start, end)
    ).fetchone()[0]
    renters = conn.execute(
        "SELECT COUNT(DISTINCT donor) FROM donations WHERE date BETWEEN ? AND ? AND kind = 'rent'",
        (start, end)).fetchone()[0]

    online_total = sum(giving_by_fund.values())
    total_income = online_total + sum(income.values())
    total_expense = sum(expense.values())
    return {
        "giving_by_fund": dict(giving_by_fund),
        "online_giving_total": round(online_total, 2),
        "online_giving_fees": round(fees, 2),
        "online_giving_net": round(net, 2),
        "gift_count": n_gifts,
        "donor_count": donors,
        "rent_online_total": round(rent_total, 2),
        "rent_payment_count": n_rent,
        "rent_payer_count": renters,
        "other_income": dict(income),
        "expenses": dict(expense),
        "payouts_deposited": round(payouts, 2),
        "transfers_net": round(transfers, 2),
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "net": round(total_income - total_expense, 2),
    }


def bank_summary(conn, start, end):
    row = conn.execute(
        "SELECT SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) deposits, "
        "SUM(CASE WHEN amount<0 THEN -amount ELSE 0 END) withdrawals, COUNT(*) n "
        "FROM transactions WHERE date BETWEEN ? AND ?", (start, end)
    ).fetchone()
    deposits, withdrawals = row["deposits"] or 0.0, row["withdrawals"] or 0.0
    # Balances are per account; add up the accounts whose statements give one.
    accounts = []
    for a in conn.execute(
        "SELECT COALESCE(account,'') acct, SUM(amount) change FROM transactions "
        "WHERE date BETWEEN ? AND ? GROUP BY acct ORDER BY acct", (start, end)
    ):
        last = conn.execute(
            "SELECT balance FROM transactions WHERE COALESCE(account,'')=? AND date BETWEEN ? AND ? "
            "AND balance IS NOT NULL ORDER BY date DESC, id DESC LIMIT 1", (a["acct"], start, end)
        ).fetchone()
        ending = last["balance"] if last else None
        accounts.append({
            "name": a["acct"] or "Primary account",
            "ending": ending,
            "beginning": round(ending - a["change"], 2) if ending is not None else None,
            "change": round(a["change"], 2),
        })
    known = [a for a in accounts if a["ending"] is not None]
    ending = round(sum(a["ending"] for a in known), 2) if known else None
    beginning = round(sum(a["beginning"] for a in known), 2) if known else None
    return {
        "deposits": round(deposits, 2), "withdrawals": round(withdrawals, 2),
        "transaction_count": row["n"], "beginning_balance": beginning, "ending_balance": ending,
        "accounts": accounts,
        "balance_incomplete": bool(known) and len(known) < len(accounts),
    }


def build_report(conn, month):
    start, end = month_bounds(month)
    prev = shift_month(month, -1)
    p_start, p_end = month_bounds(prev)
    fy_month = int(get_setting(conn, "fiscal_year_start_month", "1"))
    fy_first = fiscal_year_start(month, fy_month)
    fy_start = month_bounds(fy_first)[0]
    fy_months = len(months_between(fy_first, month))
    threshold = float(get_setting(conn, "receipt_required_over", "25") or 0)

    cur = period_totals(conn, start, end)
    prv = period_totals(conn, p_start, p_end)
    ytd = period_totals(conn, fy_start, end)
    budgets = {r["name"]: r["monthly_budget"] for r in
               conn.execute("SELECT name, monthly_budget FROM categories")}

    def lines(key):
        names = sorted(set(cur[key]) | set(prv[key]) | set(ytd[key]) |
                       ({n for n, b in budgets.items() if b and key == "expenses"
                         and _kinds(conn).get(n) == "expense"}))
        out = []
        for n in names:
            b = budgets.get(n)
            out.append({
                "name": n,
                "amount": round(cur[key].get(n, 0.0), 2),
                "prior": round(prv[key].get(n, 0.0), 2),
                "ytd": round(ytd[key].get(n, 0.0), 2),
                "budget": b,
                "ytd_budget": round(b * fy_months, 2) if b else None,
                "variance": round(b - cur[key].get(n, 0.0), 2) if b else None,
            })
        out.sort(key=lambda x: -abs(x["amount"]))
        return out

    expense_rows = conn.execute(
        "SELECT t.id, t.date, t.description, t.amount, t.category, r.id AS receipt_id "
        "FROM transactions t LEFT JOIN receipts r ON r.transaction_id = t.id "
        "LEFT JOIN categories c ON c.name = t.category "
        "WHERE t.date BETWEEN ? AND ? AND t.amount < 0 "
        "AND COALESCE(c.kind,'expense') = 'expense' ORDER BY t.date, t.id", (start, end)
    ).fetchall()
    missing = [dict(r) for r in expense_rows if r["receipt_id"] is None and -r["amount"] >= threshold]
    payees = defaultdict(lambda: [0.0, 0])
    for r in expense_rows:
        p = payees[rule_pattern_from_description(r["description"])]
        p[0] += -r["amount"]
        p[1] += 1
    top_payees = sorted(({"name": k, "amount": round(v[0], 2), "count": v[1]}
                         for k, v in payees.items()), key=lambda x: -x["amount"])[:10]

    uncategorized = [dict(r) for r in conn.execute(
        "SELECT id, date, description, amount FROM transactions "
        "WHERE date BETWEEN ? AND ? AND category IS NULL ORDER BY date", (start, end))]
    unmatched_receipts = conn.execute(
        "SELECT COUNT(*) FROM receipts WHERE transaction_id IS NULL AND "
        "(date IS NULL OR date BETWEEN ? AND ?)", (start, end)).fetchone()[0]

    receipted = sum(1 for r in expense_rows if r["receipt_id"] is not None)
    return {
        "month": month,
        "period": {"start": start, "end": end},
        "prior_month": prev,
        "fiscal_year_start": fy_start,
        "summary": {
            "income": cur["total_income"], "expense": cur["total_expense"], "net": cur["net"],
            "prior_income": prv["total_income"], "prior_expense": prv["total_expense"],
            "prior_net": prv["net"],
            "ytd_income": ytd["total_income"], "ytd_expense": ytd["total_expense"],
            "ytd_net": ytd["net"],
        },
        "giving": {
            "by_fund": [{"name": k, "amount": round(cur["giving_by_fund"].get(k, 0.0), 2),
                         "prior": round(prv["giving_by_fund"].get(k, 0.0), 2),
                         "ytd": round(ytd["giving_by_fund"].get(k, 0.0), 2)}
                        for k, v in sorted(ytd["giving_by_fund"].items(),
                                           key=lambda kv: -cur["giving_by_fund"].get(kv[0], 0))],
            "total": cur["online_giving_total"], "fees": cur["online_giving_fees"],
            "net": cur["online_giving_net"], "gifts": cur["gift_count"],
            "donors": cur["donor_count"],
            "average_gift": round(cur["online_giving_total"] / cur["gift_count"], 2)
            if cur["gift_count"] else 0,
            "payouts_deposited": cur["payouts_deposited"],
            "timing_difference": round(cur["online_giving_net"] - cur["payouts_deposited"], 2),
        },
        "rent": {
            "online_total": cur["rent_online_total"],
            "payments": cur["rent_payment_count"],
            "payers": cur["rent_payer_count"],
            # Includes any rent checks deposited at the bank and categorized as rent
            "total": round(cur["other_income"].get(RENT_CATEGORY, 0.0), 2),
            "prior": round(prv["other_income"].get(RENT_CATEGORY, 0.0), 2),
            "ytd": round(ytd["other_income"].get(RENT_CATEGORY, 0.0), 2),
            "by_payer": [dict(r) for r in conn.execute(
                "SELECT donor AS name, COUNT(*) AS payments, SUM(gross) AS amount, MAX(date) AS last_paid "
                "FROM donations WHERE kind='rent' AND date BETWEEN ? AND ? GROUP BY donor ORDER BY donor",
                (start, end))],
        },
        "other_income": lines("other_income"),
        "expenses": lines("expenses"),
        "bank": bank_summary(conn, start, end),
        "transfers_net": cur["transfers_net"],
        "receipts": {
            "threshold": threshold,
            "expense_count": len(expense_rows),
            "receipted_count": receipted,
            "missing": missing,
            "missing_total": round(sum(-r["amount"] for r in missing), 2),
            "unmatched_receipts": unmatched_receipts,
        },
        "top_payees": top_payees,
        "uncategorized": uncategorized,
    }


def report_for_ai(report):
    """A trimmed copy of the report for the narrative prompt (no donor names)."""
    r = dict(report)
    r["receipts"] = dict(report["receipts"])
    r["receipts"]["missing"] = [
        {"date": m["date"], "description": m["description"], "amount": m["amount"]}
        for m in report["receipts"]["missing"][:25]
    ]
    r["rent"] = {k: v for k, v in report["rent"].items() if k != "by_payer"}  # no resident names
    r["uncategorized_count"] = len(report["uncategorized"])
    r.pop("uncategorized", None)
    return r


def report_csv_text(conn, month):
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
    return buf.getvalue()
