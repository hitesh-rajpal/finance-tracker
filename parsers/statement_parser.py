"""Parses bank-statement PDFs into normalized transaction rows.

Bank statement PDF layouts differ a lot (columns, header wording, whether
debit/credit are one signed column or two separate ones), so this module
does two things:

  1. `extract_raw_table(pdf_bytes)` — pulls out the best-guess transaction
     table as a DataFrame, for the UI to show the user and to let them
     confirm/remap columns before committing.
  2. `normalize_table(df, mapping)` — turns that DataFrame + a column
     mapping (date/description/debit/credit or date/description/amount)
     into transaction dicts ready for storage.

This is intentionally a two-step, human-in-the-loop flow rather than a
fully automatic one: a wrong silent guess on which column is debit vs
credit would corrupt every downstream report, so we surface the mapping
once per statement layout instead.
"""
import io
import re
import pdfplumber
import pandas as pd
from dateutil import parser as dateparser

HEADER_HINTS = {
    "date": ["date", "txn date", "value date", "transaction date"],
    "description": ["narration", "description", "particulars", "details", "remarks"],
    "debit": ["debit", "withdrawal", "dr"],
    "credit": ["credit", "deposit", "cr"],
    "amount": ["amount"],
    "balance": ["balance", "closing balance"],
    "category": ["category"],
}

# Credit-card statements often use one unsigned amount column plus a trailing
# "Cr"/"Dr" marker (e.g. "3,643.00 Cr" for a payment, plain "950.00" for a
# purchase) instead of a sign or separate columns — detect that marker so
# direction isn't silently guessed wrong for every row.
_CRDR_RE = re.compile(r"\b(cr|dr)\b", re.IGNORECASE)


def _crdr_hint(val) -> str | None:
    if val is None:
        return None
    m = _CRDR_RE.search(str(val))
    if not m:
        return None
    return "credit" if m.group(1).lower() == "cr" else "debit"


def _hint_matches(hint: str, text: str) -> bool:
    """Word-boundary match — plain substring containment would let a short
    hint like 'cr' match inside an unrelated word like 'description' and
    silently mis-map a column (this happened for real against a Kotak
    statement: 'Credit' hint's 'cr' matched 'Description', routing every
    row's parsed amount through the wrong branch)."""
    return re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", text) is not None


def _score_header_row(cells: list[str]) -> int:
    text = " ".join((c or "").lower() for c in cells)
    score = 0
    for hints in HEADER_HINTS.values():
        if any(_hint_matches(h, text) for h in hints):
            score += 1
    return score


class PasswordRequired(Exception):
    """Raised when a PDF can't be opened — either it's password-protected,
    or genuinely corrupt/unsupported; the caller can't tell which without
    trying a password, so the UI offers one either way."""


def extract_raw_table(pdf_bytes: bytes, password: str | None = None) -> pd.DataFrame:
    """Best-effort extraction of the transaction table across all pages."""
    all_rows = []
    header = None
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as e:
        raise PasswordRequired(str(e)) from e
    with pdf_ctx as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                # find the most header-like row near the top of this table
                candidate_idx = max(range(min(3, len(table))), key=lambda i: _score_header_row(table[i]))
                if _score_header_row(table[candidate_idx]) >= 2:
                    if header is None:
                        header = [(_c or "").strip() for _c in table[candidate_idx]]
                    body = table[candidate_idx + 1:]
                else:
                    body = table
                for row in body:
                    if row and any((c or "").strip() for c in row):
                        all_rows.append(row)

    if not all_rows:
        return pd.DataFrame()

    width = max(len(r) for r in all_rows)
    padded = [list(r) + [None] * (width - len(r)) for r in all_rows]
    if header and len(header) == width:
        df = pd.DataFrame(padded, columns=header)
    else:
        df = pd.DataFrame(padded, columns=[f"col_{i}" for i in range(width)])
    return df


def guess_column_mapping(df: pd.DataFrame) -> dict:
    mapping = {}
    for field, hints in HEADER_HINTS.items():
        for col in df.columns:
            if any(_hint_matches(h, str(col).lower()) for h in hints):
                mapping[field] = col
                break
    return mapping


def _parse_amount(val) -> float | None:
    if val is None:
        return None
    s = re.sub(r"[^\d.\-]", "", str(val))
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(val):
    if val is None or str(val).strip() == "":
        return None
    try:
        return dateparser.parse(str(val), dayfirst=True, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


def normalize_table(df: pd.DataFrame, mapping: dict, bank: str, account: str, source_file: str,
                     default_direction: str = "debit") -> list[dict]:
    """mapping keys: date, description, optional category, and either
    (debit, credit) or amount. For a single amount column: a "Cr"/"Dr" suffix
    in the cell wins if present; else a negative sign means debit; else
    default_direction is used ("debit" fits most credit-card statements,
    which list unsigned purchase amounts; "credit" fits an account statement
    where unsigned amounts are deposits)."""
    rows = []
    for _, r in df.iterrows():
        date = _parse_date(r.get(mapping.get("date")))
        description = str(r.get(mapping.get("description"), "")).strip()
        if not date or not description:
            continue

        debit = _parse_amount(r.get(mapping.get("debit"))) if mapping.get("debit") else None
        credit = _parse_amount(r.get(mapping.get("credit"))) if mapping.get("credit") else None

        if debit is None and credit is None and mapping.get("amount"):
            raw_val = r.get(mapping.get("amount"))
            amt = _parse_amount(raw_val)
            if amt is None:
                continue
            hint = _crdr_hint(raw_val)
            if hint:
                direction = hint
            elif amt < 0:
                direction = "debit"
            else:
                direction = default_direction
            amount = abs(amt)
        elif debit:
            direction, amount = "debit", debit
        elif credit:
            direction, amount = "credit", credit
        else:
            continue

        row = {
            "date": date,
            "amount": amount,
            "direction": direction,
            "account": account,
            "bank": bank,
            "description": description,
            "raw_text": " | ".join(str(v) for v in r.values if v),
            "source": "statement",
            "source_file": source_file,
        }
        if mapping.get("category"):
            cat_val = str(r.get(mapping["category"], "")).strip()
            if cat_val and cat_val.lower() != "nan":
                row["category"] = cat_val
        rows.append(row)
    return rows
