"""Postgres/Supabase-backed storage.

Connection comes from st.secrets["supabase"] — a table of discrete fields
(host/port/dbname/user/password) rather than a single connection-string URL,
since a password containing "@" or other URL-reserved characters makes a
single-string DSN ambiguous to parse. Set locally in .streamlit/secrets.toml
(gitignored) and in the app's Secrets on Streamlit Community Cloud when
deployed. Use Supabase's *Session pooler* host (port 5432) rather than the
direct-connection host — a long-lived Streamlit process plus Supabase's low
direct-connection cap on the free tier otherwise exhausts connections
quickly.

A small psycopg2 pool is kept across Streamlit reruns via st.cache_resource
so we don't open a fresh connection on every script rerun.
"""
import hashlib
import uuid
from contextlib import contextmanager

import streamlit as st
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    date DATE NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    direction TEXT NOT NULL,          -- 'debit' or 'credit'
    account TEXT,                     -- e.g. 'HDFC XX1234', 'ICICI Card XX9876'
    bank TEXT,
    description TEXT,
    category TEXT,
    is_office BOOLEAN DEFAULT FALSE,
    source TEXT NOT NULL,             -- 'sms' | 'statement' | 'contract_note'
    source_file TEXT,
    raw_text TEXT,
    scrip TEXT,                       -- for trading rows only
    quantity DOUBLE PRECISION,
    price DOUBLE PRECISION,
    charges DOUBLE PRECISION,
    account_key TEXT,                 -- normalized join key into account_master
    parties TEXT[],                   -- who this relates to: 'Office', 'Brother', 'Ram', 'Sham', ... (0, 1, or many)
    edit_source TEXT,                 -- how category/parties were set: 'sms_tag' | 'bank_category' | 'auto' | 'manual'
    upload_batch_id TEXT,             -- which upload this row came from (upload_batches.id)
    particulars TEXT,                 -- freeform note, editable in Review & Categorize
    month_tag TEXT,                   -- 'YYYYMM', defaults from date but editable (e.g. to book a late-cycle txn into the next month)
    tags TEXT[],                      -- freeform activity tags, e.g. 'Lunch', 'Purchased Card for Mr. abcd' (0, 1, or many)
    is_suspense BOOLEAN DEFAULT FALSE -- flagged as not trackable right now; parked here to investigate/tag properly later
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS account_key TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS parties TEXT[];
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS edit_source TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS upload_batch_id TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS particulars TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS month_tag TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS tags TEXT[];
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS is_suspense BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS category_overrides (
    description_key TEXT PRIMARY KEY,  -- normalized merchant/description snippet
    category TEXT NOT NULL,
    is_office BOOLEAN NOT NULL
);

-- One row per distinct account/card the parsers have seen (auto-created on
-- first sight from e.g. "Kotak XX2071"). label/account_type start out as
-- a guess and are meant to be edited in the Accounts tab (e.g. relabeled
-- "Kotak Indian Oil Credit Card", type "Credit Card") without needing to
-- touch every past transaction.
CREATE TABLE IF NOT EXISTS account_master (
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    account_type TEXT
);

-- Known "linked to" parties (Office, or a specific person like Brother/Ram/
-- Sham) a transaction's `parties` array can reference. Auto-populated the
-- first time a name is used; 'office' (case-insensitive) is treated
-- specially as the driver of the Office-expense-claim reports.
CREATE TABLE IF NOT EXISTS party_master (
    name TEXT PRIMARY KEY
);

-- A recurring statement source (e.g. "BPCL SBI Card"), keyed by a label you
-- choose, remembering whatever's needed to process next month's statement
-- from the same source without re-entering it: the PDF password if any
-- (plaintext — inherent to the feature, the app has to hand the literal
-- password to the PDF reader each time, no different in kind from the DB/
-- API credentials already held in this same database), and the bank name /
-- account label to pre-fill instead of typing them again.
CREATE TABLE IF NOT EXISTS pdf_password_master (
    label TEXT PRIMARY KEY,
    password TEXT,
    bank TEXT,
    account TEXT
);
ALTER TABLE pdf_password_master ALTER COLUMN password DROP NOT NULL;
ALTER TABLE pdf_password_master ADD COLUMN IF NOT EXISTS bank TEXT;
ALTER TABLE pdf_password_master ADD COLUMN IF NOT EXISTS account TEXT;

