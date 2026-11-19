"""Parses CDSL/NSDL Consolidated Account Statement (CAS) PDFs.

The CAS is a bilingual (English + Hindi) PDF; the text layer interleaves
the two languages, so this parser works from the extracted *tables* rather
than line text.  Only the transaction sections are relevant here — the
holdings/valuation sections are ignored.

Returned rows:
    date (ISO str), isin, security, description,
    credit (units), debit (units), type ('credit'|'debit')
"""
from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd
import pdfplumber

from parsers.statement_parser import PasswordRequired

_DATE_RE = re.compile(r"^\d{1,2}-\d{1,2}-\d{4}$")


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _num(text) -> float | None:
    s = re.sub(r"[^\d.\-]", "", str(text or ""))
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find_date_col(table: list[list]) -> int | None:
    """Column with the most parseable dd-mm-yyyy cells."""
    if not table:
        return None
    width = max(len(r) for r in table)
    counts = [0] * width
    for row in table:
        for i, cell in enumerate(row):
            if _DATE_RE.match(_clean(cell)):
                counts[i] += 1
    if not counts or max(counts) == 0:
        return None
    return counts.index(max(counts))


def parse_cas_transactions(pdf_bytes: bytes, password: str | None = None) -> pd.DataFrame:
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as exc:
        raise PasswordRequired(str(exc)) from exc

    rows: list[dict] = []
    with pdf_ctx as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or len(table) < 2:
                    continue
                date_col = _find_date_col(table)
                if date_col is None:
                    continue

                # In the CAS transaction layout the columns after the date are:
                # Opening Balance, Credit, Debit, Closing Balance, Stamp Duty.
                credit_col = date_col + 2
                debit_col = date_col + 3

                current_isin = ""
                current_security = ""
                for row in table:
                    row = list(row) + [None] * (max(len(r) for r in table) - len(row))
                    raw_date = _clean(row[date_col]) if date_col < len(row) else ""
                    if not _DATE_RE.match(raw_date):
                        continue
                    # carry forward security details when the cell is blank
                    if date_col >= 1 and _clean(row[0]):
                        current_isin = _clean(row[0])
                    if date_col >= 2 and _clean(row[1]):
                        current_security = _clean(row[1])
                    description = _clean(row[2]) if date_col >= 3 else ""
                    try:
                        d = pd.to_datetime(raw_date, format="%d-%m-%Y").date().isoformat()
                    except ValueError:
                        continue
                    credit = _num(row[credit_col]) if credit_col < len(row) else None
                    debit = _num(row[debit_col]) if debit_col < len(row) else None
                    if (credit or 0) <= 0 and (debit or 0) <= 0:
                        continue
                    txn_type = "credit" if (credit or 0) > 0 else "debit"
                    rows.append({
                        "date": d,
                        "isin": current_isin,
                        "security": current_security,
                        "description": description,
                        "credit": credit or 0.0,
                        "debit": debit or 0.0,
                        "type": txn_type,
                    })

    if not rows:
        return pd.DataFrame(columns=["date", "isin", "security", "description", "credit", "debit", "type"])
    df = pd.DataFrame(rows).drop_duplicates().reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df
