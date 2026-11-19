"""Cross-source fund-flow reconciliation.

Three checks:

1. **5paisa ledger ↔ bank** — every `Funds Added` / `Funds Withdrawn` row in
   the 5paisa ledger is matched to the corresponding IndusInd bank
   transaction (same amount, opposite direction, within a short settlement
   window).

2. **5paisa ledger ↔ 5paisa contract notes** — the existing
   `core.fivepaisa_reconcile` checks are reused; this module only adds the
   bank leg.

3. **Bank ↔ CAS (mutual funds)** — bank debits/credits whose description
   names a mutual-fund AMC are matched to CAS unit credits/debits on the
   same or a nearby date.  The CAS does not carry rupee amounts, so this is
   a date/fund-level match, not an amount check.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

AMC_KEYWORDS = (
    "DSP", "NIPPON", "HDFC", "ICICI", "FRANKLIN", "ADITYA", "BANDHAN",
    "INVESCO", "ITI", "SUNDARAM", "TATA", "WHITEOAK", "ZERODHA", "MOTILAL",
    "AXIS", "SBI", "KOTAK", "UTI", "CANARA", "MIRAE", "PGIM", "HSBC",
    "MUTUAL FUND", "MUTUALFUND", "MF-", "AMC",
)
FIVEPAISA_KEYWORDS = ("5PAISA", "5 PAISA", "FIVE PAISA", "CASHFREE")


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        return pd.Timestamp(value).date()
    except Exception:
        return None


def _prep_bank(bank_df: pd.DataFrame) -> pd.DataFrame:
    df = bank_df.copy()
    for col in ("date", "amount", "direction", "description"):
        if col not in df.columns:
            df[col] = None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    df["description"] = df["description"].astype(str).fillna("")
    df["direction"] = df["direction"].astype(str).str.lower()
    return df.dropna(subset=["date"])


def fund_transfers(ledger_df: pd.DataFrame) -> pd.DataFrame:
    """Rows of the 5paisa ledger that represent money moving to/from the bank."""
    df = ledger_df[ledger_df["Particular"].isin(["Funds Added", "Funds Withdrawn"])].copy()
    df["ledger_date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["ledger_amount"] = pd.to_numeric(df["Net"], errors="coerce").abs().fillna(0.0)
    df["ledger_type"] = df["Particular"]
    df["expected_bank_direction"] = df["Particular"].map({
        "Funds Added": "debit",       # money leaves the bank
        "Funds Withdrawn": "credit",  # money enters the bank
    })
    return df.dropna(subset=["ledger_date"]).reset_index(drop=True)


def match_fund_transfers_to_bank(ledger_df: pd.DataFrame, bank_df: pd.DataFrame,
                                 date_window: int = 1, amount_tol: float = 0.01) -> pd.DataFrame:
    """Match each 5paisa Funds Added/Withdrawn row to an IndusInd bank row."""
    bank = _prep_bank(bank_df)
    transfers = fund_transfers(ledger_df)
    rows = []
    for _, t in transfers.iterrows():
        cand = bank[
            (bank["direction"] == t["expected_bank_direction"])
            & ((bank["amount"] - t["ledger_amount"]).abs() <= amount_tol)
        ].copy()
        if cand.empty:
            rows.append({
                "ledger_date": t["ledger_date"],
                "ledger_type": t["ledger_type"],
                "ledger_amount": t["ledger_amount"],
                "bank_date": None,
                "bank_amount": None,
                "bank_description": None,
                "day_diff": None,
                "status": "NO BANK MATCH",
            })
            continue
        cand["day_diff"] = cand["date"].apply(lambda d: abs((d - t["ledger_date"]).days))
        best = cand.sort_values("day_diff").iloc[0]
        status = "MATCHED" if best["day_diff"] <= date_window else "DATE MISMATCH"
        rows.append({
            "ledger_date": t["ledger_date"],
            "ledger_type": t["ledger_type"],
            "ledger_amount": t["ledger_amount"],
            "bank_date": best["date"],
            "bank_amount": best["amount"],
            "bank_description": best["description"],
            "day_diff": int(best["day_diff"]),
            "status": status,
        })
    return pd.DataFrame(rows)


def unmatched_bank_fivepaisa(bank_df: pd.DataFrame, ledger_df: pd.DataFrame,
                             date_window: int = 1, amount_tol: float = 0.01) -> pd.DataFrame:
    """Bank rows that look like 5paisa transfers but have no ledger entry."""
    bank = _prep_bank(bank_df)
    mask = bank["description"].str.upper().apply(
        lambda s: any(k in s for k in FIVEPAISA_KEYWORDS)
    )
    bank = bank[mask].copy()
    transfers = fund_transfers(ledger_df)
    rows = []
    for _, b in bank.iterrows():
        cand = transfers[
            (transfers["expected_bank_direction"] == b["direction"])
            & ((transfers["ledger_amount"] - b["amount"]).abs() <= amount_tol)
        ].copy()
        if cand.empty:
            rows.append({
                "bank_date": b["date"],
                "bank_amount": b["amount"],
                "bank_direction": b["direction"],
                "bank_description": b["description"],
                "status": "NO LEDGER MATCH",
            })
            continue
        cand["day_diff"] = cand["ledger_date"].apply(lambda d: abs((d - b["date"]).days))
        best = cand.sort_values("day_diff").iloc[0]
        if best["day_diff"] > date_window:
            rows.append({
                "bank_date": b["date"],
                "bank_amount": b["amount"],
                "bank_direction": b["direction"],
                "bank_description": b["description"],
                "status": f"DATE MISMATCH ({best['day_diff']} days)",
            })
    return pd.DataFrame(rows)


def _has_amc(text: str) -> bool:
    up = (text or "").upper()
    return any(k in up for k in AMC_KEYWORDS)


def match_bank_to_cas(bank_df: pd.DataFrame, cas_df: pd.DataFrame,
                      date_window: int = 3) -> pd.DataFrame:
    """Match AMC-named bank rows to CAS unit credits/debits by date."""
    bank = _prep_bank(bank_df)
    bank = bank[bank["description"].apply(_has_amc)].copy()
    cas = cas_df.copy()
    if cas.empty:
        return pd.DataFrame(columns=[
            "bank_date", "bank_amount", "bank_direction", "bank_description",
            "cas_date", "cas_fund", "cas_units", "day_diff", "status",
        ])
    cas["date"] = pd.to_datetime(cas["date"], errors="coerce").dt.date
    cas = cas.dropna(subset=["date"])

    rows = []
    for _, b in bank.iterrows():
        # money leaving the bank (debit) should add units (CAS credit)
        expected_type = "credit" if b["direction"] == "debit" else "debit"
        cand = cas[cas["type"] == expected_type].copy()
        if cand.empty:
            rows.append({
                "bank_date": b["date"], "bank_amount": b["amount"],
                "bank_direction": b["direction"], "bank_description": b["description"],
                "cas_date": None, "cas_fund": None, "cas_units": None,
                "day_diff": None, "status": "NO CAS MATCH",
            })
            continue
        cand["day_diff"] = cand["date"].apply(lambda d: abs((d - b["date"]).days))
        best = cand.sort_values("day_diff").iloc[0]
        units = best["credit"] if expected_type == "credit" else best["debit"]
        status = "MATCHED" if best["day_diff"] <= date_window else "DATE MISMATCH"
        rows.append({
            "bank_date": b["date"], "bank_amount": b["amount"],
            "bank_direction": b["direction"], "bank_description": b["description"],
            "cas_date": best["date"], "cas_fund": best["security"],
            "cas_units": units, "day_diff": int(best["day_diff"]), "status": status,
        })
    return pd.DataFrame(rows)


def summary(fund_match: pd.DataFrame, bank_unmatched: pd.DataFrame,
            cas_match: pd.DataFrame) -> dict:
    def count(df, status):
        if df is None or df.empty:
            return 0
        return int((df["status"] == status).sum())

    return {
        "fund_transfers": 0 if fund_match is None else len(fund_match),
        "fund_transfers_matched": count(fund_match, "MATCHED"),
        "fund_transfers_unmatched": 0 if fund_match is None else int(
            (fund_match["status"] != "MATCHED").sum()),
        "bank_5paisa_unmatched": 0 if bank_unmatched is None else len(bank_unmatched),
        "cas_rows": 0 if cas_match is None else len(cas_match),
        "cas_matched": count(cas_match, "MATCHED"),
        "cas_unmatched": 0 if cas_match is None else int(
            (cas_match["status"] != "MATCHED").sum()),
    }
