"""Parses Dhan (Raise Securities) contract notes and ledger statements.

Unlike statement_parser/contract_note_parser, Dhan's layout is fixed and
known (not broker-agnostic), so this module parses it directly from the
PDF's flowing text rather than exposing a manual column-mapping step —
the Equity and Derivative segment tables don't have real PDF gridlines,
and the Derivative segment wraps a contract's description across 1-3
lines in a way that shifts depending on how long the expiry/exchange
suffix is, so table extraction doesn't work here at all.

Built from reconciling a real October 2026 statement set by hand — see
that session for the worked example this logic was derived from.
"""
import io
import re
import pdfplumber
import pandas as pd
from dateutil import parser as dateparser

from parsers.statement_parser import PasswordRequired  # reuse

TRADE_DATE_RE = re.compile(r"Trade Date:\s*(\d{2})-(\d{2})-(\d{4})")
CONTRACT_NO_RE = re.compile(r"Contract Note No:\s*(\d+)")
NET_RECEIVABLE_RE = re.compile(
    r"Net Amount Receivable.*?NCLCM.*?$", re.IGNORECASE
)  # unused placeholder, real parsing is line-based below
LEDGER_BALANCE_RE = re.compile(r"Ledger Balance\s+([\d,]+\.\d{2})\s*(CR|DR)", re.IGNORECASE)

# Equity segment row, e.g.:
#   "INE034A01011 ARVIND 1 499.5000 499.5000 499.50 1 -499.50"
# We only need the ISIN, a human-readable name, and the net obligation
# (last number on the line) — the buy/sell qty & WAP split isn't needed
# for reconciliation and the column count varies depending on whether a
# row has both a buy and sell leg that day.
EQUITY_ROW_RE = re.compile(
    r"^([A-Z]{2}[A-Z0-9]{9}\d)\s+(\S+)\s+.*?(-?[\d,]+\.\d{2})\s*$"
)


def _iso_date(dd, mm, yyyy) -> str:
    return f"{yyyy}-{mm}-{dd}"


def _nums(text: str) -> list[str]:
    return re.findall(r"-?[\d,]+(?:\.\d+)?", text)


def _parse_derivative_rows(lines: list[str]) -> list[dict]:
    """Extracts (kind, qty, wap) for every BF/CF/B/S row in the Derivative
    Segment. `wap` is the carried-forward price: for a BF row this is the
    PRIOR trading day's close (what should match that day's CF); for a CF
    row this equals today's close (since carrying forward re-prices the
    position at today's settlement). Comparing BF(day N+1) to CF(day N)
    for the same contract is exactly the check that catches a NSE
    dividend-adjustment (or a genuine broker error) in the carried price.

    Handles both of Dhan's line layouts: the contract description on its
    own line(s) followed by a BF/CF data line, or (when the description
    is short) the description and BF/CF data on one line.
    """
    rows = []
    pending_name = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m1 = re.match(r"^(?:FUTSTK|FUTIDX)\s+(.*?)\s+(BF|CF|B|S)\s+(-?[\d,]+)\s+(.*)$", line)
        if m1:
            desc, kind, qty, rest = m1.groups()
            name = "FUTSTK " + desc
        else:
            m2 = re.match(r"^(BF|CF|B|S)\s+(-?[\d,]+)\s+(.*)$", line)
            if m2 and pending_name:
                kind, qty, rest = m2.groups()
                name = pending_name
            elif re.match(r"^(FUTSTK|FUTIDX|OPTSTK|OPTIDX)\s", line):
                pending_name = line
                continue
            else:
                continue
        nums = _nums(rest)
        if len(nums) < 2:
            continue
        rows.append({
            "name": name,
            "kind": kind,
            "qty": abs(int(qty.replace(",", ""))),
            "wap": float(nums[0].replace(",", "")),
        })
        pending_name = None
    return rows


def parse_contract_note(pdf_bytes: bytes, password: str | None = None) -> dict:
    """Returns a dict:
        trade_date (ISO str), contract_no (str),
        equity_rows: [{isin, name, net_obligation}],
        derivative_rows: [{name, kind, qty, wap}]   (kind: BF/CF/B/S),
        segment_totals: {NCLCM, NCLFO, Total}  (signed: CR positive, DR negative),
        ledger_balance: float or None (signed),
    Raises PasswordRequired if the password is missing/wrong.
    """
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_bytes), password=password)
    except Exception as e:
        raise PasswordRequired(str(e)) from e

    with pdf_ctx as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]
    full_text = "\n".join(pages_text)
    lines = full_text.split("\n")

    m = TRADE_DATE_RE.search(full_text)
    trade_date = _iso_date(m.group(1), m.group(2), m.group(3)) if m else None
    m = CONTRACT_NO_RE.search(full_text)
    contract_no = m.group(1) if m else None

    equity_rows = []
    for line in lines:
        m = EQUITY_ROW_RE.match(line.strip())
        if m:
            isin, name, net_obl = m.groups()
            equity_rows.append({
                "isin": isin,
                "name": name,
                "net_obligation": float(net_obl.replace(",", "")),
            })

    derivative_rows = _parse_derivative_rows(lines)

    segment_totals = {}
    net_line = None
    for line in lines:
        if "Net Amount Receivable" in line:
            net_line = line  # last occurrence wins (the Total/summary one)
    if net_line:
        pairs = re.findall(r"([\d,]+\.\d{2})\s*(DR|CR)", net_line)
        keys = ["NCLCM", "NCLFO", "Total"]
        # some days only have one segment (e.g. equity-only or F&O-only) —
        # map from the right so "Total" (last) is always correct
        for key, (amt, side) in zip(keys[-len(pairs):], pairs):
            val = float(amt.replace(",", ""))
            segment_totals[key] = -val if side == "DR" else val

    ledger_balance = None
    m = LEDGER_BALANCE_RE.search(full_text)
    if m:
        val = float(m.group(1).replace(",", ""))
        ledger_balance = -val if m.group(2).upper() == "DR" else val

    return {
        "trade_date": trade_date,
        "contract_no": contract_no,
        "equity_rows": equity_rows,
        "derivative_rows": derivative_rows,
        "segment_totals": segment_totals,
        "ledger_balance": ledger_balance,
    }


def _find_header_row(raw: pd.DataFrame) -> int:
    """Dhan's ledger/PnL xls files have a ~5-row letterhead before the real
    header, but the exact row count isn't guaranteed stable across Dhan
    template versions, so detect it instead of hardcoding header=4."""
    for i in range(min(15, len(raw))):
        row_text = " ".join(str(v) for v in raw.iloc[i].tolist()).lower()
        if "date" in row_text and "debit" in row_text and "credit" in row_text:
            return i
    return 4  # fallback to the offset seen in practice


def parse_ledger(xls_bytes: bytes) -> pd.DataFrame:
    """Returns a cleaned ledger DataFrame with columns:
    Date (datetime64), Type (str, stripped), Description, Debit (float),
    Credit (float), RunningBalance (float, signed CR positive)."""
    raw = pd.read_excel(io.BytesIO(xls_bytes), header=None)
    header_row = _find_header_row(raw)
    df = pd.read_excel(io.BytesIO(xls_bytes), header=header_row)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(subset=["Date"]).copy()
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str.strip(), format="%d-%b-%Y", errors="coerce")
    df["Type"] = df["Type"].astype(str).str.strip()
    df["Debit"] = pd.to_numeric(df["Debit"], errors="coerce").fillna(0.0)
    df["Credit"] = pd.to_numeric(df["Credit"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["Date"])
    return df.reset_index(drop=True)
