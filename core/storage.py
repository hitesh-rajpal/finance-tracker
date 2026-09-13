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
    parties TEXT[]                    -- who this relates to: 'Office', 'Brother', 'Ram', 'Sham', ... (0, 1, or many)
);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS account_key TEXT;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS parties TEXT[];

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
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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
                cur.execute(
                    """INSERT INTO transactions
                       (id, date, amount, direction, account, bank, description,
                        category, is_office, source, source_file, raw_text,
                        scrip, quantity, price, charges, account_key, parties)
                       VALUES (%(id)s, %(date)s, %(amount)s, %(direction)s, %(account)s,
                        %(bank)s, %(description)s, %(category)s, %(is_office)s, %(source)s,
                        %(source_file)s, %(raw_text)s, %(scrip)s, %(quantity)s, %(price)s,
                        %(charges)s, %(account_key)s, %(parties)s)
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


def update_transaction(txn_id: str, category: str, is_office: bool, parties: list[str] | None = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transactions SET category = %s, is_office = %s, parties = %s WHERE id = %s",
                (category, is_office, parties, txn_id),
            )


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
