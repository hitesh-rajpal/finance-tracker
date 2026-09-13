# Finance Tracker — Office Expenses, Bank & Trading

A Streamlit dashboard that ingests bank SMS alerts, bank statement PDFs, and
demat contract notes, then produces office-expense claims, cash flow, and
trading reports. Data is stored in a dedicated Supabase Postgres project
(not shared with any other app), and the app itself is deployed on Streamlit
Community Cloud, gated behind a single shared password (`APP_PASSWORD` in
secrets) since it's reachable at a public URL.

## Setup (one-time)

1. **Supabase**: create a new project at supabase.com (dedicated to this app —
   don't reuse NHS Portal/schoolerplocal's project). Grab the *pooled*
   connection string from Project Settings -> Database -> Connection string ->
   "Session pooler" (port 5432).
2. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill
   in the `[supabase]` fields (from step 1) and `APP_PASSWORD` (pick your own).
   Fields are kept separate (host/user/password), not one connection-string
   URL, since a `@` in the DB password makes a single URL ambiguous to parse.
   This file is gitignored — it never gets committed or deployed via git.
3. **Streamlit Community Cloud**: push this folder to a GitHub repo (private
   is fine), then on share.streamlit.io create a new app pointing at
   `app.py` in that repo. In the app's Settings -> Secrets, paste the same
   two keys from step 2 (Streamlit Cloud secrets are separate from your local
   `secrets.toml` — set them there too).

## Run it locally

```bash
"C:\Users\hp\AppData\Local\Programs\Python\Python312\python.exe" -m streamlit run app.py
```

Or from Claude Code: the dev-server config `finance-tracker` in `.claude/launch.json`
runs it on port 5189. Either way it talks to the same Supabase database as the
deployed version — local and cloud always show the same data.

## How it works

1. **Upload tab** — drop in:
   - SMS: a `.txt` file. Best workflow — forward each bank alert to yourself
     (WhatsApp or similar) and reply directly under it with a tag. `Office` /
     `Personal` set the office/personal split (text after either becomes the
     category, e.g. `Personal Home Loan EMI`); any other freeform reply —
     `FD`, `Cashback`, `Home Loan EMI`, `Bank Interest` — is used verbatim as
     the category instead (defaults to Personal). The parser groups a
     multi-line forwarded alert into one message first, so the tag always
     pairs with the whole alert, not a fragment of it. A plain `.csv`/`.xlsx`
     SMS-backup export also works, just without tag support.
   - Bank statement PDFs — you confirm/remap the date/description/debit/credit columns
     once per statement layout (bank PDF layouts vary too much to guess blindly)
   - Demat contract note PDFs (Zerodha, ICICI Direct, Groww, etc.) — same
     confirm-the-mapping flow, plus it extracts the net settlement figure to
     match against your bank statement
2. **Review & Categorize** — untagged transactions get an auto-suggested category
   and Office/Personal split from `config/categories.py` keyword rules. Correct
   any of them here (or delete a row with its trash icon, then Save); corrections
   are remembered per-merchant so you don't re-tag the same payee twice.
3. **Accounts** — every bank/card the parsers have seen gets an auto-created row
   here (e.g. "kotak xx2071"). Relabel it (e.g. "Kotak Indian Oil Credit Card")
   and set its type (Bank Account / Credit Card / Demat/Trading / Other) — this
   updates how it shows up everywhere else without touching past transactions.
4. **Reports** — pick a date range and bank/broker filter, then see:
   - Office expense claim, grouped by bank/card, with an Excel export button
   - Monthly cash flow (credit vs debit)
   - Expense-by-category breakdown
   - Personal vs Office split
   - Income analysis by category/month
5. **Trading** — trade rows from contract notes, summarized by scrip.
6. **Reconciliation** — statement entries with no matching *tagged* SMS (same
   amount/direction within 2 days), grouped by account — shows you which
   account still has untagged/unforwarded transactions.

A "Danger zone — delete all transactions" wipe button lives in the Review tab
for clearing out test data; it doesn't touch Account labels or the
category-correction memory.

## Notes on parsing accuracy

The SMS parser handles common Indian bank alert phrasing (HDFC/ICICI/SBI/Axis/etc.)
generically via regex. The PDF parsers (statement + contract note) deliberately
show you the raw extracted table and ask you to confirm which column is which
the first time you see a given bank/broker's layout — auto-guessing debit vs
credit silently would risk corrupting every report downstream. Once mapped,
re-uploads of the same bank's statements reuse the same manual mapping choices
you pick in the UI each time (mapping isn't saved per-bank yet — a natural next
step once real statements confirm the layouts are stable per bank).

## Editing the category rules

`config/categories.py` — `CATEGORY_RULES` is an ordered list of
`(category_name, [keywords])`. `OFFICE_DEFAULT_CATEGORIES` decides which
categories default to "Office" until you override a specific transaction.