-- Things you EXPECT to happen (savings interest at a rate, credit-card
-- cashback at a rate, a home/personal loan EMI on its amortization
-- schedule, or any other fixed recurring transfer to/from a bank,
-- institute, your office, or a relative) so actual transactions can be
-- cross-checked against them rather than just recorded.
CREATE TABLE IF NOT EXISTS financial_rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    rule_type TEXT NOT NULL,          -- 'Interest' | 'Cashback' | 'Loan EMI' | 'Recurring'
    counterparty_type TEXT,           -- 'Bank' | 'Institute' | 'Office' | 'Relative' | 'Other'
    counterparty_name TEXT,
    account_key TEXT,                 -- which account (from account_master) this checks against
    match_category TEXT,              -- which transaction category counts as the "actual" side
    -- Interest / Cashback (rate x basis)
    rate_percent DOUBLE PRECISION,
    basis_amount DOUBLE PRECISION,    -- for Interest: a balance you supply (not tracked automatically)
    basis_category TEXT,              -- for Cashback: which spend category the rate applies to (blank = all spend on the account)
    frequency TEXT,                   -- 'Monthly' | 'Quarterly' | 'Yearly'
    -- Loan EMI (full amortization schedule)
    principal DOUBLE PRECISION,
    annual_rate DOUBLE PRECISION,
    tenure_months INTEGER,
    start_date DATE,
    -- Recurring (fixed amount, e.g. salary / rent / a relative's monthly transfer)
    expected_amount DOUBLE PRECISION,
    due_day INTEGER,
    active BOOLEAN DEFAULT TRUE
);

-- Rate resets for a floating-rate Loan EMI rule. At each effective_date, the
-- EMI is recalculated on the outstanding balance at that point (for
-- whatever tenure remains), matching the usual bank convention of a fixed
-- payoff date with an adjusting installment amount.
CREATE TABLE IF NOT EXISTS loan_rate_changes (
    id TEXT PRIMARY KEY,
    rule_id TEXT NOT NULL,
    effective_date DATE NOT NULL,
    new_annual_rate DOUBLE PRECISION NOT NULL
);

-- One row per "Parse & save" / "Add transactions" click, so you can see
-- exactly what you uploaded and when, and delete a whole upload's worth of
-- transactions in one go.
CREATE TABLE IF NOT EXISTS upload_batches (
    id TEXT PRIMARY KEY,
    uploaded_at TIMESTAMP DEFAULT now(),
    source_file TEXT,
    source_type TEXT,                 -- 'sms' | 'statement' | 'contract_note'
    parsed_count INTEGER,
    saved_count INTEGER,
    skipped_count INTEGER,
    file_storage_path TEXT            -- path in Supabase Storage of the original uploaded file, if kept
);
ALTER TABLE upload_batches ADD COLUMN IF NOT EXISTS file_storage_path TEXT;

-- Rows from an upload that were SKIPPED because they matched something
-- already recorded — kept so you can see exactly which incoming message/line
-- matched which existing transaction, and why (reference number vs amount+date).
CREATE TABLE IF NOT EXISTS upload_matches (
    id TEXT PRIMARY KEY,
    batch_id TEXT,
    new_raw_text TEXT,
    new_amount DOUBLE PRECISION,
    new_date DATE,
    matched_transaction_id TEXT,
    match_reason TEXT                 -- 'reference_number' | 'amount_date'
);

-- One row per "Generate claim" click: a snapshot of which transactions were
-- handed to accounts together as one reimbursement statement, and where
-- that statement is in its lifecycle. The Excel itself isn't stored — it's
-- rebuilt on demand from transaction_ids, so it always matches the DB.
CREATE TABLE IF NOT EXISTS expense_claims (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    transaction_ids TEXT[] NOT NULL,
    total_amount DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL DEFAULT 'Generated',  -- 'Generated' | 'Given to Accounts' | 'Payment Received'
    given_to_accounts_at DATE,
    payment_id TEXT                   -- set once paid; links to expense_claim_payments.id
);

-- A ULIP/investment policy's static details — premium schedule, sum assured
-- etc. Fund value itself is NOT here: that's a point-in-time snapshot (see
-- investment_value_snapshots below) since it moves with the market.
CREATE TABLE IF NOT EXISTS investment_policies (
    key TEXT PRIMARY KEY,              -- user-chosen short key, e.g. 'indusind_nippon_ulip'
    label TEXT NOT NULL,               -- e.g. 'IndusInd Nippon Life Premier (ULIP)'
    insurer TEXT,
    policy_no TEXT,
    policy_type TEXT,                  -- e.g. 'ULIP'
    premium_amount DOUBLE PRECISION,
    premium_frequency TEXT,            -- 'Yearly' | 'Quarterly' | 'Monthly'
    next_premium_due DATE,
    sum_assured DOUBLE PRECISION
);

