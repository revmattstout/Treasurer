"""The board report: a PDF that always fits on one sheet of letter paper,
front and back (2 pages at most).

It summarizes by category only. It never lists individual donors,
residents, check writers, or purchases. Every income and expense category
appears, with this month, last month, fiscal year to date and budget.

Fitting is enforced, not hoped for: the PDF is built, its pages counted,
and if it runs over two pages it is rebuilt smaller (type size, spacing),
then with a shortened summary, then with online-giving funds combined into
one line, until it fits.
"""
import io
import os
from datetime import date

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from .reports import UNCAT_IN, UNCAT_OUT, build_report

MAX_PAGES = 2
MARGIN_X = 0.55 * inch
FRAME_WIDTH = LETTER[0] - 2 * MARGIN_X
BRAND = colors.HexColor("#b3122e")
INK = colors.HexColor("#1d2330")
MUTED = colors.HexColor("#5d6676")
LINE = colors.HexColor("#d5d9e0")
SHADE = colors.HexColor("#eef1f5")
RED = colors.HexColor("#b42318")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def money(v, parens=True):
    if v is None:
        return "—"
    s = f"${abs(v):,.2f}"
    return f"({s})" if v < 0 and parens else s


def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def board_data(conn, month):
    """Report figures reduced to what the board sees: categories and totals only."""
    rep = build_report(conn, month)
    cats = conn.execute("SELECT name, kind, monthly_budget FROM categories "
                        "WHERE kind IN ('income','expense') ORDER BY name").fetchall()

    def merged(lines, kind):
        by = {l["name"]: l for l in lines}
        out = []
        for c in cats:
            if c["kind"] != kind:
                continue
            l = by.pop(c["name"], None) or {"name": c["name"], "amount": 0.0, "prior": 0.0, "ytd": 0.0,
                                            "budget": c["monthly_budget"], "variance": None}
            out.append(l)
        # Lines that aren't regular categories (fees from giving, uncategorized)
        extras = [l for l in by.values()]
        return out + sorted(extras, key=lambda l: l["name"] in (UNCAT_IN, UNCAT_OUT))

    income_cats = merged(rep["other_income"], "income")
    expense_cats = merged(rep["expenses"], "expense")
    return rep, income_cats, expense_cats


class _PageCounter:
    def __init__(self):
        self.pages = 0

    def __call__(self, canvas, doc):
        self.pages = doc.page


