"""IndusInd Bank statement parser.

The generic `statement_parser.extract_raw_table` is header-heuristic based and
gets confused by IndusInd statements that carry a term-deposit table, a reward
points table, or a merged "Product Description" cell before the real
transaction table.  IndusInd's transaction tables have a very stable shape,
so this parser finds the transaction header row anywhere in any table and maps
columns by header name.

Returned DataFrame columns match `statement_parser.normalize_table` output:
    date (datetime.date), description, debit, credit, balance, amount, direction
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

import pandas as pd
import pdfplumber

from parsers.statement_parser import PasswordRequired

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_DATE_MON_RE = re.compile(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$")
_DATE_NUM_RE = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{4})$")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _norm_header(value) -> str:
    return re.sub(r"[^a-z]", "", _clean(value).lower())


def _parse_date(value) -> date | None:
    text = _clean(value)
    m = _DATE_MON_RE.match(text)
    if m:
        day, mon, year = m.groups()
        month = _MONTHS.get(mon.lower())
        if month:
            try:
                return date(int(year), month, int(day))
            except ValueError:
                return None
    m = _DATE_NUM_RE.match(text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None
    return None


def _parse_amount(value) -> float | None:
    text = _clean(value)
    if not text or text in ("-", "--"):
        return None
    text = text.replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in (".", "-"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _header_indices(row: list) -> dict | None:
    """Map column roles from a header row, if it is a transaction header."""
    names = [_norm_header(c) for c in row]
    roles = {}
    for i, name in enumerate(names):
        if name == "date" and "date" not in roles:
            roles["date"] = i
        elif name in ("particulars", "particular", "description", "narration") and "description" not in roles:
            roles["description"] = i
        elif name in ("withdrawal", "withdrawl", "debit", "withdrawals") and "debit" not in roles:
            roles["debit"] = i
        elif name in ("deposit", "credit", "deposits") and "credit" not in roles:
            roles["credit"] = i
        elif name == "balance" and "balance" not in roles:
            roles["balance"] = i
    if "date" in roles and "description" in roles and (
        "debit" in roles or "credit" in roles
    ):
        return roles
    return None


def _row_date(row: list, roles: dict) -> date | None:
    idx = roles["date"]
    if idx >= len(row):
        return None
    return _parse_date(row[idx])


def _is_noise(description: str) -> bool:
    up = description.upper()
    return any(
        marker in up
        for marker in (
            "BROUGHT FORWARD",
            "CARRIED FORWARD",
            "OPENING BALANCE",
            "CLOSING BALANCE",
            "TOTAL",
        )
    )


def parse_indusind_statement(pdf_bytes: bytes, password: str | None = None) -> pd.DataFrame:
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as exc:
        raise PasswordRequired(str(exc)) from exc

    rows: list[dict] = []
    with pdf_ctx as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                header_at = None
                roles = None
                for i, row in enumerate(table):
                    found = _header_indices(row)
                    if found:
                        header_at, roles = i, found
                        break
                if roles is None:
                    continue
                for row in table[header_at + 1:]:
                    d = _row_date(row, roles)
                    if d is None:
                        continue
                    desc_idx = roles["description"]
                    description = _clean(row[desc_idx]) if desc_idx < len(row) else ""
                    if _is_noise(description):
                        continue
                    debit = _parse_amount(row[roles["debit"]]) if roles.get("debit", 99) < len(row) else None
                    credit = _parse_amount(row[roles["credit"]]) if roles.get("credit", 99) < len(row) else None
                    balance = _parse_amount(row[roles["balance"]]) if roles.get("balance", 99) < len(row) else None
                    if not debit and not credit:
                        continue
                    rows.append({
                        "date": d,
                        "description": description,
                        "debit": debit or 0.0,
                        "credit": credit or 0.0,
                        "balance": balance,
                    })

    if not rows:
        return pd.DataFrame(columns=["date", "description", "debit", "credit", "balance",
                                     "amount", "direction"])
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["date", "description", "debit", "credit", "balance"])
    df = df.sort_values("date").reset_index(drop=True)
    df["amount"] = df.apply(lambda r: r["debit"] if r["debit"] else r["credit"], axis=1)
    df["direction"] = df.apply(lambda r: "debit" if r["debit"] else "credit", axis=1)
    return df


def extract_raw_like(pdf_bytes: bytes, password: str | None = None) -> pd.DataFrame:
    """IndusInd rows in the column shape the Upload tab's mapping UI expects.

    This lets the existing date/description/debit/credit mapping flow work
    unchanged for statements whose table the generic extractor misreads.
    """
    df = parse_indusind_statement(pdf_bytes, password=password)
    if df.empty:
        return df
    out = pd.DataFrame({
        "Date": df["date"].apply(lambda d: d.strftime("%d-%b-%Y")),
        "Particulars": df["description"],
        "Withdrawal": df["debit"].replace(0, None),
        "Deposit": df["credit"].replace(0, None),
        "Balance": df["balance"],
    })
    return out.reset_index(drop=True)
