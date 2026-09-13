"""Parses demat/broker contract notes (PDF) into trade rows.

Contract note layouts are broker-specific (Zerodha, ICICI Direct, Groww,
Upstox all differ), so — same as statement_parser — this exposes a raw
table + column-mapping step rather than guessing silently. It additionally
pulls the "net amount receivable/payable" settlement figure from the free
text of the note, which is what you match against the corresponding bank
debit/credit during reconciliation.
"""
import io
import re
import pdfplumber
import pandas as pd
from dateutil import parser as dateparser

from parsers.statement_parser import _parse_amount, _score_header_row  # reuse

TRADE_HEADER_HINTS = {
    "scrip": ["security", "symbol", "scrip", "company"],
    "side": ["b/s", "buy/sell", "trade type"],
    "quantity": ["qty", "quantity"],
    "price": ["rate", "price", "avg price", "trade price"],
    "brokerage": ["brokerage"],
    "net_amount": ["net total", "net amount", "net value"],
}

NET_SETTLEMENT_RE = re.compile(
    r"net amount (?:receivable|payable)[^₹Rs\d]*(?:Rs\.?|₹)?\s*([\d,]+\.\d{2})",
    re.IGNORECASE,
)
NOTE_DATE_RE = re.compile(r"(?:trade date|contract date)\s*[:\-]?\s*([0-9]{1,2}[-/][A-Za-z0-9]{2,4}[-/]?[0-9]{0,4})", re.IGNORECASE)


def extract_trade_table(pdf_bytes: bytes) -> tuple[pd.DataFrame, dict]:
    all_rows = []
    header = None
    full_text = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            full_text.append(page.extract_text() or "")
            for table in page.extract_tables():
                if not table or len(table) < 2:
                    continue
                candidate_idx = max(range(min(3, len(table))), key=lambda i: _score_header_row(table[i]))
                if _score_header_row(table[candidate_idx]) >= 1:
                    if header is None:
                        header = [(_c or "").strip() for _c in table[candidate_idx]]
                    body = table[candidate_idx + 1:]
                else:
                    body = table
                for row in body:
                    if row and any((c or "").strip() for c in row):
                        all_rows.append(row)

    text_blob = "\n".join(full_text)
    meta = {}
    m = NET_SETTLEMENT_RE.search(text_blob)
    if m:
        meta["net_settlement"] = float(m.group(1).replace(",", ""))
    d = NOTE_DATE_RE.search(text_blob)
    if d:
        try:
            meta["trade_date"] = dateparser.parse(d.group(1), dayfirst=True, fuzzy=True).date().isoformat()
        except (ValueError, OverflowError):
            pass
    meta["full_text"] = text_blob

    if not all_rows:
        return pd.DataFrame(), meta
    width = max(len(r) for r in all_rows)
    padded = [list(r) + [None] * (width - len(r)) for r in all_rows]
    if header and len(header) == width:
        df = pd.DataFrame(padded, columns=header)
    else:
        df = pd.DataFrame(padded, columns=[f"col_{i}" for i in range(width)])
    return df, meta


def guess_trade_mapping(df: pd.DataFrame) -> dict:
    mapping = {}
    for field, hints in TRADE_HEADER_HINTS.items():
        for col in df.columns:
            if any(h in str(col).lower() for h in hints):
                mapping[field] = col
                break
    return mapping


def normalize_trades(df: pd.DataFrame, mapping: dict, meta: dict, broker: str, source_file: str) -> list[dict]:
    trade_date = meta.get("trade_date")
    rows = []
    for _, r in df.iterrows():
        scrip = str(r.get(mapping.get("scrip"), "")).strip()
        if not scrip or scrip.lower() in ("nan", "none", ""):
            continue
        qty = _parse_amount(r.get(mapping.get("quantity")))
        price = _parse_amount(r.get(mapping.get("price")))
        net_amount = _parse_amount(r.get(mapping.get("net_amount")))
        side_raw = str(r.get(mapping.get("side"), "")).strip().upper()
        direction = "debit" if side_raw.startswith("B") else ("credit" if side_raw.startswith("S") else None)
        if direction is None or qty is None:
            continue
        rows.append({
            "date": trade_date,
            "amount": net_amount if net_amount is not None else (qty * (price or 0)),
            "direction": direction,
            "account": broker,
            "bank": broker,
            "description": f"{side_raw} {scrip} x{qty}",
            "category": "Trading/Investment",
            "raw_text": " | ".join(str(v) for v in r.values if v),
            "source": "contract_note",
            "source_file": source_file,
            "scrip": scrip,
            "quantity": qty,
            "price": price,
        })
    return rows
