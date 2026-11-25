"""SQLite storage for the treasurer app."""
import sqlite3
from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    -- income | expense | payout (online-giving deposits, excluded from income
    -- because the giving records already count those gifts) | transfer
    kind TEXT NOT NULL,
    monthly_budget REAL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    category TEXT NOT NULL REFERENCES categories(name) ON UPDATE CASCADE,
    priority INTEGER NOT NULL DEFAULT 100
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (datetime('now')),
    rows_added INTEGER NOT NULL DEFAULT 0,
    rows_skipped INTEGER NOT NULL DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- ISO yyyy-mm-dd
    amount REAL NOT NULL,            -- negative = money out
    description TEXT NOT NULL,
    category TEXT REFERENCES categories(name) ON UPDATE CASCADE,
    category_source TEXT,            -- rule | manual | ai
    balance REAL,                    -- running balance if the bank provided it
    account TEXT,
    notes TEXT,
    import_id INTEGER REFERENCES imports(id),
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);

CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    gross REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    net REAL NOT NULL,
    donor TEXT,
    fund TEXT,
    method TEXT,
    platform TEXT,
    kind TEXT NOT NULL DEFAULT 'gift',   -- gift | rent (student housing payments)
    import_id INTEGER REFERENCES imports(id),
    fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_don_date ON donations(date);

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    date TEXT,
    amount REAL,                     -- positive total
    vendor TEXT,
    category TEXT,
    purpose TEXT,
    transaction_id INTEGER UNIQUE REFERENCES transactions(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS email_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    sender TEXT,
    subject TEXT,
    status TEXT NOT NULL,            -- processing | imported | rejected | no_attachments | error
    detail TEXT
);

CREATE TABLE IF NOT EXISTS report_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    month TEXT NOT NULL,             -- report month YYYY-MM
    scheduled INTEGER NOT NULL,      -- 1 = the automatic monthly send, 0 = sent by hand
    status TEXT NOT NULL,            -- sending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    recipients TEXT,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- One automatic send per report month
CREATE UNIQUE INDEX IF NOT EXISTS idx_sends_sched ON report_sends(month) WHERE scheduled = 1;

