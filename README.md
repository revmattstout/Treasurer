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

## Putting it on the web (Render)

The repository includes a `render.yaml` that sets everything up on [Render](https://render.com). It costs about **$7.25/month**: a Starter instance plus a 1 GB disk. The free tier won't work, because it sleeps (so email isn't checked and the report isn't sent) and deletes uploaded files whenever it restarts.

1. Sign up at render.com with your GitHub account.
2. Click **New → Blueprint** and choose the `treasurer` repository.
3. Render asks for the secret settings. Fill in:
   - **APP_PASSWORD**: the password everyone will use to sign in. **Required.** The site refuses to show anything until it's set.
   - **TREASURER_EMAIL_APP_PASSWORD**: the 16-letter Gmail app password.
   - **TREASURER_EMAIL_ALLOWED_SENDERS**: the addresses allowed to email in receipts.
   - **ANTHROPIC_API_KEY**: optional. It lets Claude read receipts and PDFs and draft summaries.
   - **TREASURER_APP_URL**: leave it blank for now.
4. Click **Apply**. After a few minutes the site is live at an address like `https://wesley-treasurer.onrender.com`.
5. Copy that address into **TREASURER_APP_URL** (Environment tab), so the monthly email links back to the report.

Your data lives on the Render disk at `/var/data`. Render snapshots disks daily. For an extra copy, download each month's CSV from the report page.

Each time new code is pushed to the branch, Render redeploys automatically.

### Optional settings

Copy `.env.example` to `.env` and fill it in, or set these as environment variables. The `.env` file is git-ignored.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Turns on Claude: reading receipts and PDF statements, category suggestions, narrative drafts |
| `APP_PASSWORD` | Requires a password to use the app. **Set this if anyone else can reach the server.** |
| `TREASURER_DATA_DIR` | Where the database and receipts are stored |
| `HOST` / `PORT` | Listen address (default `127.0.0.1:5000`) |
| `TREASURER_MODEL` | Claude model (default `claude-opus-5-5`) |
| `TREASURER_EMAIL_ADDRESS`, `TREASURER_EMAIL_APP_PASSWORD`, `TREASURER_EMAIL_ALLOWED_SENDERS`, `TREASURER_EMAIL_POLL_MINUTES` | Email import (see below) |

When Claude is on, receipt images, PDF statements and transaction descriptions are sent to Anthropic's API. The narrative prompt receives report totals but not donor names.

## Emailing files in

You can email attachments to a dedicated inbox, and the app imports them the same way as uploads.

**Setup (Gmail):**

1. Sign in to the Gmail account and turn on **2-Step Verification** (Google Account → Security).
2. On the same page, create an **App password** named "Treasurer". Google shows 16 letters.
3. In `.env`, set:
   - `TREASURER_EMAIL_ADDRESS` to the Gmail address
   - `TREASURER_EMAIL_APP_PASSWORD` to the 16 letters
   - `TREASURER_EMAIL_ALLOWED_SENDERS` to the addresses you'll send from, separated by commas
4. Restart the app.

Gmail won't accept the account's regular password here, so you need the app password. Leave IMAP on in Gmail's settings (it is by default).

**What happens to each attachment:**

- **Photos and non-statement PDFs** become receipts. The subject and the first line of the message become the receipt's purpose, for example "Pizza for Tuesday worship".
- **OFX/QFX files and bank CSVs** are imported as bank statements. Put **statement** in the subject for a PDF statement. Add **checking** or **savings** to the subject to label the account.
- **Giving CSVs** are imported as gifts. They're recognized by their columns, or put **giving** or the platform name in the subject.

**Security:**

- Mail is accepted only from `TREASURER_EMAIL_ALLOWED_SENDERS`.
- It must also pass Gmail's DMARC/DKIM sender checks, so a forged "From" address is rejected.
- Each email is imported only once.
- The Upload page shows a log of every email checked and what was done with it.

**When the inbox is checked:**

- When you click **Check email now** on the Upload page.
- Every `TREASURER_EMAIL_POLL_MINUTES` minutes while the app is running.
- When you run `python -m treasurer.mailbox` from cron or Task Scheduler. Add `--watch 10` to keep it running.

## Monthly report email

On the **third Wednesday of each month** at 8 AM Eastern, last month's report is emailed to **revmattstout@gmail.com**. You can change the recipients, send time, or turn it off under **Settings → Monthly report email**.

**What's in the email:**

- The summary numbers, the statement of activities, bank and giving totals, and the list of expenses missing receipts.
- The summary narrative, if you've written one. If you haven't and Claude is on, Claude drafts one, labeled as a draft.
- Attachments: the transaction detail (CSV) and the full report (HTML).
- If nothing has been uploaded for that month yet, the email arrives as a "(no data yet)" reminder instead.

The email uses the Gmail account and app password from the email-import setup above. Sending is set up as soon as `TREASURER_EMAIL_ADDRESS` and `TREASURER_EMAIL_APP_PASSWORD` are set.

**When it goes out:**

- While the app is running. If the computer was off on the day, the report goes out the next time the app runs that month.
- Each report is sent automatically only once. A failed send is retried up to 3 times, and the history is shown on the Settings page.
- If the app isn't always running, schedule `python -m treasurer.mailer` to run daily (cron or Task Scheduler). It only sends when a report is due.
- To send one right away, use **Email this report** on any report page, or run `python -m treasurer.mailer --month 2026-10 --now`.

Optional settings:

- `TREASURER_APP_URL` (e.g. `http://192.168.1.20:5000`) adds a link back to the app in the email.
- `TREASURER_TIMEZONE` changes the time zone (default `America/New_York`).

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
