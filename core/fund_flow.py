"""Cross-source fund-flow reconciliation.

Three checks:

1. **5paisa ledger ↔ bank** — every `Funds Added` / `Funds Withdrawn` row in
   the 5paisa ledger is matched one-to-one to the corresponding IndusInd bank
   transaction (same amount, opposite direction, within a short settlement
   window).  One-to-one matters: two same-sized transfers on nearby days must
   not both claim the same bank row.

2. **Bank rows that look like 5paisa but have no ledger entry** — the inverse
   check, using the bank rows left over after the match above.

3. **Bank ↔ CAS (mutual funds)** — bank debits/credits whose description
   clearly names a mutual-fund purchase/redemption are matched to CAS *unit*
   transactions for the same AMC on the same or a nearby date.  The CAS does
   not carry rupee amounts, so this is a date/fund-level match, not an amount
   check.  Equity rows in the CAS (ISINs starting `INE`) are ignored, and
   AMC dividends / clearing-house settlements are not treated as purchases.
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

# Rows that genuinely indicate a direct mutual-fund purchase/redemption.
MF_STRONG_MARKERS = (
    "STARMF", "BILLDESK", "MFE BANKING", "MUTUAL FUND", "MUTUALFUND",
    "ASSET MANAGEMENT", "AMC LTD", " AMC ", "MF-",
)
# Things that contain an AMC name but are not a purchase/redemption.
MF_EXCLUDE_MARKERS = (
    "DIVID", "DIVIDEND", "INDIAN CLE", "CLEARING", "CLEARIN",
    "VENTURA", "5PAISA", "5 PAISA", "CASHFREE", "TATA MOTORS",
    "EQUITY SHARES", "BSE ", "NSE ", "IPFUND", "INITIAL FUNDING",
)
# (token looked for in the bank description, token looked for in the CAS security)
AMC_TOKENS = (
    ("NIPPON", "NIPPON"),
    ("DSP", "DSP"),
    ("FRANKLIN", "FRANKLIN"),
    ("INVESCO", "INVESCO"),
    ("BANDHAN", "BANDHAN"),
    ("EDELWEISS", "EDELWEISS"),
    ("QUANT", "QUANT"),
    ("PARAG", "PARAG"),
    ("HDFC MF", "HDFC"),
    ("HDFCMUTUAL", "HDFC"),
    ("ICICI PRU", "ICICI"),
    ("ICICIPRU", "ICICI"),
    ("ICICIMF", "ICICI"),      # BILLDESK: "Icicimfarnitrage"
    ("ICICI MF", "ICICI"),
    ("SBI MF", "SBI"),
    ("SBIMAGNUM", "SBI"),      # BILLDESK: "Sbimagnum"
    ("SBI MUTUAL", "SBI"),
    ("KOTAK", "KOTAK"),
    ("UTI", "UTI"),
    ("CANARA", "CANARA"),
    ("MIRAE", "MIRAE"),
    ("PGIM", "PGIM"),
    ("HSBC", "HSBC"),
    ("TATA MF", "TATA"),
    ("TATAMF", "TATA"),
    ("ADITYA", "ADITYA"),
    ("ABSL", "ADITYA"),
    ("AXIS", "AXIS"),
    ("MOTILAL", "MOTILAL"),
    ("SUNDARAM", "SUNDARAM"),
    ("ZERODHA", "ZERODHA"),
    ("WHITEOAK", "WHITEOAK"),
    ("ITI", "ITI"),
    ("NAVI", "NAVI"),
    ("SAMCO", "SAMCO"),
    ("GROWW", "GROWW"),
)
FIVEPAISA_KEYWORDS = ("5PAISA", "5 PAISA", "FIVE PAISA", "CASHFREE", "5PAI")


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
    df = df.dropna(subset=["date"])
    # The archive contains overlapping statements (quarterly + ad-hoc).  The
    # closing balance identifies the same transaction even when the extracted
    # narration differs by a newline, so dedupe on it when it is available.
    if "balance" in df.columns:
        df["balance"] = pd.to_numeric(df["balance"], errors="coerce")
        with_bal = df.dropna(subset=["balance"])
        without = df[df["balance"].isna()]
        with_bal = with_bal.drop_duplicates(subset=["date", "amount", "direction", "balance"])
        df = pd.concat([with_bal, without], ignore_index=True)
        df = df.drop_duplicates(subset=["date", "amount", "direction", "balance"])
    df = df.drop_duplicates(subset=["date", "amount", "direction", "description"])
    return df.sort_values("date").reset_index(drop=True)


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
    return df.dropna(subset=["ledger_date"]).sort_values("ledger_date").reset_index(drop=True)


def _greedy_match(transfers: pd.DataFrame, bank: pd.DataFrame,
                  date_window: int, amount_tol: float) -> tuple[pd.DataFrame, set[int]]:
    """Match transfers one-to-one to bank rows, nearest date first."""
    used: set[int] = set()
    rows = []
    for _, t in transfers.iterrows():
        cand = bank[
            (bank["direction"] == t["expected_bank_direction"])
            & ((bank["amount"] - t["ledger_amount"]).abs() <= amount_tol)
            & (~bank.index.isin(used))
        ].copy()
        if cand.empty:
            # Was there a bank row that matched exactly but was already used?
            dup = bank[
                (bank["direction"] == t["expected_bank_direction"])
                & ((bank["amount"] - t["ledger_amount"]).abs() <= amount_tol)
                & (bank.index.isin(used))
            ]
            rows.append({
                "ledger_date": t["ledger_date"],
                "ledger_type": t["ledger_type"],
                "ledger_amount": t["ledger_amount"],
                "bank_date": None,
                "bank_amount": None,
                "bank_description": None,
                "day_diff": None,
                "bank_index": None,
                "status": "DUPLICATE LEDGER ROW" if not dup.empty else "NO BANK MATCH",
            })
            continue
        cand["day_diff"] = cand["date"].apply(lambda d: abs((d - t["ledger_date"]).days))
        best_idx = cand.sort_values(["day_diff", "date"]).index[0]
        best = cand.loc[best_idx]
        used.add(best_idx)
        status = "MATCHED" if best["day_diff"] <= date_window else "DATE MISMATCH"
        rows.append({
            "ledger_date": t["ledger_date"],
            "ledger_type": t["ledger_type"],
            "ledger_amount": t["ledger_amount"],
            "bank_date": best["date"],
            "bank_amount": best["amount"],
            "bank_description": best["description"],
            "day_diff": int(best["day_diff"]),
            "bank_index": int(best_idx),
            "status": status,
        })
    return pd.DataFrame(rows), used


def match_fund_transfers_to_bank(ledger_df: pd.DataFrame, bank_df: pd.DataFrame,
                                 date_window: int = 2, amount_tol: float = 0.01) -> pd.DataFrame:
    """Match each 5paisa Funds Added/Withdrawn row to an IndusInd bank row."""
    bank = _prep_bank(bank_df)
    transfers = fund_transfers(ledger_df)
    match, _used = _greedy_match(transfers, bank, date_window, amount_tol)
    return match


def _bank_5paisa_rows(bank_df: pd.DataFrame) -> pd.DataFrame:
    bank = _prep_bank(bank_df)
    mask = bank["description"].str.upper().apply(
        lambda s: any(k in s for k in FIVEPAISA_KEYWORDS)
    )
    return bank[mask].copy()


def unmatched_bank_fivepaisa(bank_df: pd.DataFrame, ledger_df: pd.DataFrame,
                             date_window: int = 2, amount_tol: float = 0.01) -> pd.DataFrame:
    """Bank rows that look like 5paisa transfers but have no unused ledger entry."""
    bank = _prep_bank(bank_df)
    five_rows = _bank_5paisa_rows(bank_df)
    transfers = fund_transfers(ledger_df)
    _match, used = _greedy_match(transfers, bank, date_window, amount_tol)
    leftover = five_rows[~five_rows.index.isin(used)]
    if leftover.empty:
        return pd.DataFrame(columns=[
            "bank_date", "bank_amount", "bank_direction", "bank_description", "status",
        ])
    out = pd.DataFrame({
        "bank_date": leftover["date"],
        "bank_amount": leftover["amount"],
        "bank_direction": leftover["direction"],
        "bank_description": leftover["description"],
        "status": "NO LEDGER MATCH",
    })
    return out.reset_index(drop=True)


def _is_mf_bank_row(description: str) -> bool:
    up = " " + (description or "").upper() + " "
    # BSE StAR MF / BillDesk payments are routed through Indian Clearing
    # Corporation, so "CLEARING" must not disqualify them.
    if any(k in up for k in ("STARMF", "BILLDESK", "MFE BANKING")):
        return True
    if any(k in up for k in MF_EXCLUDE_MARKERS):
        return False
    return any(k in up for k in ("MUTUAL FUND", "MUTUALFUND", "ASSET MANAGEMENT",
                                 "AMC LTD", " AMC ", "MF-"))


def _amc_token(text: str) -> str | None:
    up = (text or "").upper()
    for bank_token, cas_token in AMC_TOKENS:
        if bank_token and bank_token in up:
            return cas_token
    return None


def _is_mf_cas_row(row) -> bool:
    isin = str(row.get("isin") or "").upper()
    if isin:
        return isin.startswith("INF")
    return "MF" in str(row.get("security") or "").upper()


def match_bank_to_cas(bank_df: pd.DataFrame, cas_df: pd.DataFrame,
                      date_window: int = 7) -> pd.DataFrame:
    """Match clearly-MF bank rows to CAS unit credits/debits for the same AMC.

    CAS rows carry units, not rupees, so this is a date/fund-level check.
    """
    columns = [
        "bank_date", "bank_amount", "bank_direction", "bank_description",
        "cas_date", "cas_fund", "cas_units", "day_diff", "match_basis", "status",
    ]
    bank = _prep_bank(bank_df)
    bank = bank[bank["description"].apply(_is_mf_bank_row)].copy()
    if bank.empty or cas_df is None or cas_df.empty:
        return pd.DataFrame(columns=columns)

    cas = cas_df.copy()
    cas["date"] = pd.to_datetime(cas["date"], errors="coerce").dt.date
    cas = cas.dropna(subset=["date"])
    cas = cas[cas.apply(_is_mf_cas_row, axis=1)].copy()
    if cas.empty:
        return pd.DataFrame(columns=columns)

    used: set[int] = set()
    rows = []
    for _, b in bank.iterrows():
        token = _amc_token(b["description"])
        expected_type = "credit" if b["direction"] == "debit" else "debit"
        if token:
            cand = cas[
                (cas["type"] == expected_type)
                & (cas["security"].astype(str).str.upper().str.contains(token, regex=False))
                & (~cas.index.isin(used))
            ].copy()
            basis = f"AMC {token} + date"
        else:
            # No AMC token in narration — don't do blind date matching because
            # it produces false positives (e.g. STARMF debits matching to any
            # fund of that AMC months later).  Only AMC-identified rows are
            # matched; others are flagged as "NO AMC IN DESCRIPTION".
            cand = cas.iloc[0:0].copy()
            basis = "no AMC in narration"
        if cand.empty:
            rows.append({
                "bank_date": b["date"], "bank_amount": b["amount"],
                "bank_direction": b["direction"], "bank_description": b["description"],
                "cas_date": None, "cas_fund": None, "cas_units": None,
                "day_diff": None, "match_basis": basis,
                "status": "NO CAS MATCH",
            })
            continue
        cand["day_diff"] = cand["date"].apply(lambda d: abs((d - b["date"]).days))
        best_idx = cand.sort_values(["day_diff", "date"]).index[0]
        best = cand.loc[best_idx]
        used.add(best_idx)
        units = best["credit"] if expected_type == "credit" else best["debit"]
        status = "MATCHED" if best["day_diff"] <= date_window else "DATE MISMATCH"
        rows.append({
            "bank_date": b["date"], "bank_amount": b["amount"],
            "bank_direction": b["direction"], "bank_description": b["description"],
            "cas_date": best["date"], "cas_fund": best["security"],
            "cas_units": units, "day_diff": int(best["day_diff"]),
            "match_basis": basis, "status": status,
        })
    return pd.DataFrame(rows, columns=columns)


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
