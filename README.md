# Wesley Treasurer

Monthly financial reports for the **VSU Wesley Foundation**. You upload bank statements, online giving records and receipts, and it produces a board-ready monthly treasurer's report.

## What it does

- **Bank statements:** imports CSV or OFX/QFX/QBO exports from online banking. With the Claude integration turned on, it also reads PDF statements. It finds the columns itself (Date/Posting Date, Amount or Debit/Credit, Balance…), skips rows it has already imported, and supports several accounts (e.g. checking and savings).
- **Online giving:** imports CSV exports from Tithe.ly, Pushpay, Givelify, PayPal, Planning Center, Vanco and similar platforms. It reads gross, fee, net, donor and fund, and skips failed or pending gifts.
- **Receipts:** accepts photos or PDFs. Each receipt is matched automatically to the bank charge with the same amount within a few days. With Claude on, the vendor, date and total are read from the image; otherwise you type them in.
- **Categorization:** editable "description contains…" rules come preloaded for common vendors (Publix, Georgia Power, Gusto, Tithe.ly…). Tick **always** when you pick a category and it becomes a rule. Claude can also suggest categories for the rest, and those are marked **AI** so you can review them.
- **Monthly report:** shows
  - a statement of activities (this month, prior month, fiscal YTD, budget, variance)
  - giving by fund
  - bank beginning and ending balances per account
  - giving fees and payout reconciliation
  - withdrawals missing receipts
  - largest payees
  - an editable narrative, which Claude can draft for you

  You can print it, save it as a PDF, or download it as CSV.

### How online giving is counted

Gifts are counted **once**, from the giving records, at their gross amount by fund. When the platform later deposits the lump-sum payout in the bank, that deposit is categorized **Online Giving Payout** and isn't counted as income again. Platform fees appear under **Bank & Processing Fees**. The report also compares net gifts to payouts deposited, so a missing payout stands out.

If you only upload bank statements, recategorize those payout deposits as **Donations – Cash & Check** (or another income category).

## Running it

Requires Python 3.10+.

```bash
pip install -r requirements.txt
python run.py            # then open http://127.0.0.1:5000
```

Data is stored in `instance/` (a SQLite database plus the receipt files). Back that folder up. Set `TREASURER_DATA_DIR` to keep it somewhere else.

### Optional settings (environment variables)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Turns on Claude: reading receipts and PDF statements, category suggestions, narrative drafts |
| `APP_PASSWORD` | Requires a password to use the app. **Set this if anyone else can reach the server.** |
| `TREASURER_DATA_DIR` | Where the database and receipts are stored |
| `HOST` / `PORT` | Listen address (default `127.0.0.1:5000`) |
| `TREASURER_MODEL` | Claude model (default `claude-opus-5-5`) |

When Claude is on, receipt images, PDF statements and transaction descriptions are sent to Anthropic's API. The narrative prompt receives report totals but not donor names.

### Settings page

- Organization name and fiscal-year start month (e.g. July for an academic year)
- The dollar amount above which a missing receipt is flagged
- Monthly budgets per category
- Categories and categorization rules

## Try it with sample data

`sample_data/` has a sample bank CSV (Sept–Oct 2026), a savings OFX file and a Tithe.ly-style giving export.

## Tests

```bash
python -m pytest
```
