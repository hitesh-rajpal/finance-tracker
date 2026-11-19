"""Parses 5paisa contract notes (equity/F&O and commodity).

The layout is fixed and known, so — like the Dhan parser — this reads the
flowing text rather than asking for a manual column mapping. Two document
shapes are handled:

* the standard contract note (Equity Segment + Derivative Segment +
  obligation table + an Annexure of actual orders/trades),
* the commodity contract note (a single trade table with its own columns).

Both expose a signed net settlement figure; the derivative table also
exposes each carried-forward position's opening price and that day's closing
rate, which is what makes a BF/CF continuity check possible.
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import pandas as pd
import pdfplumber

from parsers.statement_parser import PasswordRequired

TRADE_DATE_RE = re.compile(r"TRADE DATE\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})")
# Require at least one digit: on blank BSE notes the next token is the column
# header "BSE", which is not a contract number.
# Match contract note headers - capture the contract number and the segment labels
CONTRACT_NO_RE = re.compile(r"CONTRACT NOTE NO\.\s+([A-Z]*\d[A-Z0-9]*)")

# Pattern to split multiple contract notes in one PDF
# Each note starts with "CONTRACT NOTE NO." followed by contract number and segment labels
CONTRACT_NOTE_SPLIT_RE = re.compile(
    r"(CONTRACT NOTE NO\.\s+[A-Z]*\d[A-Z0-9]*\s+(?:BSE|NSE|MCX|NCX|Capital|FO|Currency|Commodity|Segment).*)"
)
NET_RE = re.compile(r"Net Amount (Receivable|Payable) by Client\s+(.*)")
OBLIGATION_RE = re.compile(r"Payin Payout Obligation\s+#?\s*(-?[\d,]+\.\d{2})")
AMT_PAIR_RE = re.compile(r"([-\d,]+\.\d{2})\s*(Cr|Dr)")
POSITION_RE = re.compile(
    r"^(FUTSTK|FUTIDX|OPTSTK|OPTIDX)\s+(.+?)\s+(B|S)\s+([\d,]+)\s+"
    r"(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)"
    r"(?:\s+(B/F))?\s*$"
)
# Actual trade in the standard note's Annexure:
#   order_no order_time trade_no trade_time <description> EXCHANGE-SIDE qty price value [B/F]
ANNEXURE_TRADE_RE = re.compile(
    r"^(\d{10,})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4,})\s+(\d{2}:\d{2}:\d{2})\s+(.+?)\s+"
    r"((?:NSE|BSE|MCX|NCX)-(?:BUY|SELL))\s+(-?[\d,]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)"
    r"(?:\s+(B/F))?\s*$"
)
# Commodity note trade row (description is on the preceding line):
#   order_no order_time trade_no trade_time BUY qty price brokerage net_rate ... net_total
COMMODITY_TRADE_RE = re.compile(
    r"^(\d{10,})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4,})\s+(\d{2}:\d{2}:\d{2})\s+(BUY|SELL)\s+"
    r"(-?[\d,]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s*$"
)
COMMODITY_DESC_RE = re.compile(r"^(OPTFUT|FUTCOM|OPTCOM|FUTIDX|OPTFUT)\s")


def _num(text: str) -> float:
    return float(str(text).replace(",", ""))


def _parse_date(text: str) -> str | None:
    try:
        return datetime.strptime(" ".join(text.split()), "%b %d %Y").date().isoformat()
    except ValueError:
        return None


def _parse_net(lines: list[str]) -> tuple[float | None, str | None, list[float]]:
    net_line = None
    for line in lines:
        if line.startswith("Net Amount Receivable by Client") or line.startswith("Net Amount Payable by Client"):
            net_line = line
    if not net_line:
        return None, None, []
    label = "Receivable" if "Receivable" in net_line else "Payable"
    pairs = AMT_PAIR_RE.findall(net_line)
    if not pairs:
        return None, label, []
    amounts = [(_num(amt), side) for amt, side in pairs]
    total, _side = amounts[-1]
    signed = total if label == "Receivable" else -total
    return signed, label, [v if s == "Cr" else -v for v, s in amounts]


def _parse_positions(lines: list[str]) -> list[dict]:
    positions = []
    for raw in lines:
        line = raw.strip()
        m = POSITION_RE.match(line)
        if not m:
            continue
        kind, desc, side, qty, foreign_wap, wap, other_wap, wap_common, close_rate, net_total, bf = m.groups()
        positions.append({
            "kind": kind,
            "description": f"{kind} {desc}".strip(),
            "side": side,
            "qty": abs(int(qty.replace(",", ""))),
            "foreign_wap": _num(foreign_wap),
            "wap": _num(wap),
            "other_wap": _num(other_wap),
            "wap_common": _num(wap_common),
            "close_rate": _num(close_rate),
            "net_total": _num(net_total),
            "brought_forward": bool(bf),
        })
    return positions


def _parse_trades(lines: list[str], is_commodity: bool) -> list[dict]:
    trades = []
    pending_desc = None
    for raw in lines:
        line = raw.strip()
        if is_commodity:
            m = COMMODITY_TRADE_RE.match(line)
            if m:
                order_no, order_time, trade_no, trade_time, side, qty, price, _brokerage, _net_rate, _x, net_total = m.groups()
                trades.append({
                    "order_no": order_no,
                    "trade_no": trade_no,
                    "time": trade_time,
                    "description": pending_desc or "",
                    "side": side,
                    "quantity": abs(_num(qty)),
                    "price": _num(price),
                    "value": _num(net_total),
                    "raw_text": line,
                })
                continue
            if COMMODITY_DESC_RE.match(line):
                pending_desc = line
            continue

        m = ANNEXURE_TRADE_RE.match(line)
        if m:
            order_no, order_time, trade_no, trade_time, desc, exch_side, qty, price, value, _bf = m.groups()
            side = "BUY" if exch_side.endswith("BUY") else "SELL"
            trades.append({
                "order_no": order_no,
                "trade_no": trade_no,
                "time": trade_time,
                "description": desc.strip(),
                "side": side,
                "quantity": abs(_num(qty)),
                "price": _num(price),
                "value": _num(value),
                "raw_text": line,
            })
    return trades


def parse_ledger(xls_bytes: bytes) -> pd.DataFrame:
    """Returns the 5paisa ledger as a DataFrame with columns:
    Date (date), Segment, Particular, Description, Debit, Credit,
    Balance and Net (Credit - Debit, i.e. signed cash movement).

    The ledger is the expected-date source: every traded day has one
    'Bill - FNO' / 'Bill - Cash' / 'Bill - Commodities' row per segment.
    """
    raw = pd.read_excel(io.BytesIO(xls_bytes), header=None)
    header_row = None
    for i in range(len(raw)):
        row_text = " ".join(str(v) for v in raw.iloc[i].tolist()).lower()
        if "transaction date" in row_text:
            header_row = i
            break
    if header_row is None:
        raise ValueError("5paisa ledger: could not find the 'Transaction Date' header row")

    df = pd.read_excel(io.BytesIO(xls_bytes), header=header_row)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(subset=["Transaction Date"]).copy()
    for col in ("Debit", "Credit"):
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        ).fillna(0.0)
    df["Date"] = pd.to_datetime(df["Transaction Date"], errors="coerce", dayfirst=True)
    df = df.dropna(subset=["Date"])
    df["Date"] = df["Date"].dt.date
    df["Net"] = df["Credit"] - df["Debit"]
    return df.reset_index(drop=True)


BILL_PARTICULARS = ("Bill - FNO", "Bill - Cash", "Bill - Commodities")


def _parse_single_note(text: str) -> dict | None:
    """Parse a single contract note from text."""
    lines = text.split("\n")
    
    m = TRADE_DATE_RE.search(text)
    trade_date = _parse_date(m.group(0).split("TRADE DATE", 1)[1]) if m else None
    m = CONTRACT_NO_RE.search(text)
    contract_no = m.group(1) if m else None
    net_amount, net_label, segment_amounts = _parse_net(lines)
    obligation_match = OBLIGATION_RE.search(text)
    obligation = _num(obligation_match.group(1)) if obligation_match else None
    is_commodity = "COMMODITY CONTRACT NOTES" in text

    return {
        "trade_date": trade_date,
        "contract_no": contract_no,
        "net_amount": net_amount,
        "net_label": net_label,
        "obligation": obligation,
        "segment_amounts": segment_amounts,
        "is_commodity": is_commodity,
        "positions": _parse_positions(lines),
        "trades": _parse_trades(lines, is_commodity),
        "full_text": text,
    }


def parse_contract_note(pdf_bytes: bytes, password: str | None = None) -> dict:
    """Returns a single note dict (for backwards compatibility).
    
    If the PDF contains multiple contract notes (e.g., Cash + F&O combined),
    this returns the first one. Use `parse_contract_notes` for all notes.
    """
    notes = parse_contract_notes(pdf_bytes, password)
    return notes[0] if notes else {
        "trade_date": None, "contract_no": None, "net_amount": None,
        "net_label": None, "obligation": None, "segment_amounts": [],
        "is_commodity": False, "positions": [], "trades": [], "full_text": ""
    }


def parse_contract_notes(pdf_bytes: bytes, password: str | None = None) -> list[dict]:
    """Parse ALL contract notes in a PDF (handles multi-note PDFs like Cash+F&O).
    
    Returns list of note dicts, one per contract note found.
    """
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as exc:
        raise PasswordRequired(str(exc)) from exc

    with pdf_ctx as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    full_text = "\n".join(pages)

    # Split by contract note headers
    # Each note starts with "CONTRACT NOTE NO." 
    splits = list(CONTRACT_NOTE_SPLIT_RE.finditer(full_text))
    
    notes = []
    if not splits:
        # Fallback: try parsing as single note
        note = _parse_single_note(full_text)
        if note["trade_date"]:
            notes.append(note)
    else:
        for i, match in enumerate(splits):
            start = match.start()
            end = splits[i + 1].start() if i + 1 < len(splits) else len(full_text)
            note_text = full_text[start:end]
            note = _parse_single_note(note_text)
            if note["trade_date"]:
                notes.append(note)
    
    return notes