-- One row per time the fund value was checked. total_value is what's shown
-- as the headline number; the actual fund-by-fund split lives in
-- investment_value_funds below, since which funds exist and what they're
-- named can change over time (switches, insurer renaming a fund, etc.) —
-- never assume a fixed set of fund columns.
CREATE TABLE IF NOT EXISTS investment_value_snapshots (
    id TEXT PRIMARY KEY,
    policy_key TEXT NOT NULL,
    as_of_date DATE NOT NULL,
    total_value DOUBLE PRECISION NOT NULL,
    source TEXT,                       -- how this number was obtained, e.g. 'manual' | 'api'
    created_at TIMESTAMP DEFAULT now()
);

-- Per-fund breakdown for one snapshot. fund_name is free text on purpose —
-- insurers add/rename/merge funds, so this must never be an enum or a fixed
-- column set.
CREATE TABLE IF NOT EXISTS investment_value_funds (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    fund_name TEXT NOT NULL,
    units DOUBLE PRECISION,
    nav DOUBLE PRECISION,
    value DOUBLE PRECISION NOT NULL
);

-- A single payment from accounts, which can cover more than one claim at
-- once (e.g. two months' statements settled together in one transfer) —
-- every expense_claims row it covers points back to it via payment_id.
CREATE TABLE IF NOT EXISTS expense_claim_payments (
    id TEXT PRIMARY KEY,
    payment_date DATE NOT NULL,
    amount DOUBLE PRECISION NOT NULL,
    note TEXT,
    created_at TIMESTAMP DEFAULT now()
);
"""


@st.cache_resource
def _pool() -> SimpleConnectionPool:
    db = st.secrets["supabase"]
    return SimpleConnectionPool(
        1, 5,
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"],
    )


@contextmanager
def get_conn():
    pool = _pool()
    conn = pool.getconn()
    broken = False
    try:
        yield conn
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # A pooled connection can go stale while idle — Supabase's pooler
        # drops idle connections after a timeout, and a Streamlit Cloud app
        # can sit unused for hours between visits. Once a connection is
        # dead, calling rollback() on it just raises a second, more
        # confusing error that buries the real one, and handing it back to
        # the pool would make the next request fail the same way. Close
        # every connection in the pool (all opened around the same time,
        # so equally likely stale) and drop the cached pool object so the
        # very next action opens fresh ones instead of repeating this.
        broken = True
        try:
            pool.closeall()
        except Exception:
            pass
        _pool.clear()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        if not broken:
            pool.putconn(conn)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)


def make_txn_id(date: str, amount: float, description: str, source: str) -> str:
    raw = f"{date}|{amount}|{description}|{source}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def upsert_transactions(rows: list[dict]) -> int:
    """Insert rows, skipping ones whose id already exists (dedupe across
    re-uploads / overlapping SMS+statement coverage). Returns count inserted."""
    if not rows:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            inserted = 0
            for r in rows:
                r.setdefault("account_key", None)
                r.setdefault("parties", None)
                r.setdefault("edit_source", None)
                r.setdefault("upload_batch_id", None)
                r.setdefault("particulars", None)
                r.setdefault("tags", None)
                r.setdefault("is_suspense", False)
                if not r.get("month_tag"):
                    date_val = r.get("date")
                    r["month_tag"] = str(date_val).replace("-", "")[:6] if date_val else None
                cur.execute(
                    """INSERT INTO transactions
                       (id, date, amount, direction, account, bank, description,
                        category, is_office, source, source_file, raw_text,
                        scrip, quantity, price, charges, account_key, parties,
                        edit_source, upload_batch_id, particulars, month_tag, tags, is_suspense)
                       VALUES (%(id)s, %(date)s, %(amount)s, %(direction)s, %(account)s,
                        %(bank)s, %(description)s, %(category)s, %(is_office)s, %(source)s,
                        %(source_file)s, %(raw_text)s, %(scrip)s, %(quantity)s, %(price)s,
                        %(charges)s, %(account_key)s, %(parties)s, %(edit_source)s, %(upload_batch_id)s,
                        %(particulars)s, %(month_tag)s, %(tags)s, %(is_suspense)s)
                       ON CONFLICT (id) DO NOTHING""",
                    r,
                )
                inserted += cur.rowcount
    return inserted


def delete_transactions(ids: list[str]):
    if not ids:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions WHERE id = ANY(%s)", (ids,))


def fetch_all() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM transactions ORDER BY date")
            return cur.fetchall()


def get_batch_transactions(batch_id: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM transactions WHERE upload_batch_id = %s ORDER BY date", (batch_id,))
            return cur.fetchall()


def update_transaction(txn_id: str, category: str, is_office: bool, parties: list[str] | None = None,
                        particulars: str | None = None, month_tag: str | None = None,
                        tags: list[str] | None = None, is_suspense: bool = False):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET category = %s, is_office = %s, parties = %s, particulars = %s, "
                "month_tag = %s, tags = %s, is_suspense = %s, edit_source = 'manual' WHERE id = %s",
                (category, is_office, parties, particulars, month_tag, tags, is_suspense, txn_id),
            )


def save_upload_batch(batch: dict):
    batch.setdefault("file_storage_path", None)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO upload_batches
                   (id, source_file, source_type, parsed_count, saved_count, skipped_count, file_storage_path)
                   VALUES (%(id)s, %(source_file)s, %(source_type)s, %(parsed_count)s, %(saved_count)s,
                    %(skipped_count)s, %(file_storage_path)s)""",
                batch,
            )


