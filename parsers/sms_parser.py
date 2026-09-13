"""Parses bank transaction-alert SMS text into normalized rows.

Handles three input shapes:
  - Forwarded-to-self chat text (WhatsApp, etc.): each bank alert is one
    message block (possibly spanning several lines), immediately followed
    by a manual tag message — "Office" or "Personal <note>". We pair each
    transaction block with the tag right after it and treat that tag as
    authoritative for is_office, rather than guessing from keywords.
  - a plain .txt file with one SMS per line (no tags) — falls out of the
    same block-grouping logic naturally, just with no tag block following.
  - a .csv/.xlsx export from an SMS-backup app, where we auto-detect the
    message-body column (and an optional date column)

Indian bank alert SMS vary a lot by bank, but nearly all of them share the
same bones: an amount, a debit/credit verb, an account/card tail, and a
date. We match several common phrasings and fall back to a generic
amount+direction extraction (flagged low-confidence) when nothing more
specific fits, rather than silently dropping the message.
"""
import re
import io
import pandas as pd
from dateutil import parser as dateparser

AMOUNT_RE = r"(?:INR|Rs\.?|₹)\s?([\d,]+(?:\.\d{1,2})?)"
DATE_RE = r"(\d{1,2}[-/][A-Za-z]{3}[-/]?\d{0,4}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})"

DEBIT_WORDS = ["debited", "spent", "paid", "withdrawn", "purchase of", "debit of"]
CREDIT_WORDS = ["credited", "received", "deposited", "credit of"]

BANK_HINTS = ["HDFC", "ICICI", "SBI", "AXIS", "KOTAK", "IDFC", "YES BANK", "PNB", "BOB", "CANARA",
              "INDUSIND", "TATA", "AMEX", "AMERICAN EXPRESS", "SLICE", "ONECARD", "RBL"]

ACCOUNT_RE = r"(?:a/c|acct|account|card)\s?(?:no\.?|ending)?\s?[Xx\*]*([0-9]{3,6})"

# A tag message you send right after forwarding a bank alert to yourself —
# "Office", "Personal ice cream ram and sagar", etc.
TAG_RE = re.compile(r"^\s*(office|personal)\b[:\s-]*(.*)$", re.IGNORECASE)

# Best-effort strip of a WhatsApp "DATE, TIME - Sender: " line prefix, in case
# a raw chat export (rather than copy-pasted message bodies) is pasted in.
WHATSAPP_PREFIX_RE = re.compile(
    r"^\[?\d{1,2}[/.]\d{1,2}[/.]\d{2,4},?\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s?[AaPp][Mm])?\]?\s*-\s*[^:]{1,40}:\s*"
)


def _guess_bank(text: str) -> str | None:
    upper = text.upper()
    for b in BANK_HINTS:
        if b in upper:
            return b.title()
    return None


def _guess_direction(text: str) -> str | None:
    lower = text.lower()
    if any(w in lower for w in DEBIT_WORDS):
        return "debit"
    if any(w in lower for w in CREDIT_WORDS):
        return "credit"
    return None


def _guess_date(text: str, fallback_date=None):
    m = re.search(DATE_RE, text)
    if m:
        try:
            return dateparser.parse(m.group(1), dayfirst=True, fuzzy=True).date().isoformat()
        except (ValueError, OverflowError):
            pass
    if fallback_date is not None:
        try:
            return dateparser.parse(str(fallback_date), dayfirst=True, fuzzy=True).date().isoformat()
        except (ValueError, OverflowError):
            pass
    return None


def _extract_description(text: str) -> str:
    # Strip common boilerplate so the merchant/payee stands out for categorization.
    cleaned = re.sub(r"(?i)(sms|alert|do not share|call \d+|otp|customer care).*$", "", text)
    return cleaned.strip()[:200]


