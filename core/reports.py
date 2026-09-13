"""Pure functions that turn the transactions DataFrame into the report
tables the Streamlit app renders. Kept separate from app.py so the logic
is easy to unit-test or reuse (e.g. from a notebook) without Streamlit.
"""
import re
import pandas as pd

_REF_RE = re.compile(r"\d{8,}")


def _extract_refs(text: str) -> set:
    return set(_REF_RE.findall(text or ""))


def find_new_transactions(existing_df: pd.DataFrame, new_rows: list[dict], tolerance_days: int = 2):
    """Splits freshly-parsed rows (not yet saved) into (already_recorded, genuinely_new)
    by matching against every existing transaction — first by a shared long reference
    number (UPI ref / txn id) found in both descriptions, then by amount+direction+date
    within tolerance_days. This is what stops the same real transaction from being
    double-counted when it arrives via both an SMS tag and a later full statement/contract-
    note upload, regardless of which one you upload first."""
    if existing_df.empty:
        return [], new_rows
    existing_text = existing_df["raw_text"].fillna(existing_df["description"])
    existing_refs = existing_text.apply(_extract_refs)
    matched, unmatched = [], []
    for r in new_rows:
        r_refs = _extract_refs(r.get("raw_text") or r.get("description") or "")
        if r_refs and existing_refs.apply(lambda s: bool(s & r_refs)).any():
            matched.append(r)
            continue
        date = pd.to_datetime(r["date"])
        window = existing_df[
            (existing_df["direction"] == r["direction"])
            & (existing_df["amount"].sub(r["amount"]).abs() < 0.01)
            & (existing_df["date"].sub(date).abs() <= pd.Timedelta(days=tolerance_days))
        ]
        if not window.empty:
            matched.append(r)
        else:
            unmatched.append(r)
    return matched, unmatched


def to_dataframe(rows) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["is_office"] = df["is_office"].astype(bool)
    df["month"] = df["date"].dt.to_period("M").astype(str)
    return df


def filter_range(df: pd.DataFrame, start=None, end=None) -> pd.DataFrame:
    if df.empty:
        return df
    out = df
    if start is not None:
        out = out[out["date"] >= pd.to_datetime(start)]
    if end is not None:
        out = out[out["date"] <= pd.to_datetime(end)]
    return out


def office_expense_claim(df: pd.DataFrame) -> pd.DataFrame:
    """Office-tagged debits, grouped by bank/account — the table you'd
    hand in with a reimbursement claim."""
    if df.empty:
        return df
    subset = df[(df["is_office"]) & (df["direction"] == "debit") & (df["source"] != "contract_note")]
    if subset.empty:
        return subset
    grouped = (
        subset.groupby(["account", "bank"], dropna=False)["amount"]
        .agg(total="sum", count="count")
        .reset_index()
        .sort_values("total", ascending=False)
    )
    return grouped


def office_expense_detail(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    subset = df[(df["is_office"]) & (df["direction"] == "debit") & (df["source"] != "contract_note")]
    return subset[["date", "amount", "account", "bank", "category", "description"]].sort_values("date")


def monthly_cash_flow(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    bank_only = df[df["source"] != "contract_note"]
    pivot = bank_only.pivot_table(
        index="month", columns="direction", values="amount", aggfunc="sum", fill_value=0
    ).reset_index()
    for col in ("debit", "credit"):
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot["net"] = pivot["credit"] - pivot["debit"]
    return pivot.sort_values("month")


def category_breakdown(df: pd.DataFrame, is_office: bool | None = None) -> pd.DataFrame:
    if df.empty:
        return df
    subset = df[(df["direction"] == "debit") & (df["source"] != "contract_note")]
    if is_office is not None:
        subset = subset[subset["is_office"] == is_office]
    return (
        subset.groupby("category")["amount"]
        .agg(total="sum", count="count")
        .reset_index()
        .sort_values("total", ascending=False)
    )


def income_analysis(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    subset = df[(df["direction"] == "credit") & (df["source"] != "contract_note")]
    return (
        subset.groupby(["category", "month"])["amount"]
        .sum()
        .reset_index()
        .sort_values(["month", "category"])
    )


def personal_vs_office(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    subset = df[(df["direction"] == "debit") & (df["source"] != "contract_note")]
    subset = subset.copy()
    subset["tag"] = subset["is_office"].map({True: "Office", False: "Personal"})
    return subset.groupby("tag")["amount"].sum().reset_index()


def trading_summary(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    subset = df[df["source"] == "contract_note"]
    if subset.empty:
        return subset
    return (
        subset.groupby(["scrip", "direction"])
        .agg(quantity=("quantity", "sum"), amount=("amount", "sum"))
        .reset_index()
    )


def unmatched_between_sms_and_statement(df: pd.DataFrame, tolerance_days: int = 2) -> pd.DataFrame:
    """Flags statement entries with no SMS row (or vice versa) within a
    small date/amount tolerance — the reconciliation view."""
    if df.empty:
        return df
    sms = df[df["source"] == "sms"]
    stmt = df[df["source"] == "statement"]
    if sms.empty or stmt.empty:
        return pd.DataFrame()

    unmatched = []
    for _, s in stmt.iterrows():
        window = sms[
            (sms["direction"] == s["direction"])
            & (sms["amount"].sub(s["amount"]).abs() < 0.01)
            & (sms["date"].sub(s["date"]).abs() <= pd.Timedelta(days=tolerance_days))
        ]
        if window.empty:
            unmatched.append({**s.to_dict(), "missing_from": "sms"})
    return pd.DataFrame(unmatched)
