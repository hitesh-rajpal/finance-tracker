"""Reconciliation checks for Dhan contract notes against the ledger.

Two independent checks, both derived from reconciling a real October 2026
statement set by hand:

1. `ledger_match_table` — each contract note's day-total net amount
   should exactly equal that day's "Trades Executed" ledger movement.
   A mismatch here means a genuine booking error on the broker's side
   (or a parsing bug — check the source PDF before assuming the former).

2. `bfcf_continuity_table` — for every open F&O position, the BF
   (brought-forward) price on day N+1 should exactly equal the CF
   (carried-forward) price on day N, since both represent the same
   day N closing settlement price. A break here means either a genuine
   NSE price adjustment (most commonly an extraordinary dividend — see
   SEBI circular SEBI/HO/MRD2/MRD2_DCAP/P/CIR/2022/90, which adjusts
   F&O contracts for dividends at or above 2% of the underlying's
   market value) or a position that was actually closed/rolled, not a
   data error by itself — cross-check against an actual corporate
   action before concluding it's wrong.
"""
import pandas as pd
from collections import defaultdict


def ledger_match_table(contract_notes: dict, ledger_df: pd.DataFrame) -> pd.DataFrame:
    """contract_notes: {date_iso: parsed dict from dhan_parser.parse_contract_note}"""
    trades = ledger_df[ledger_df["Type"] == "Trades Executed"].copy()
    trades["net"] = trades["Credit"] - trades["Debit"]
    ledger_daily = trades.groupby("Date")["net"].sum()

    rows = []
    for date in sorted(contract_notes):
        note = contract_notes[date]
        cn_total = note["segment_totals"].get("Total")
        ts = pd.Timestamp(date)
        ledger_total = ledger_daily.get(ts)
        if cn_total is None or ledger_total is None:
            status = "NO DATA"
            diff = None
        else:
            diff = round(cn_total - ledger_total, 2)
            status = "OK" if abs(diff) < 0.01 else "MISMATCH"
        rows.append({
            "date": date, "contract_no": note.get("contract_no"),
            "contract_note_total": cn_total, "ledger_total": ledger_total,
            "diff": diff, "status": status,
        })
    return pd.DataFrame(rows)


def _identity(r: dict):
    """(symbol, expiry, qty) when available, else falls back to (None, None, qty)
    — the fallback only matters for a row dhan_parser couldn't find a date
    for, which shouldn't happen in practice but shouldn't crash either."""
    return (r.get("symbol"), r.get("expiry"), r["qty"])


def bfcf_continuity_table(contract_notes: dict) -> pd.DataFrame:
    """For each F&O position, day N's CF (closing) price must exactly equal
    day N+1's BF (carried-forward) price for the SAME contract (same
    symbol+expiry+qty, not just the same qty — two different contracts can
    share a lot size, e.g. during a rollover week)."""
    dates = sorted(contract_notes)
    rows = []
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        cf_prev = {_identity(r): r for r in contract_notes[prev]["derivative_rows"] if r["kind"] == "CF"}
        bf_cur = {_identity(r): r for r in contract_notes[cur]["derivative_rows"] if r["kind"] == "BF"}
        for key, bf in bf_cur.items():
            cf = cf_prev.get(key)
            if cf is None:
                continue  # no prior-day CF for this exact contract — see new_position_table
            diff = round(bf["wap"] - cf["wap"], 4)
            status = "OK" if abs(diff) < 0.0001 else "ADJUSTMENT/ERROR"
            rows.append({
                "prev_date": prev, "date": cur, "contract": bf["name"], "qty": bf["qty"],
                "prev_close": cf["wap"], "cur_open": bf["wap"],
                "diff": diff, "status": status,
            })
    return pd.DataFrame(rows)


def new_position_table(contract_notes: dict) -> pd.DataFrame:
    """Flags every F&O position that appears (as a CF row) with no matching
    prior-day CF-continuity — i.e. a brand-new position opened that day —
    and checks it's backed by an actual same-day B/S order at the same
    quantity. A position with no matching order is a red flag: either a
    missing contract note somewhere, or a genuine data error."""
    dates = sorted(contract_notes)
    rows = []
    for idx, date in enumerate(dates):
        note = contract_notes[date]
        bf_keys = {_identity(r) for r in note["derivative_rows"] if r["kind"] == "BF"}
        cf_rows = [r for r in note["derivative_rows"] if r["kind"] == "CF"]
        trade_rows = [r for r in note["derivative_rows"] if r["kind"] in ("B", "S")]
        for cf in cf_rows:
            key = _identity(cf)
            if key in bf_keys:
                continue  # continuing position, carried from a same-day BF
            match = next((t for t in trade_rows if _identity(t)[2] == key[2]
                          and t.get("symbol") == key[0] and t.get("expiry") == key[1]), None)
            rows.append({
                "date": date, "contract": cf["name"], "qty": cf["qty"],
                "matched_order": f"{match['kind']} @ {match['wap']}" if match else None,
                "status": "OK - matched to order" if match else "NO MATCHING ORDER FOUND",
            })
    return pd.DataFrame(rows)


def closed_position_table(contract_notes: dict) -> pd.DataFrame:
    """The mirror check: a position that had a BF (still open coming into
    the day) but no CF that day (not carried forward — closed) should be
    backed by a same-day closing B/S order."""
    dates = sorted(contract_notes)
    rows = []
    for date in dates:
        note = contract_notes[date]
        cf_keys = {_identity(r) for r in note["derivative_rows"] if r["kind"] == "CF"}
        bf_rows = [r for r in note["derivative_rows"] if r["kind"] == "BF"]
        trade_rows = [r for r in note["derivative_rows"] if r["kind"] in ("B", "S")]
        for bf in bf_rows:
            key = _identity(bf)
            if key in cf_keys:
                continue  # still open at day's close
            match = next((t for t in trade_rows if _identity(t)[2] == key[2]
                          and t.get("symbol") == key[0] and t.get("expiry") == key[1]), None)
            rows.append({
                "date": date, "contract": bf["name"], "qty": bf["qty"],
                "matched_order": f"{match['kind']} @ {match['wap']}" if match else None,
                "status": "OK - matched to order" if match else "NO MATCHING ORDER FOUND",
            })
    return pd.DataFrame(rows)


def summarize(ledger_table: pd.DataFrame, continuity_table: pd.DataFrame,
              new_position_tbl: pd.DataFrame | None = None,
              closed_position_tbl: pd.DataFrame | None = None) -> dict:
    def _unmatched(df):
        return int((df["status"] == "NO MATCHING ORDER FOUND").sum()) if df is not None and not df.empty else 0

    return {
        "days_checked": len(ledger_table),
        "ledger_mismatches": int((ledger_table["status"] == "MISMATCH").sum()),
        "ledger_no_data": int((ledger_table["status"] == "NO DATA").sum()),
        "continuity_links_checked": len(continuity_table),
        "continuity_breaks": int((continuity_table["status"] == "ADJUSTMENT/ERROR").sum())
                              if not continuity_table.empty else 0,
        "new_positions": len(new_position_tbl) if new_position_tbl is not None else 0,
        "new_positions_unmatched": _unmatched(new_position_tbl),
        "closed_positions": len(closed_position_tbl) if closed_position_tbl is not None else 0,
        "closed_positions_unmatched": _unmatched(closed_position_tbl),
    }
