"""Dhan reconciliation: trade history ↔ ledger, position tracking, fund flow."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parsers.dhan_parser import (
    parse_dhan_ledger,
    parse_dhan_trades,
    compute_dhan_positions,
    match_dhan_trades_to_ledger,
    get_dhan_fund_movements,
)


@dataclass
class DhanReconResult:
    trade_ledger_match: pd.DataFrame
    fund_movements: pd.DataFrame
    positions: pd.DataFrame
    dp_charges: pd.DataFrame
    summary: dict


def run_dhan_reconciliation(
    ledger_path: str = None,
    trades_path: str = None,
    ledger_df: pd.DataFrame = None,
    trades_df: pd.DataFrame = None,
) -> DhanReconResult:
    """Run full Dhan reconciliation. Accepts either file paths or pre-parsed DataFrames."""
    
    # Parse files if DataFrames not provided
    if ledger_df is None:
        if ledger_path is None:
            raise ValueError("Either ledger_path or ledger_df must be provided")
        ledger = parse_dhan_ledger(ledger_path)
    else:
        ledger = ledger_df
    
    if trades_df is None:
        if trades_path is None:
            raise ValueError("Either trades_path or trades_df must be provided")
        trades = parse_dhan_trades(trades_path)
    else:
        trades = trades_df
    
    # 1. Match trades to ledger
    trade_match = match_dhan_trades_to_ledger(trades, ledger)
    
    # 2. Fund movements
    funds = get_dhan_fund_movements(ledger)
    
    # 3. Current positions
    positions = compute_dhan_positions(trades)
    
    # 4. DP charges
    dp_charges = ledger[ledger['Narration'] == 'DP Transaction Charges'][
        ['date', 'Transaction_ID', 'Voucher_Descriptions', 'amount']
    ].copy()
    dp_charges.columns = ['date', 'txn_id', 'description', 'amount']
    
    # Summary
    matched = trade_match[trade_match['match_status'] == 'MATCHED']
    equity_match = matched[matched['segment'] == 'Equity']
    deriv_match = matched[matched['segment'] == 'Derivative']
    
    summary = {
        'ledger_date_range': f"{ledger['date'].min()} to {ledger['date'].max()}",
        'trades_date_range': f"{trades['date'].min()} to {trades['date'].max()}",
        'total_ledger_rows': len(ledger),
        'total_trades': len(trades),
        'trade_ledger_matched': len(matched),
        'trade_ledger_in_ledger_only': len(trade_match[trade_match['match_status'] == 'IN LEDGER ONLY']),
        'equity_matches': len(equity_match),
        'equity_amount_matched': int(equity_match['amount_match'].sum()) if len(equity_match) > 0 else 0,
        'derivative_matches': len(deriv_match),
        'derivative_amount_matched': int(deriv_match['amount_match'].sum()) if len(deriv_match) > 0 else 0,
        'total_fund_deposits': len(funds[funds['amount'] > 0]),
        'total_fund_withdrawals': len(funds[funds['amount'] < 0]),
        'total_deposited': float(funds[funds['amount'] > 0]['amount'].sum()),
        'total_withdrawn': float(funds[funds['amount'] < 0]['amount'].sum()),
        'open_positions': len(positions),
        'dp_charges_count': len(dp_charges),
    }
    
    return DhanReconResult(
        trade_ledger_match=trade_match,
        fund_movements=funds,
        positions=positions,
        dp_charges=dp_charges,
        summary=summary,
    )


def export_dhan_recon_excel(result: DhanReconResult, output_path: str) -> None:
    """Export Dhan reconciliation to multi-sheet Excel."""
    with pd.ExcelWriter(output_path, engine='openpyxl') as writer:
        # Sheet 1: Summary
        summary_df = pd.DataFrame([result.summary])
        summary_df.to_excel(writer, sheet_name='Summary', index=False)
        
        # Sheet 2: Trade-Ledger Match
        result.trade_ledger_match.to_excel(writer, sheet_name='Trade-Ledger Match', index=False)
        
        # Sheet 3: Fund Movements
        result.fund_movements.to_excel(writer, sheet_name='Fund Movements', index=False)
        
        # Sheet 4: Current Positions
        result.positions.to_excel(writer, sheet_name='Open Positions', index=False)
        
        # Sheet 5: DP Charges
        result.dp_charges.to_excel(writer, sheet_name='DP Charges', index=False)
        
        # Sheet 6: Trade History (full)
        # Note: would need to pass trades_df separately
        
    print(f"Exported Dhan reconciliation to {output_path}")


def match_dhan_positions_to_trades(
    trades_df: pd.DataFrame,
    date_window: int = 7,
) -> pd.DataFrame:
    """
    Match opening/closing trades to track position lifecycle.
    
    For each buy trade, find the corresponding sell trade(s) that close it.
    """
    from collections import defaultdict
    
    trades_df = trades_df.sort_values(['symbol', 'date', 'time']).reset_index(drop=True)
    
    # Track open buys per symbol
    open_buys = defaultdict(list)  # symbol -> list of {qty, price, date, trade_idx}
    results = []
    
    for idx, trade in trades_df.iterrows():
        sym = trade['symbol']
        qty = trade['quantity']
        price = trade['price']
        side = trade['side']
        dt = trade['date']
        
        if side == 'Buy':
            open_buys[sym].append({
                'buy_idx': idx,
                'buy_date': dt,
                'buy_price': price,
                'qty': qty,
                'remaining': qty,
            })
            results.append({
                'trade_idx': idx,
                'symbol': sym,
                'date': dt,
                'side': side,
                'quantity': qty,
                'price': price,
                'action': 'OPEN',
                'matched_qty': 0,
                'matched_sell_idx': None,
                'matched_sell_date': None,
                'matched_sell_price': None,
                'pnl': None,
            })
        else:  # Sell
            remaining = qty
            sell_price = price
            sell_date = dt
            
            # FIFO match against open buys
            for buy in open_buys[sym]:
                if buy['remaining'] <= 0:
                    continue
                match_qty = min(buy['remaining'], remaining)
                buy['remaining'] -= match_qty
                remaining -= match_qty
                
                # Calculate P&L
                pnl = (sell_price - buy['buy_price']) * match_qty
                
                results.append({
                    'trade_idx': idx,
                    'symbol': sym,
                    'date': dt,
                    'side': side,
                    'quantity': match_qty,
                    'price': sell_price,
                    'action': 'CLOSE',
                    'matched_qty': match_qty,
                    'matched_buy_idx': buy['buy_idx'],
                    'matched_buy_date': buy['buy_date'],
                    'matched_buy_price': buy['buy_price'],
                    'pnl': pnl,
                })
                
                if remaining == 0:
                    break
            
            # If remaining > 0, it's a short sell (no matching buy)
            if remaining > 0:
                results.append({
                    'trade_idx': idx,
                    'symbol': sym,
                    'date': dt,
                    'side': side,
                    'quantity': remaining,
                    'price': sell_price,
                    'action': 'SHORT_OPEN',
                    'matched_qty': 0,
                    'matched_buy_idx': None,
                    'matched_buy_date': None,
                    'matched_buy_price': None,
                    'pnl': None,
                })
    
    # Add any remaining open buys
    for sym, buys in open_buys.items():
        for buy in buys:
            if buy['remaining'] > 0:
                results.append({
                    'trade_idx': None,
                    'symbol': sym,
                    'date': buy['buy_date'],
                    'side': 'Buy',
                    'quantity': buy['remaining'],
                    'price': buy['buy_price'],
                    'action': 'STILL_OPEN',
                    'matched_qty': 0,
                    'matched_sell_idx': None,
                    'matched_sell_date': None,
                    'matched_sell_price': None,
                    'pnl': None,
                })
    
    return pd.DataFrame(results)


if __name__ == '__main__':
    ledger_path = r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment\Dhan_Yashima_1108852293\Ledger\Ledger_2020-04-01_to_2026-11-17.xlsx"
    trades_path = r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment\Dhan_Yashima_1108852293\Trades\Trade_History_2020-04-01_to_2026-11-17.csv"
    
    print("Running Dhan reconciliation...")
    result = run_dhan_reconciliation(ledger_path, trades_path)
    
    print(f"\n=== SUMMARY ===")
    for k, v in result.summary.items():
        print(f"  {k}: {v}")
    
    print(f"\n=== Trade-Ledger Match (mismatches) ===")
    mismatched = result.trade_ledger_match[
        (result.trade_ledger_match['match_status'] == 'MATCHED') & 
        (~result.trade_ledger_match['amount_match'])
    ]
    print(f"Total matched: {len(result.trade_ledger_match[result.trade_ledger_match['match_status'] == 'MATCHED'])}")
    print(f"Amount mismatched: {len(mismatched)}")
    
    print(f"\n=== Fund Movements ===")
    print(f"Deposits: {result.summary['total_fund_deposits']}, Total: {result.summary['total_deposited']:,.2f}")
    print(f"Withdrawals: {result.summary['total_fund_withdrawals']}, Total: {result.summary['total_withdrawn']:,.2f}")
    
    print(f"\n=== Open Positions ({len(result.positions)}) ===")
    if len(result.positions) > 0:
        print(result.positions.to_string())
    
    print(f"\n=== DP Charges ({len(result.dp_charges)}) ===")
    print(result.dp_charges.to_string())
    
    # Position lifecycle
    from parsers.dhan_parser import parse_dhan_trades
    trades = parse_dhan_trades(trades_path)
    lifecycle = match_dhan_positions_to_trades(trades)
    print(f"\n=== Position Lifecycle ===")
    print(f"Total entries: {len(lifecycle)}")
    print(lifecycle['action'].value_counts())
    
    # Export
    export_dhan_recon_excel(result, 'dhan_reconciliation.xlsx')