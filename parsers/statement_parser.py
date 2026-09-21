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


_DATE_CELL_RE = re.compile(
    r"^\d{1,2}[\s/-][A-Za-z]{3,9}[\s/-]?\d{2,4}$"   # 24 Jul 26 / 24-Jul-2026 / 24/Jul/26
    r"|^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"              # 24/07/2026 / 24-07-26
    r"|^\d{4}-\d{2}-\d{2}$"                          # 2026-07-24
)


def _cell_looks_like_date(text) -> bool:
    """Deliberately a strict pattern match, not dateutil.parser — dateutil
    happily treats a bare number like '499' or '9,999' as a year (defaulting
    the rest to today), which made this check pass for a fee-schedule table
    full of plain rupee amounts. Requires actual day+month+year structure."""
    text = (text or "").strip()
    if not text or len(text) > 20:
        return False
    return bool(_DATE_CELL_RE.match(text))


def _table_has_date_column(table) -> bool:
    """Whether any cell in this table looks like a date — used to skip
    boilerplate tables (fee schedules, T&C tables) that extract_tables()
    picks up alongside the real transaction table but that would otherwise
    corrupt it once everything gets concatenated together."""
    return any(_cell_looks_like_date(cell) for row in table for cell in row)


# Some statements (seen on a real SBI card statement) lay out transactions
# as plain text lines with no visible table grid at all, so extract_tables()
# finds nothing there — e.g. "24 Jul 26 Avenue Supermarts Ltd IN 308.00 D".
# This mirrors sms_parser's line-based approach for the same reason: no
# gridlines for pdfplumber's table detector to key off of.
_TEXT_LINE_TXN_RE = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})\s+(.+?)\s+([\d,]+\.\d{2})\s*([DC])$")


def _extract_text_line_rows(pdf) -> list[list[str]]:
    rows = []
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            m = _TEXT_LINE_TXN_RE.match(line.strip())
            if not m:
                continue
            txn_date, desc, amount, marker = m.groups()
            marker_word = "Cr" if marker == "C" else "Dr"
            rows.append([txn_date, desc.strip(), f"{amount} {marker_word}"])
    return rows


# Some ICICI statements (seen on a real Amazon Pay Card statement) bury the
# transaction list in flowing page text too, but in a different shape: no
# grid, wrapped around a sidebar rewards chart, each real line reading
# roughly "DD/MM/YYYY SerNo Description... RewardPoints Amount [CR]" — e.g.
# "28/07/2026 13872649838 AMAZON PAY INDIA PVT LT BANGALORE IN 115 5,760.00".
# Debits carry no marker at all (unlike SBI's trailing D/C); only credits
# get a trailing "CR". Matched with .search rather than .match since chart
# labels ("49%", "Apparel/Grocery-51% Others-49%") can land on the same
# line, before the date.
_ICICI_TEXT_LINE_TXN_RE = re.compile(
    r"(\d{2}/\d{2}/\d{4})\s+(\d{6,})\s+(.+?)\s+-?\d+\s+([\d,]+\.\d{2})(\s*CR)?\s*$"
)


def _extract_icici_style_rows(pdf) -> list[list[str]]:
    rows = []
    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.split("\n"):
            m = _ICICI_TEXT_LINE_TXN_RE.search(line.strip())
            if not m:
                continue
            txn_date, ser_no, desc, amount, cr = m.groups()
            marker = " Cr" if cr else ""
            # Keep the serial number in the description — it flows into
            # raw_text and lets dedup match this row against an SMS alert
            # referencing the same number (see reports._extract_refs).
            rows.append([txn_date, f"{ser_no} {desc.strip()}", f"{amount}{marker}"])
    return rows


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
                if not _table_has_date_column(table):
                    continue  # boilerplate (fee schedule, T&C, etc.), not the transaction table
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

        # Always also try the plain-text (no-gridline) extractors, even when
        # the table pass found *some* rows — a small boilerplate/summary
        # table can pass the date-column filter and quietly stand in for
        # the real, much longer transaction list (confirmed on a real ICICI
        # statement: an 8-row refund-only table beat out the real 32-row
        # list because extract_tables() never saw the real list at all).
        # Whichever method finds the most rows wins: a correct, complete
        # extraction is essentially always the largest one, while a
        # false-positive boilerplate table stays small.
        text_rows_sbi = _extract_text_line_rows(pdf)
        text_rows_icici = _extract_icici_style_rows(pdf)

    best_text_rows = max([text_rows_sbi, text_rows_icici], key=len, default=[])
    if len(best_text_rows) > len(all_rows):
        return pd.DataFrame(best_text_rows, columns=["Date", "Description", "Amount"])

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
