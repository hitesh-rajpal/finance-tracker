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


def bfcf_continuity_table(contract_notes: dict) -> pd.DataFrame:
    dates = sorted(contract_notes)
    rows = []
    for i in range(1, len(dates)):
        prev, cur = dates[i - 1], dates[i]
        cf_prev = defaultdict(list)
        for r in contract_notes[prev]["derivative_rows"]:
            if r["kind"] == "CF":
                cf_prev[r["qty"]].append((r["name"], r["wap"]))
        bf_cur = defaultdict(list)
        for r in contract_notes[cur]["derivative_rows"]:
            if r["kind"] == "BF":
                bf_cur[r["qty"]].append((r["name"], r["wap"]))
        for qty in bf_cur:
            if qty not in cf_prev:
                continue
            prev_list = sorted(cf_prev[qty], key=lambda x: x[1])
            cur_list = sorted(bf_cur[qty], key=lambda x: x[1])
            for (pname, prate), (cname, crate) in zip(prev_list, cur_list):
                diff = round(crate - prate, 4)
                status = "OK" if abs(diff) < 0.0001 else "ADJUSTMENT/ERROR"
                rows.append({
                    "prev_date": prev, "date": cur, "qty": qty,
                    "contract": cname or pname,
                    "prev_close": prate, "cur_open": crate,
                    "diff": diff, "status": status,
                })
    return pd.DataFrame(rows)


def summarize(ledger_table: pd.DataFrame, continuity_table: pd.DataFrame) -> dict:
    return {
        "days_checked": len(ledger_table),
        "ledger_mismatches": int((ledger_table["status"] == "MISMATCH").sum()),
        "ledger_no_data": int((ledger_table["status"] == "NO DATA").sum()),
        "continuity_links_checked": len(continuity_table),
        "continuity_breaks": int((continuity_table["status"] == "ADJUSTMENT/ERROR").sum())
                              if not continuity_table.empty else 0,
    }
