"""Rule-based transaction categorization."""


def load_rules(conn):
    return conn.execute(
        "SELECT r.id, r.pattern, r.category, c.kind FROM rules r "
        "JOIN categories c ON c.name = r.category "
        "ORDER BY r.priority, r.id"
    ).fetchall()


def match_rule(rules, description, amount):
    """Return the category of the first rule that matches, or None.

    Income and payout rules only match deposits; expense rules only match
    withdrawals; transfer rules match either direction.
    """
    text = (description or "").upper()
    for r in rules:
        if r["pattern"].upper() not in text:
            continue
        kind = r["kind"]
        if kind in ("income", "payout") and amount <= 0:
            continue
        if kind == "expense" and amount >= 0:
            continue
        return r["category"]
    return None


def apply_rules(conn, only_uncategorized=True):
    """Categorize transactions using the rules table. Returns count updated.

    Never overrides a category a person chose by hand.
    """
    rules = load_rules(conn)
    where = "category IS NULL" if only_uncategorized else "COALESCE(category_source,'') != 'manual'"
    rows = conn.execute(f"SELECT id, description, amount FROM transactions WHERE {where}").fetchall()
    n = 0
    for row in rows:
        cat = match_rule(rules, row["description"], row["amount"])
        if cat:
            conn.execute(
                "UPDATE transactions SET category=?, category_source='rule' WHERE id=?",
                (cat, row["id"]),
            )
            n += 1
    conn.commit()
    return n


def rule_pattern_from_description(description):
    """Guess a reusable rule pattern from a bank description.

    Drops trailing reference numbers, dates and store numbers so that
    "PUBLIX #1234 VALDOSTA GA 03/14" becomes "PUBLIX".
    """
    words = []
    for w in (description or "").upper().split():
        if any(ch.isdigit() for ch in w) or w.startswith("#"):
            break
        words.append(w)
        if len(words) == 3:
            break
    return " ".join(words) or (description or "").upper()[:20]
