"""Reconciliation checks for 5paisa contract notes against the 5paisa ledger.

Three independent checks:

1. `ledger_match_table` — every traded day's contract-note net amount (summed
   across the day's F&O/equity note and any commodity note) must equal that
   day's "Bill - FNO" + "Bill - Cash" + "Bill - Commodities" ledger movement.
   A difference that is exactly the right magnitude but the wrong sign is
   reported separately as a sign anomaly, because that pattern points at a
   broker-side sign convention problem rather than a missing or wrong amount.

2. `bfcf_continuity_table` — for every position carried into a day (marked
   B/F on the note), that day's opening price (`wap`) must equal the previous
   note day's closing price (`close_rate`) for the same contract.

3. `closed_position_table` — a position carried into a day that isn't on the
   next note should be explained by a closing trade (on either note, since
   5paisa prints same-day rolls on the note where the position was still
   carried in) or by reaching its expiry.

New positions can't be checked the same way: 5paisa prints a position
opened that day as a transaction row, indistinguishable from a trade, so the
notes have no reliable "this position is brand new" signal.

Notes are passed in as ``{iso_date: [parsed note, ...]}`` because 5paisa can
issue more than one note on a day (equity/F&O plus a separate commodity note).
"""
from __future__ import annotations

import re
from datetime import date

import pandas as pd

BILL_PARTICULARS = ("Bill - FNO", "Bill - Cash", "Bill - Commodities")
EXPIRY_RE = re.compile(r"\b(\d{1,2})([A-Za-z]{3})(\d{4})\b")


def _as_date(value) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _note_daily_net(notes: list[dict]) -> float:
    return round(sum(n.get("net_amount") or 0.0 for n in notes), 2)