def get_upload_batches() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM upload_batches ORDER BY uploaded_at DESC")
            return cur.fetchall()


def save_upload_matches(matches: list[dict]):
    if not matches:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            for m in matches:
                cur.execute(
                    """INSERT INTO upload_matches
                       (id, batch_id, new_raw_text, new_amount, new_date, matched_transaction_id, match_reason)
                       VALUES (%(id)s, %(batch_id)s, %(new_raw_text)s, %(new_amount)s, %(new_date)s,
                        %(matched_transaction_id)s, %(match_reason)s)""",
                    m,
                )


def get_upload_matches(batch_id: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT m.*, t.description AS matched_description, t.date AS matched_date,
                          t.amount AS matched_amount, t.source AS matched_source,
                          t.source_file AS matched_source_file
                   FROM upload_matches m
                   LEFT JOIN transactions t ON t.id = m.matched_transaction_id
                   WHERE m.batch_id = %s""",
                (batch_id,),
            )
            return cur.fetchall()


def delete_upload_batch(batch_id: str, source_file: str):
    """Deletes the batch record, its match log, and every transaction that
    came from that upload (matched by upload_batch_id — falls back to
    source_file for pre-existing rows saved before batch tracking existed)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM transactions WHERE upload_batch_id = %s OR (upload_batch_id IS NULL AND source_file = %s)",
                (batch_id, source_file),
            )
            cur.execute("DELETE FROM upload_matches WHERE batch_id = %s", (batch_id,))
            cur.execute("DELETE FROM upload_batches WHERE id = %s", (batch_id,))