def _build(conn, month, org, logo_path, scale, narrative_chars, combine_funds, prepared):
    rep, income_cats, expense_cats = board_data(conn, month)
    s = rep["summary"]
    fs = 9.0 * scale            # body type size
    pad = 2.2 * scale           # table cell padding
    lead = fs * 1.25

    st_body = ParagraphStyle("body", fontName="Helvetica", fontSize=fs, leading=lead, textColor=INK)
    st_small = ParagraphStyle("small", parent=st_body, fontSize=fs * 0.85, leading=fs * 1.05, textColor=MUTED)
    st_h2 = ParagraphStyle("h2", parent=st_body, fontName="Helvetica-Bold", fontSize=fs * 1.2,
                           leading=fs * 1.5, spaceBefore=6 * scale, spaceAfter=3 * scale, textColor=INK)
    st_title = ParagraphStyle("title", parent=st_body, fontName="Helvetica-Bold", fontSize=15 * scale,
                              leading=18 * scale)
    st_num = ParagraphStyle("num", parent=st_body, alignment=TA_RIGHT)

    story = []
    month_label = date.fromisoformat(month + "-01").strftime("%B %Y")

    # ---- header
    title = [Paragraph(_esc(org), ParagraphStyle("org", parent=st_body, textColor=MUTED)),
             Paragraph(f"Treasurer's Report: {month_label}", st_title),
             Paragraph(f"{rep['period']['start']} to {rep['period']['end']} · fiscal year from "
                       f"{rep['fiscal_year_start']} · cash basis · prepared {prepared}", st_small)]
    logo_size = 0.75 * inch * scale
    if logo_path and os.path.exists(logo_path):
        head = Table([[Image(logo_path, logo_size, logo_size, mask="auto"), title]],
                     colWidths=[logo_size + 8, None])
    else:
        head = Table([[title]])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6 * scale)]

    # ---- key figures
    bank = rep["bank"]
    tiles = [("Income", s["income"], f"prior month {money(s['prior_income'])}"),
             ("Expenses", s["expense"], f"prior month {money(s['prior_expense'])}"),
             ("Net income (loss)", s["net"], f"fiscal YTD {money(s['ytd_net'])}"),
             ("Ending bank balance", bank["ending_balance"],
              f"beginning {money(bank['beginning_balance'])}" if bank["ending_balance"] is not None
              else "no balance on statement")]
    cells = []
    for label, v, sub in tiles:
        color = RED if (label.startswith("Net") and v is not None and v < 0) else INK
        cells.append([Paragraph(label, st_small),
                      Paragraph(f"<b>{money(v)}</b>", ParagraphStyle("v", parent=st_body, fontSize=fs * 1.45,
                                                                       leading=fs * 1.75, textColor=color)),
                      Paragraph(sub, st_small)])
    t = Table([cells], colWidths=[FRAME_WIDTH / 4] * 4, hAlign="LEFT")
    t.setStyle(TableStyle([("BOX", (i, 0), (i, 0), 0.6, LINE) for i in range(4)] +
                          [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 4 * scale),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 4 * scale)]))
    story += [t]

    # ---- summary narrative
    narrative = conn.execute("SELECT text FROM narratives WHERE month=?", (month,)).fetchone()
    if narrative and narrative_chars:
        text = narrative["text"].strip()
        if len(text) > narrative_chars:
            cut = text[:narrative_chars].rsplit(" ", 1)[0].rstrip(",;:")
            text = cut + "…"
        story.append(Paragraph("Summary", st_h2))
        for para in [p for p in text.split("\n") if p.strip()]:
            story.append(Paragraph(_esc(para), ParagraphStyle("n", parent=st_body, spaceAfter=3 * scale)))

    # ---- statement of activities: every category
    has_budget = any(l.get("budget") for l in income_cats + expense_cats)
    head_row = ["", "This month", "Prior month", "Fiscal YTD"] + (["Monthly budget", "Better / (worse)"]
                                                                   if has_budget else [])
    rows, styles = [head_row], []

    def add_section(label):
        rows.append([label] + [""] * (len(head_row) - 1))
        styles.extend([("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), SHADE),
                       ("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1), "Helvetica-Bold")])

    def add_line(name, cur, prior, ytd, budget=None, variance=None, bold=False, rule=False):
        r = [name, money(cur), money(prior), money(ytd)]
        if has_budget:
            r += [money(budget) if budget else "", money(variance) if variance is not None else ""]
        rows.append(r)
        i = len(rows) - 1
        if bold:
            styles.append(("FONTNAME", (0, i), (-1, i), "Helvetica-Bold"))
        if rule:
            styles.append(("LINEABOVE", (0, i), (-1, i), 0.9, INK))
        if variance is not None and variance < 0:
            styles.append(("TEXTCOLOR", (5, i), (5, i), RED))

    add_section("Income")
    funds = rep["giving"]["by_fund"]
    if combine_funds and len(funds) > 1:
        add_line(f"Online giving ({len(funds)} funds)", sum(f["amount"] for f in funds),
                 sum(f["prior"] for f in funds), sum(f["ytd"] for f in funds))
    else:
        for f in funds:
            add_line(f"Online giving: {f['name']}", f["amount"], f["prior"], f["ytd"])
    for l in income_cats:
        add_line(l["name"], l["amount"], l["prior"], l["ytd"], l.get("budget"),
                 None if not l.get("budget") else round(l["amount"] - l["budget"], 2))
    add_line("Total income", s["income"], s["prior_income"], s["ytd_income"], bold=True, rule=True)
    add_section("Expenses")
    for l in expense_cats:
        add_line(l["name"], l["amount"], l["prior"], l["ytd"], l.get("budget"), l.get("variance"))
    add_line("Total expenses", s["expense"], s["prior_expense"], s["ytd_expense"], bold=True, rule=True)
    add_line("Net income (loss)", s["net"], s["prior_net"], s["ytd_net"], bold=True, rule=True)

    ncols = len(head_row)
    first = 2.4 * inch if has_budget else 3.4 * inch
    rest = (FRAME_WIDTH - first) / (ncols - 1)
    soa = Table(rows, colWidths=[first] + [rest] * (ncols - 1), repeatRows=1, hAlign="LEFT")
    soa.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"), ("FONTSIZE", (0, 0), (-1, -1), fs),
        ("LEADING", (0, 0), (-1, -1), fs * 1.15), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, 0), fs * 0.8),
        ("TEXTCOLOR", (0, 0), (-1, 0), MUTED), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, LINE), ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ] + styles))
    if has_budget:
        note = ("Better / (worse) compares this month with its monthly budget: income above budget or "
                "spending below budget is better; red is worse than budget.")
    else:
        note = "Set monthly budgets in Settings to add budget columns."
    story += [Paragraph("Statement of activities", st_h2), soa, Spacer(1, 2 * scale), Paragraph(note, st_small)]

    # ---- second-page summaries, side by side
    def kv_table(title_text, pairs, width):
        data = [[Paragraph(f"<b>{title_text}</b>", st_body), ""]] + [[Paragraph(_esc(k), st_body), Paragraph(v, st_num)]
                                                                     for k, v in pairs]
        tt = Table(data, colWidths=[width * 0.64, width * 0.36])
        tt.setStyle(TableStyle([("SPAN", (0, 0), (1, 0)), ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
                                ("LINEBELOW", (0, 1), (-1, -1), 0.3, LINE),
                                ("TOPPADDING", (0, 0), (-1, -1), pad), ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
                                ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return tt

    g, rent, ck, rc = rep["giving"], rep["rent"], rep["checks"], rep["receipts"]
    cash = [("Beginning balance", money(bank["beginning_balance"])), ("Deposits", money(bank["deposits"])),
            ("Withdrawals", money(-bank["withdrawals"])), ("Ending balance", f"<b>{money(bank['ending_balance'])}</b>")]
    if len(bank["accounts"]) > 1:
        cash += [(f"  {a['name']}", money(a["ending"])) for a in bank["accounts"]]
    if rep["transfers_net"]:
        cash.append(("Transfers between accounts (net)", money(rep["transfers_net"])))
    giving = [("Online gifts", f"{plural(g['gifts'], 'gift')} from {plural(g['donors'], 'donor')}"), ("Online giving total", money(g["total"])),
              ("Average online gift", money(g["average_gift"])),
              ("Gifts by check (matched to deposits)", money(ck["gift_total"])),
              ("Check gifts", f"{plural(ck['gift_count'], 'check')} from {plural(ck['donor_count'], 'donor')}"),
              ("Online payment fees", money(-g["fees"])),
              ("Platform payouts deposited", money(g["payouts_deposited"]))]
    housing = [("Rent collected this month", money(rent["total"])), ("Prior month", money(rent["prior"])),
               ("Fiscal year to date", money(rent["ytd"])),
               ("Residents who paid", str(len(rent["by_payer"])))]
    controls = [("Withdrawals with a receipt", f"{rc['receipted_count']} of {rc['expense_count']}"),
                (f"Missing receipts ({money(rc['threshold'])} or more)",
                 f"{len(rc['missing'])} · {money(rc['missing_total'])}"),
                ("Uncategorized transactions", str(len(rep["uncategorized"])))]
    if abs(g["timing_difference"]) > 0.009:
        controls.append(("Online payments not yet paid out", money(g["timing_difference"])))

    gap = 0.25 * inch
    col = (FRAME_WIDTH - gap) / 2
    grid = Table([[kv_table("Cash position", cash, col), kv_table("Giving", giving, col)],
                  [kv_table("Student housing", housing, col), kv_table("Records & controls", controls, col)]],
                 colWidths=[col + gap, col], hAlign="LEFT")
    grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (0, -1), gap), ("RIGHTPADDING", (1, 0), (1, -1), 0),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 6 * scale)]))
    story += [Spacer(1, 4 * scale), KeepTogether([Paragraph("Summary figures", st_h2), grid])]
    story.append(Paragraph(
        "Cash basis. Online gifts are counted from giving-platform records at their gross amount; platform payouts "
        "deposited in the bank are not counted again. Gifts by check are included in “Donations - Cash &amp; "
        "Check”; rent paid online or by check in “Student Housing Rent”. Individual donors, residents and "
        "transactions are available from the treasurer.", st_small))

    counter = _PageCounter()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                            topMargin=0.5 * inch, bottomMargin=0.55 * inch,
                            title=f"{org} Treasurer's Report {month_label}", author=f"{org} Treasurer")

    def footer(canvas, d):
        counter(canvas, d)
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(0.55 * inch, 0.32 * inch, f"{org} · Treasurer's Report · {month_label}")
        canvas.drawRightString(LETTER[0] - 0.55 * inch, 0.32 * inch, f"Page {d.page}")
        canvas.setFillColor(BRAND)
        canvas.rect(0, LETTER[1] - 0.12 * inch, LETTER[0], 0.12 * inch, stroke=0, fill=1)
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue(), counter.pages


def build_board_pdf(conn, month, org, logo_path=None, prepared=None):
    """Return (pdf_bytes, pages). Always 2 pages or fewer."""
    prepared = prepared or date.today().isoformat()
    # (type scale, summary characters, combine giving funds), most readable first.
    # Shrink type a little, then shorten the summary, then combine funds, and
    # only then go to the smallest type.
    attempts = ([(sc, 1400, False) for sc in (1.0, 0.94, 0.88)] +
                [(sc, 700, False) for sc in (0.88, 0.82)] +
                [(sc, 700, True) for sc in (0.88, 0.82, 0.76)] +
                [(sc, 0, True) for sc in (0.82, 0.76, 0.7, 0.64)])
    last = None
    for scale, chars, combine in attempts:
        pdf, pages = _build(conn, month, org, logo_path, scale, chars, combine, prepared)
        last = (pdf, pages)
        if pages <= MAX_PAGES:
            return pdf, pages
    # Unreachable in practice (dozens of categories still fit at the smallest
    # size); never send something longer than promised without saying so.
    raise RuntimeError(f"Board report could not be fit on {MAX_PAGES} pages (got {last[1]})")