def dedupe_notes(notes: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Drops byte-for-byte duplicate copies of the same note.

    The archive contains a couple of `_alt_` downloads of the same contract
    note; counting both would double that day's total and make an exact
    ledger match look like a mismatch.
    """
    out = {}
    for iso_date, day_notes in notes.items():
        seen = set()
        unique = []
        for note in day_notes:
            key = (note.get("contract_no"), note.get("trade_date"), note.get("net_amount"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(note)
        out[iso_date] = unique
    return out


def _bill_daily_totals(ledger_df: pd.DataFrame) -> dict[date, float]:
    bills = ledger_df[ledger_df["Particular"].isin(BILL_PARTICULARS)]
    return {_as_date(k): round(float(v), 2) for k, v in bills.groupby("Date")["Net"].sum().items()}


def ledger_match_table(notes: dict[str, list[dict]], ledger_df: pd.DataFrame) -> pd.DataFrame:
    """Compares each day's note net with that day's ledger bill total.

    Status:
      OK             — the amounts agree.
      SIGN ANOMALY   — same magnitude, opposite sign. The note's own
                       Payin/Payout Obligation line is kept in
                       `note_obligation`; where it matches the ledger, the
                       note's "Net Amount" line is the one with the wrong
                       direction.
      MISMATCH       — the amounts genuinely differ.
      NO LEDGER BILL — a note exists for a day the ledger has no bill for.
    """
    ledger_daily = _bill_daily_totals(ledger_df)
    rows = []
    for iso_date in sorted(notes):
        note_total = _note_daily_net(notes[iso_date])
        ledger_total = ledger_daily.get(date.fromisoformat(iso_date))
        if ledger_total is None:
            status, diff = "NO LEDGER BILL", None
        else:
            diff = round(note_total - ledger_total, 2)
            if abs(diff) < 0.01:
                status = "OK"
            elif abs(note_total + ledger_total) < 0.01:
                status = "SIGN ANOMALY"
            else:
                status = "MISMATCH"
        obligations = [
            n["obligation"] for n in notes[iso_date] if n.get("obligation") is not None
        ]
        rows.append({
            "date": iso_date,
            "notes": len(notes[iso_date]),
            "contract_nos": ", ".join(filter(None, (n.get("contract_no") for n in notes[iso_date]))),
            "note_total": note_total,
            "note_obligation": round(sum(obligations), 2) if obligations else None,
            "ledger_total": ledger_total,
            "diff": diff,
            "status": status,
        })
    return pd.DataFrame(rows)


def _expiry(description: str) -> date | None:
    m = EXPIRY_RE.search(description or "")
    if not m:
        return None
    try:
        return pd.to_datetime("".join(m.groups()), format="%d%b%Y").date()
    except ValueError:
        return None


def _normalize_contract(text: str) -> str:
    """Same contract is printed two ways — '27-Oct-2026' for that day's
    trades, '27Oct2026' for carried positions — so keys must ignore the
    dashes or every roll looks like a position that vanished."""
    text = " ".join((text or "").split())
    return re.sub(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b", r"\1\2\3", text)


def _position_key(pos: dict) -> tuple[str, str]:
    # Description carries symbol + expiry (e.g. "FUTSTK AXISBANK 30Dec2025"),
    # so it identifies the contract; side says which way the position runs.
    return (_normalize_contract(pos.get("description")), pos.get("side"))


def _is_open_position(pos: dict) -> bool:
    """5paisa's derivative table interleaves the day's own trades with the
    carried positions. A carried position is flagged B/F; one opened today
    and still open at the close has that day's closing rate filled in. A row
    with neither is the day's trade line (its price columns describe the
    trade, not a carried position)."""
    return bool(pos.get("brought_forward")) or (pos.get("close_rate") or 0.0) > 0


def _is_same_day_trade_row(pos: dict) -> bool:
    """Trades executed that day are printed with a dashed expiry
    ("27-Oct-2026"); carried positions use the compact form ("27Oct2026").
    The annexure repeats the trades, so these rows are not positions."""
    return bool(re.search(r"\b\d{1,2}-[A-Za-z]{3}-\d{4}\b", pos.get("description") or ""))


def _positions(
    notes: dict[str, list[dict]],
    iso_date: str,
    *,
    carried_only: bool = False,
    include_same_day_trades: bool = False,
) -> dict[tuple[str, str], dict]:
    """Positions on a note, keyed by (contract, side).

    Carried positions win over same-day trade rows for the same contract,
    because the trade row's price columns describe the fill, not the
    position. `include_same_day_trades` is used where a position first
    appearing as a trade row still matters (e.g. continuity from the day it
    was opened, or checking a position was at least traded today).
    """
    carried: dict[tuple[str, str], dict] = {}
    traded: dict[tuple[str, str], dict] = {}
    for note in notes[iso_date]:
        for pos in note.get("positions") or []:
            if not _is_open_position(pos):
                continue
            bucket = traded if _is_same_day_trade_row(pos) else carried
            bucket[_position_key(pos)] = pos
    out = dict(carried)
    if include_same_day_trades:
        for key, pos in traded.items():
            out.setdefault(key, pos)
    if carried_only:
        out = {k: v for k, v in out.items() if v.get("brought_forward")}
    return out


def bfcf_continuity_table(notes: dict[str, list[dict]]) -> pd.DataFrame:
    """Every B/F position's opening `wap` must equal the previous note day's
    `close_rate` for the same contract."""
    dates = sorted(notes)
    rows = []
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        prev_positions = _positions(notes, prev, include_same_day_trades=True)
        cur_positions = _positions(notes, cur, carried_only=True)
        for key, pos in cur_positions.items():
            if not pos.get("brought_forward"):
                continue
            before = prev_positions.get(key)
            if before is None:
                continue  # flagged by new_position_table instead
            diff = round((pos["wap"] or 0.0) - (before["close_rate"] or 0.0), 4)
            rows.append({
                "prev_date": prev,
                "date": cur,
                "contract": key[0],
                "side": key[1],
                "qty": pos["qty"],
                "prev_close": before["close_rate"],
                "cur_open": pos["wap"],
                "diff": diff,
                "status": "OK" if abs(diff) < 0.0001 else "ADJUSTMENT/ERROR",
            })
    return pd.DataFrame(rows)


def _trade_matches(trade: dict, description: str) -> bool:
    return _normalize_contract(trade.get("description")) == description


def closed_position_table(notes: dict[str, list[dict]]) -> pd.DataFrame:
    """A carried position that isn't on the next note should be explained by
    a closing trade on the note it was last seen on (the usual same-day roll)
    or on the next note, or by having reached its expiry."""
    dates = sorted(notes)
    rows = []
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        before = _positions(notes, prev, carried_only=True)
        after = _positions(notes, cur, include_same_day_trades=True)
        prev_trades = [t for n in notes[prev] for t in n.get("trades") or []]
        cur_trades = [t for n in notes[cur] for t in n.get("trades") or []]
        cur_day = date.fromisoformat(cur)
        for key, pos in before.items():
            if key in after:
                continue
            trade = next(
                (t for t in cur_trades + prev_trades
                 if _trade_matches(t, key[0])
                 and (t.get("side") == "BUY") != (key[1] == "B")),
                None,
            )
            expiry = _expiry(key[0])
            if trade:
                status = "OK - matched to trade"
            elif expiry is not None and expiry <= cur_day:
                status = "OK - expired"
            else:
                status = "NO CLOSING TRADE FOUND"
            rows.append({
                "prev_date": prev,
                "date": cur,
                "contract": key[0],
                "side": key[1],
                "qty": pos["qty"],
                "matched_trade": f"{trade['side']} {trade['quantity']:g} @ {trade['price']:g}"
                if trade else None,
                "status": status,
            })
    return pd.DataFrame(rows)


def summarize(
    ledger_table: pd.DataFrame,
    continuity_table: pd.DataFrame,
    closed_positions: pd.DataFrame | None = None,
) -> dict:
    def _count(df: pd.DataFrame | None, statuses) -> int:
        if df is None or df.empty:
            return 0
        return int(df["status"].isin(statuses).sum())

    return {
        "days_checked": len(ledger_table),
        "ledger_mismatches": _count(ledger_table, {"MISMATCH"}),
        "sign_anomalies": _count(ledger_table, {"SIGN ANOMALY"}),
        "ledger_missing": _count(ledger_table, {"NO LEDGER BILL"}),
        "continuity_links_checked": len(continuity_table),
        "continuity_breaks": _count(continuity_table, {"ADJUSTMENT/ERROR"}),
        "closed_positions": len(closed_positions) if closed_positions is not None else 0,
        "closed_positions_unmatched": _count(closed_positions, {"NO CLOSING TRADE FOUND"}),
        "closed_positions_expired": _count(closed_positions, {"OK - expired"}),
    }