def ensure_party(name: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO party_master (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                (name,),
            )


def get_party_master() -> list[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM party_master ORDER BY name")
            return [r[0] for r in cur.fetchall()]


def delete_party(name: str):
    """Removes a name from the master list only — does not touch any
    transaction that already references it in its parties array."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM party_master WHERE name = %s", (name,))


def rename_party(old_name: str, new_name: str):
    """Renames in the master list AND backfills every transaction whose
    parties array references the old name, so relabeling doesn't orphan
    past transactions the way it would if we only touched party_master."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET parties = array_replace(parties, %s, %s) WHERE %s = ANY(parties)",
                (old_name, new_name, old_name),
            )
            cur.execute("DELETE FROM party_master WHERE name = %s", (old_name,))
            cur.execute("INSERT INTO party_master (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (new_name,))


def save_statement_profile(label: str, password: str | None = None, bank: str | None = None,
                            account: str | None = None):
    """Upserts a statement-source profile by label. Any field left as None
    (e.g. saving just the password before the bank/account fields are even
    shown, or saving bank/account for a source that needs no password) keeps
    whatever was already stored for that field, rather than wiping it out."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pdf_password_master (label, password, bank, account)
                   VALUES (%(label)s, %(password)s, %(bank)s, %(account)s)
                   ON CONFLICT (label) DO UPDATE SET
                     password = COALESCE(EXCLUDED.password, pdf_password_master.password),
                     bank = COALESCE(EXCLUDED.bank, pdf_password_master.bank),
                     account = COALESCE(EXCLUDED.account, pdf_password_master.account)""",
                {"label": label, "password": password, "bank": bank, "account": account},
            )


def get_pdf_passwords() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM pdf_password_master ORDER BY label")
            return cur.fetchall()


def delete_pdf_password(label: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pdf_password_master WHERE label = %s", (label,))


def save_override(description_key: str, category: str, is_office: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO category_overrides (description_key, category, is_office)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (description_key) DO UPDATE SET
                     category = EXCLUDED.category, is_office = EXCLUDED.is_office""",
                (description_key, category, is_office),
            )


def get_overrides() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM category_overrides")
            rows = cur.fetchall()
    return {r["description_key"]: (r["category"], bool(r["is_office"])) for r in rows}


def save_investment_policy(key: str, label: str, insurer: str | None = None, policy_no: str | None = None,
                            policy_type: str | None = None, premium_amount: float | None = None,
                            premium_frequency: str | None = None, next_premium_due: str | None = None,
                            sum_assured: float | None = None):
    """Upserts a policy's static details by key — a field left as None keeps
    whatever was already stored, same COALESCE pattern as save_statement_profile."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO investment_policies
                     (key, label, insurer, policy_no, policy_type, premium_amount,
                      premium_frequency, next_premium_due, sum_assured)
                   VALUES (%(key)s, %(label)s, %(insurer)s, %(policy_no)s, %(policy_type)s,
                           %(premium_amount)s, %(premium_frequency)s, %(next_premium_due)s, %(sum_assured)s)
                   ON CONFLICT (key) DO UPDATE SET
                     label = EXCLUDED.label,
                     insurer = COALESCE(EXCLUDED.insurer, investment_policies.insurer),
                     policy_no = COALESCE(EXCLUDED.policy_no, investment_policies.policy_no),
                     policy_type = COALESCE(EXCLUDED.policy_type, investment_policies.policy_type),
                     premium_amount = COALESCE(EXCLUDED.premium_amount, investment_policies.premium_amount),
                     premium_frequency = COALESCE(EXCLUDED.premium_frequency, investment_policies.premium_frequency),
                     next_premium_due = COALESCE(EXCLUDED.next_premium_due, investment_policies.next_premium_due),
                     sum_assured = COALESCE(EXCLUDED.sum_assured, investment_policies.sum_assured)""",
                {"key": key, "label": label, "insurer": insurer, "policy_no": policy_no,
                 "policy_type": policy_type, "premium_amount": premium_amount,
                 "premium_frequency": premium_frequency, "next_premium_due": next_premium_due,
                 "sum_assured": sum_assured},
            )


def get_investment_policies() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM investment_policies ORDER BY label")
            return cur.fetchall()


def save_investment_snapshot(policy_key: str, as_of_date: str, total_value: float,
                              funds: list[dict], source: str = "manual") -> str:
    """funds: [{'fund_name':..., 'units':..., 'nav':..., 'value':...}, ...]
    Returns the new snapshot's id."""
    snapshot_id = str(uuid.uuid4())
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO investment_value_snapshots (id, policy_key, as_of_date, total_value, source)
                   VALUES (%s, %s, %s, %s, %s)""",
                (snapshot_id, policy_key, as_of_date, total_value, source),
            )
            for f in funds:
                cur.execute(
                    """INSERT INTO investment_value_funds (id, snapshot_id, fund_name, units, nav, value)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (str(uuid.uuid4()), snapshot_id, f["fund_name"], f.get("units"), f.get("nav"), f["value"]),
                )
    return snapshot_id


def get_investment_snapshots(policy_key: str | None = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if policy_key:
                cur.execute(
                    "SELECT * FROM investment_value_snapshots WHERE policy_key = %s ORDER BY as_of_date DESC",
                    (policy_key,),
                )
            else:
                cur.execute("SELECT * FROM investment_value_snapshots ORDER BY as_of_date DESC")
            return cur.fetchall()


def get_investment_snapshot_funds(snapshot_id: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM investment_value_funds WHERE snapshot_id = %s ORDER BY fund_name",
                        (snapshot_id,))
            return cur.fetchall()


def delete_investment_snapshot(snapshot_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM investment_value_funds WHERE snapshot_id = %s", (snapshot_id,))
            cur.execute("DELETE FROM investment_value_snapshots WHERE id = %s", (snapshot_id,))


def ensure_account(key: str, default_label: str):
    """Auto-create a master row the first time an account/card key is seen,
    so it shows up in the Accounts tab to be relabeled — never overwrites an
    existing label."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO account_master (key, label, account_type)
                   VALUES (%s, %s, NULL)
                   ON CONFLICT (key) DO NOTHING""",
                (key, default_label),
            )


def get_account_master() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM account_master ORDER BY label")
            return cur.fetchall()


def update_account(key: str, label: str, account_type: str | None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE account_master SET label = %s, account_type = %s WHERE key = %s",
                (label, account_type, key),
            )


def delete_all():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transactions")


_RULE_COLUMNS = [
    "id", "name", "rule_type", "counterparty_type", "counterparty_name", "account_key",
    "match_category", "rate_percent", "basis_amount", "basis_category", "frequency",
    "principal", "annual_rate", "tenure_months", "start_date", "expected_amount",
    "due_day", "active",
]


def save_rule(rule: dict):
    rule.setdefault("active", True)
    for c in _RULE_COLUMNS:
        rule.setdefault(c, None)
    cols = ", ".join(_RULE_COLUMNS)
    placeholders = ", ".join(f"%({c})s" for c in _RULE_COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _RULE_COLUMNS if c != "id")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO financial_rules ({cols}) VALUES ({placeholders})
                    ON CONFLICT (id) DO UPDATE SET {updates}""",
                rule,
            )


def get_rules(active_only: bool = True) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = "SELECT * FROM financial_rules"
            if active_only:
                q += " WHERE active = TRUE"
            q += " ORDER BY name"
            cur.execute(q)
            return cur.fetchall()


def delete_rule(rule_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM financial_rules WHERE id = %s", (rule_id,))
            cur.execute("DELETE FROM loan_rate_changes WHERE rule_id = %s", (rule_id,))


def save_rate_change(change: dict):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO loan_rate_changes (id, rule_id, effective_date, new_annual_rate)
                   VALUES (%(id)s, %(rule_id)s, %(effective_date)s, %(new_annual_rate)s)""",
                change,
            )


def get_rate_changes(rule_id: str) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM loan_rate_changes WHERE rule_id = %s ORDER BY effective_date", (rule_id,)
            )
            return cur.fetchall()


def delete_rate_change(change_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM loan_rate_changes WHERE id = %s", (change_id,))


def save_expense_claim(claim: dict):
    claim.setdefault("status", "Generated")
    claim.setdefault("given_to_accounts_at", None)
    claim.setdefault("payment_id", None)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO expense_claims
                   (id, label, transaction_ids, total_amount, status, given_to_accounts_at, payment_id)
                   VALUES (%(id)s, %(label)s, %(transaction_ids)s, %(total_amount)s, %(status)s,
                    %(given_to_accounts_at)s, %(payment_id)s)""",
                claim,
            )


def get_expense_claims() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM expense_claims ORDER BY created_at DESC")
            return cur.fetchall()


def mark_claim_given_to_accounts(claim_id: str, given_date):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE expense_claims SET status = 'Given to Accounts', given_to_accounts_at = %s WHERE id = %s",
                (given_date, claim_id),
            )


def delete_expense_claim(claim_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM expense_claims WHERE id = %s", (claim_id,))


def save_claim_payment(payment: dict, claim_ids: list[str]):
    """Records a payment — possibly covering several claims settled together
    in one transfer — and marks every one of those claims Payment Received,
    linked back to this payment."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO expense_claim_payments (id, payment_date, amount, note)
                   VALUES (%(id)s, %(payment_date)s, %(amount)s, %(note)s)""",
                payment,
            )
            cur.execute(
                "UPDATE expense_claims SET status = 'Payment Received', payment_id = %s WHERE id = ANY(%s)",
                (payment["id"], claim_ids),
            )


def get_claim_payments() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM expense_claim_payments ORDER BY payment_date DESC")
            return cur.fetchall()


def delete_claim_payment(payment_id: str):
    """Undoes a payment: reverts every claim it covered back to 'Given to
    Accounts' and removes the payment record."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE expense_claims SET status = 'Given to Accounts', payment_id = NULL WHERE payment_id = %s",
                (payment_id,),
            )
            cur.execute("DELETE FROM expense_claim_payments WHERE id = %s", (payment_id,))