CREATE TABLE IF NOT EXISTS narratives (
    month TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

DEFAULT_CATEGORIES = [
    # Income
    ("Donations - Cash & Check", "income", None),
    ("Church & Conference Support", "income", None),
    ("Student Housing Rent", "income", None),
    ("Grants", "income", None),
    ("Fundraisers & Events", "income", None),
    ("Interest", "income", None),
    ("Other Income", "income", None),
    # Online giving payouts (the gifts themselves come from giving records)
    ("Online Giving Payout", "payout", None),
    # Transfers between our own accounts
    ("Transfer", "transfer", None),
    # Expenses
    ("Staff & Payroll", "expense", None),
    ("Worship & Programs", "expense", None),
    ("Food & Fellowship", "expense", None),
    ("Missions & Outreach", "expense", None),
    ("Retreats & Travel", "expense", None),
    ("Student Ministry Supplies", "expense", None),
    ("Facilities & Utilities", "expense", None),
    ("Repairs & Maintenance", "expense", None),
    ("Insurance", "expense", None),
    ("Office & Administrative", "expense", None),
    ("Software & Subscriptions", "expense", None),
    ("Bank & Processing Fees", "expense", None),
    ("Apportionments & Dues", "expense", None),
    ("Other Expense", "expense", None),
]

DEFAULT_RULES = [
    # Giving platforms: deposits from these are payouts of online gifts
    ("TITHE.LY", "Online Giving Payout"), ("TITHELY", "Online Giving Payout"),
    ("PUSHPAY", "Online Giving Payout"), ("GIVELIFY", "Online Giving Payout"),
    ("VANCO", "Online Giving Payout"), ("PLANNING CENTER", "Online Giving Payout"),
    ("ONEREALM", "Online Giving Payout"), ("SUBSPLASH", "Online Giving Payout"),
    ("PAYPAL TRANSFER", "Online Giving Payout"), ("STRIPE", "Online Giving Payout"),
    # Expenses
    ("GEORGIA POWER", "Facilities & Utilities"), ("CITY OF VALDOSTA", "Facilities & Utilities"),
    ("SPECTRUM", "Facilities & Utilities"), ("AT&T", "Facilities & Utilities"),
    ("COMCAST", "Facilities & Utilities"), ("ATMOS", "Facilities & Utilities"),
    ("WATER", "Facilities & Utilities"),
    ("PUBLIX", "Food & Fellowship"), ("WINN-DIXIE", "Food & Fellowship"),
    ("WINN DIXIE", "Food & Fellowship"), ("KROGER", "Food & Fellowship"),
    ("SAMS CLUB", "Food & Fellowship"), ("SAM'S CLUB", "Food & Fellowship"),
    ("CHICK-FIL-A", "Food & Fellowship"), ("PIZZA", "Food & Fellowship"),
    ("DOMINO", "Food & Fellowship"), ("ALDI", "Food & Fellowship"),
    ("HOME DEPOT", "Repairs & Maintenance"), ("LOWE'S", "Repairs & Maintenance"),
    ("LOWES", "Repairs & Maintenance"),
    ("STAPLES", "Office & Administrative"), ("OFFICE DEPOT", "Office & Administrative"),
    ("USPS", "Office & Administrative"),
    ("ADOBE", "Software & Subscriptions"), ("GOOGLE", "Software & Subscriptions"),
    ("MICROSOFT", "Software & Subscriptions"), ("ZOOM", "Software & Subscriptions"),
    ("CANVA", "Software & Subscriptions"), ("SPOTIFY", "Software & Subscriptions"),
    ("PLANNING CENTER ONLINE", "Software & Subscriptions"),
    ("CHURCH MUTUAL", "Insurance"), ("BROTHERHOOD MUTUAL", "Insurance"),
    ("GUSTO", "Staff & Payroll"), ("ADP", "Staff & Payroll"), ("PAYCHEX", "Staff & Payroll"),
    ("PAYROLL", "Staff & Payroll"),
    ("SERVICE CHARGE", "Bank & Processing Fees"), ("MONTHLY FEE", "Bank & Processing Fees"),
    ("OVERDRAFT", "Bank & Processing Fees"),
    ("INTEREST", "Interest"),
    ("TRANSFER TO", "Transfer"), ("TRANSFER FROM", "Transfer"),
    ("DELTA", "Retreats & Travel"), ("SOUTHWEST", "Retreats & Travel"),
    ("HOTEL", "Retreats & Travel"), ("SHELL OIL", "Retreats & Travel"),
    ("EXXON", "Retreats & Travel"), ("CHEVRON", "Retreats & Travel"),
    ("AMAZON", "Student Ministry Supplies"), ("AMZN", "Student Ministry Supplies"),
    ("WALMART", "Student Ministry Supplies"), ("WAL-MART", "Student Ministry Supplies"),
    ("TARGET", "Student Ministry Supplies"),
    ("COKESBURY", "Worship & Programs"), ("CCLI", "Worship & Programs"),
    ("UMC", "Apportionments & Dues"), ("CONFERENCE", "Apportionments & Dues"),
    # Rules only fire in the matching direction (income rules on deposits,
    # expense rules on withdrawals), so the same text can map both ways.
    ("CONFERENCE", "Church & Conference Support"), ("UMC", "Church & Conference Support"),
    ("MOBILE DEPOSIT", "Donations - Cash & Check"), ("DEPOSIT", "Donations - Cash & Check"),
]

DEFAULT_SETTINGS = {
    "org_name": "VSU Wesley Foundation",
    "fiscal_year_start_month": "1",
    "receipt_required_over": "25",
    "report_recipients": "revmattstout@gmail.com",
    # Funds in the online payment records (e.g. Planning Center) that are
    # student housing rent rather than gifts. Comma-separated, whole words.
    "rent_funds": "Rent",
    "report_schedule_enabled": "1",
    "report_send_hour": "8",
}


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def connect(path):
    # The web server and the background-jobs process share this file: wait
    # briefly for each other's writes instead of failing, and use WAL so
    # readers never block behind a writer.
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(conn):
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO categories(name, kind, monthly_budget) VALUES (?,?,?)",
            DEFAULT_CATEGORIES,
        )
        conn.executemany(
            "INSERT INTO rules(pattern, category, priority) VALUES (?,?,?)",
            # Earlier entries win; longer, more specific patterns first.
            [(p, c, 100 - min(len(p), 50)) for p, c in DEFAULT_RULES],
        )
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))
    # Upgrades for databases created by earlier versions
    cols = {r[1] for r in conn.execute("PRAGMA table_info(donations)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE donations ADD COLUMN kind TEXT NOT NULL DEFAULT 'gift'")
        reclassify_rent(conn)
    conn.execute("INSERT OR IGNORE INTO categories(name, kind) VALUES ('Student Housing Rent', 'income')")
    conn.commit()


RENT_CATEGORY = "Student Housing Rent"


def rent_patterns(conn):
    raw = get_setting(conn, "rent_funds", "Rent") or ""
    return [p.strip() for p in raw.split(",") if p.strip()]


def is_rent_fund(fund, patterns):
    """True if the fund name contains any pattern as a whole word,
    e.g. "Rent" matches "Rent", "Rent - Fall 2026", "Housing rent" but not "Parent"."""
    import re
    return any(re.search(rf"(?<![A-Za-z]){re.escape(p)}(?![A-Za-z])", fund or "", re.IGNORECASE)
               for p in patterns)


def reclassify_rent(conn):
    """Re-mark every online payment as rent or gift from its fund name."""
    pats = rent_patterns(conn)
    for r in conn.execute("SELECT id, fund FROM donations").fetchall():
        conn.execute("UPDATE donations SET kind=? WHERE id=?",
                     ("rent" if is_rent_fund(r["fund"], pats) else "gift", r["id"]))


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