def parse_sms_line(text: str, fallback_date=None) -> dict | None:
    text = text.strip()
    if not text:
        return None
    amt_match = re.search(AMOUNT_RE, text)
    if not amt_match:
        return None
    amount = float(amt_match.group(1).replace(",", ""))
    direction = _guess_direction(text) or "debit"
    date = _guess_date(text, fallback_date)
    acct_match = re.search(ACCOUNT_RE, text, re.IGNORECASE)
    account = acct_match.group(1) if acct_match else None
    bank = _guess_bank(text)
    confidence = "high" if (amt_match and direction and date) else "low"
    return {
        "date": date,
        "amount": amount,
        "direction": direction,
        "account": f"{bank} XX{account}" if bank and account else (f"XX{account}" if account else bank),
        "bank": bank,
        "description": _extract_description(text),
        "raw_text": text,
        "confidence": confidence,
        "source": "sms",
    }


def _looks_like_block_start(line: str) -> bool:
    return bool(re.search(AMOUNT_RE, line)) or bool(TAG_RE.match(line))


def group_into_blocks(content: str) -> list[str]:
    """Groups lines into message blocks: a blank line always ends a block,
    and a line that looks like the start of a new bank alert or a tag also
    starts a new block even without a blank line before it — this is what
    keeps a multi-line forwarded SMS (date/UPI-ref/bank-name each on their
    own line) together as one block."""
    blocks, current = [], []
    for raw_line in content.splitlines():
        line = WHATSAPP_PREFIX_RE.sub("", raw_line.strip()).strip()
        if not line:
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        if current and _looks_like_block_start(line):
            blocks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _apply_tag(row: dict, tag_text: str):
    """A reply block that isn't itself a transaction is a manual tag on the
    transaction right before it. 'Office' / 'Personal' control the
    office/personal split (and any text after them becomes the category,
    e.g. 'Personal Home Loan EMI'). Any other freeform reply — 'FD',
    'Cashback', 'Home Loan EMI', 'Bank Interest' — is used verbatim as the
    category (is_office defaults to False for these), so you're not limited
    to a fixed category list: whatever you reply with becomes the category."""
    tag_text = tag_text.strip()
    if not tag_text:
        return
    m = TAG_RE.match(tag_text)
    if m:
        tag_type, rest = m.group(1).lower(), m.group(2).strip()
        row["is_office"] = (tag_type == "office")
        if rest:
            row["category"] = rest
    else:
        row["is_office"] = False
        row["category"] = tag_text
    row["description"] += f" | {tag_text}"


_JUNK_RE = re.compile(r"otp|https?://|customer care|do not share", re.IGNORECASE)


def parse_txt_blob(content: str) -> list[dict]:
    rows = []
    for block in group_into_blocks(content):
        row = parse_sms_line(block)
        if row:
            rows.append(row)
            continue
        # Not a transaction: treat as a tag for the previous one, unless it
        # looks like unrelated boilerplate (OTP/promo) rather than a tag you
        # actually typed.
        if rows and not _JUNK_RE.search(block):
            _apply_tag(rows[-1], block)
    return rows


def parse_sms_file(filename: str, raw_bytes: bytes) -> list[dict]:
    if filename.lower().endswith(".txt"):
        return parse_txt_blob(raw_bytes.decode("utf-8", errors="ignore"))

    if filename.lower().endswith((".csv", ".xlsx", ".xls")):
        if filename.lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(raw_bytes))
        else:
            df = pd.read_excel(io.BytesIO(raw_bytes))
        body_col = next((c for c in df.columns if c.lower() in
                          ("body", "message", "text", "sms", "content")), df.columns[-1])
        date_col = next((c for c in df.columns if "date" in c.lower() or "time" in c.lower()), None)
        rows = []
        for _, r in df.iterrows():
            fallback = r[date_col] if date_col is not None else None
            row = parse_sms_line(str(r[body_col]), fallback_date=fallback)
            if row:
                rows.append(row)
        return rows

    raise ValueError(f"Unsupported SMS file type: {filename}")
