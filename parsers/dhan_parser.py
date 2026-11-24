"""Parser for Dhan ledger and trade history files."""

from __future__ import annotations

import pandas as pd
from datetime import date
from typing import Literal


def parse_dhan_ledger(filepath: str) -> pd.DataFrame:
    """
    Parse Dhan ledger Excel file.
    
    The file has a header row at index 9 (0-indexed) with columns:
    Date, Transaction ID, Narration, Voucher Descriptions, Debit, Credit
    """
    df = pd.read_excel(filepath, header=9)
    df.columns = [
        'Date', 'Transaction_ID', 'Narration', 'Voucher_Descriptions',
        'Debit', 'Credit', 'c6', 'c7', 'c8', 'c9', 'c10', 'footer'
    ]
    
    # Drop the first row which is a duplicate header
    df = df.iloc[1:].reset_index(drop=True)
    
    # Parse date
    df['date'] = pd.to_datetime(df['Date'], format='%b %d, %Y', errors='coerce').dt.date
    
    # Parse amounts (Debit is negative/outflow, Credit is positive/inflow)
    df['debit'] = pd.to_numeric(df['Debit'], errors='coerce').fillna(0.0)
    df['credit'] = pd.to_numeric(df['Credit'], errors='coerce').fillna(0.0)
    
    # Net amount: credit + debit (debit is already negative = money out, credit positive = money in)
    df['amount'] = df['credit'] + df['debit']
    
    # Direction
    df['direction'] = df.apply(
        lambda r: 'credit' if r['credit'] > 0 else ('debit' if r['debit'] < 0 else 'none'), axis=1
    )
    
    # Clean up
    df = df.dropna(subset=['date'])
    df = df[~df['Narration'].isin(['Narration', 'Voucher Descriptions'])]  # Remove any stray headers
    
    return df[['date', 'Transaction_ID', 'Narration', 'Voucher_Descriptions', 
               'debit', 'credit', 'amount', 'direction']].copy()


def parse_dhan_trades(filepath: str) -> pd.DataFrame:
    """
    Parse Dhan trade history CSV file.
    
    Columns: Date, Time, Name, Buy/Sell, Order, Exchange, Segment, 
             Quantity/Lot, Trade Price, Trade Value, Status
    """
    df = pd.read_csv(filepath)
    
    # Parse date
    df['date'] = pd.to_datetime(df['Date'], errors='coerce').dt.date
    df['time'] = pd.to_datetime(df['Time'], format='%H:%M:%S', errors='coerce').dt.time
    
    # Parse numeric
    df['quantity'] = pd.to_numeric(df['Quantity/Lot'], errors='coerce').fillna(0).astype(int)
    df['price'] = pd.to_numeric(df['Trade Price'], errors='coerce').fillna(0.0)
    df['value'] = pd.to_numeric(df['Trade Value'], errors='coerce').fillna(0.0)
    
    # Normalize side
    df['side'] = df['Buy/Sell'].str.strip().str.capitalize()  # 'Buy' or 'Sell'
    
    # Segment: 'Equity' or 'Derivative'
    df['segment'] = df['Segment'].str.strip()
    
    # Order type: 'MARGIN' or 'DELIVERY'
    df['order_type'] = df['Order'].str.strip()
    
    # Symbol/contract name
    df['symbol'] = df['Name'].str.strip()
    
    # Exchange
    df['exchange'] = df['Exchange'].str.strip()
    
    # Status
    df['status'] = df['Status'].str.strip()
    
    # Only keep 'Traded' status
    df = df[df['status'] == 'Traded'].copy()
    
    return df[['date', 'time', 'symbol', 'side', 'quantity', 'price', 'value',
               'segment', 'order_type', 'exchange', 'status']].copy()


def extract_symbol_expiry_dhan(symbol: str) -> tuple[str, str | None, Literal['FUT', 'OPT', 'EQ'] | None]:
    """
    Extract base symbol, expiry, and instrument type from Dhan symbol format.
    
    Examples:
    - 'BANDHANBNK NOV FUT' -> ('BANDHANBNK', '2025-11-27', 'FUT')  (last Thursday)
    - 'NIFTY 17 NOV 24500 PUT' -> ('NIFTY', '2025-11-17', 'OPT')
    - 'HDFC Bank' -> ('HDFC Bank', None, 'EQ')
    """
    symbol = symbol.strip().upper()
    
    # Check for FUT format: SYMBOL MON FUT
    import re
    fut_match = re.match(r'^([A-Z0-9&]+)\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+FUT$', symbol)
    if fut_match:
        base = fut_match.group(1)
        month_str = fut_match.group(2)
        # Need to determine year from context - for now return month
        return base, month_str, 'FUT'
    
    # Check for OPT format: SYMBOL DD MON STRIKE CALL/PUT
    opt_match = re.match(r'^([A-Z0-9&]+)\s+(\d{1,2})\s+(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d+(?:\.\d+)?)\s+(CALL|PUT)$', symbol)
    if opt_match:
        base = opt_match.group(1)
        day = opt_match.group(2)
        month = opt_match.group(3)
        strike = opt_match.group(4)
        opt_type = opt_match.group(5)
        return base, f"{day}-{month}", 'OPT'
    
    # Check for equity (no FUT/OPT)
    if 'FUT' not in symbol and 'CALL' not in symbol and 'PUT' not in symbol:
        return symbol, None, 'EQ'
    
    return symbol, None, None


def compute_dhan_positions(trades_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute current open positions from trade history.
    
    For each symbol, match buys and sells (FIFO) to find remaining quantity.
    """
    from collections import defaultdict
    
    # Group by symbol and process chronologically
    trades_df = trades_df.sort_values(['symbol', 'date', 'time']).reset_index(drop=True)
    
    positions = defaultdict(list)  # symbol -> list of (qty, price, date, side)
    
    for _, trade in trades_df.iterrows():
        sym = trade['symbol']
        qty = trade['quantity']
        price = trade['price']
        side = trade['side']
        dt = trade['date']
        
        if side == 'Buy':
            positions[sym].append({'qty': qty, 'price': price, 'date': dt, 'side': 'Buy'})
        else:  # Sell
            remaining = qty
            # FIFO match against existing buys
            new_positions = []
            for pos in positions[sym]:
                if pos['side'] == 'Buy' and pos['qty'] > 0:
                    if pos['qty'] <= remaining:
                        remaining -= pos['qty']
                        pos['qty'] = 0
                    else:
                        pos['qty'] -= remaining
                        remaining = 0
                        break
                new_positions.append(pos)
            
            # If there's remaining sell qty (short position), add as short
            if remaining > 0:
                new_positions.append({'qty': remaining, 'price': price, 'date': dt, 'side': 'Sell'})
            
            positions[sym] = [p for p in new_positions if p['qty'] > 0]
    
    # Build result dataframe
    results = []
    for sym, pos_list in positions.items():
        for pos in pos_list:
            results.append({
                'symbol': sym,
                'side': pos['side'],
                'quantity': pos['qty'],
                'avg_price': pos['price'],
                'open_date': pos['date'],
            })
    
    return pd.DataFrame(results)


def match_dhan_trades_to_ledger(trades_df: pd.DataFrame, ledger_df: pd.DataFrame) -> pd.DataFrame:
    """
    Match Dhan trade executed amounts to ledger 'Trades Executed' entries.
    
    The ledger has daily net trade amounts (F&O and Equity Cash separately).
    We aggregate trades by date and segment and compare.
    
    Sign convention:
    - Ledger: amount = credit + debit (positive = money in to client, negative = money out)
    - Trades: sell = money in (positive), buy = money out (negative)
    - net_value = total_sell - total_buy (should match ledger amount)
    """
    # Aggregate trades by date and segment
    trades_df = trades_df.copy()
    trades_df['signed_value'] = trades_df.apply(
        lambda r: r['value'] if r['side'] == 'Sell' else -r['value'], axis=1
    )
    
    trade_agg = trades_df.groupby(['date', 'segment']).agg(
        total_buy_value=('value', lambda x: x[trades_df.loc[x.index, 'side'] == 'Buy'].sum()),
        total_sell_value=('value', lambda x: x[trades_df.loc[x.index, 'side'] == 'Sell'].sum()),
        net_value=('signed_value', 'sum'),
        trade_count=('value', 'count')
    ).reset_index()
    
    # Filter ledger for trade executions
    ledger_trades = ledger_df[ledger_df['Narration'] == 'Trades Executed'].copy()
    
    # Parse voucher description to get segment
    def get_segment(voucher):
        voucher = str(voucher).upper()
        if 'EQUITY CASH' in voucher:
            return 'Equity'
        elif 'F&O' in voucher or 'FNO' in voucher:
            return 'Derivative'
        return 'Unknown'
    
    ledger_trades['segment'] = ledger_trades['Voucher_Descriptions'].apply(get_segment)
    ledger_trades['ledger_net'] = ledger_trades['amount']  # Already credit - debit
    
    # Merge
    merged = pd.merge(trade_agg, ledger_trades[['date', 'segment', 'ledger_net', 'Transaction_ID']], 
                      on=['date', 'segment'], how='outer', indicator=True)
    
    merged['match_status'] = merged['_merge'].map({
        'both': 'MATCHED',
        'left_only': 'IN TRADES ONLY',
        'right_only': 'IN LEDGER ONLY'
    })
    
    # Compare amounts
    merged['diff'] = merged['net_value'] - merged['ledger_net']
    merged['amount_match'] = merged['diff'].abs() < 1.0  # Within ₹1
    
    return merged


def get_dhan_fund_movements(ledger_df: pd.DataFrame) -> pd.DataFrame:
    """Extract fund deposits and withdrawals from Dhan ledger."""
    funds = ledger_df[ledger_df['Narration'].isin(['Funds Deposited', 'Funds Withdrawal'])].copy()
    funds = funds[['date', 'Narration', 'Voucher_Descriptions', 'amount']].copy()
    funds.columns = ['date', 'type', 'description', 'amount']
    return funds.sort_values('date').reset_index(drop=True)


# ============================================================
# Dhan P&L and Tax P&L parsers (replace missing trade history)
# ============================================================

def parse_dhan_pnl(filepath: str) -> dict[str, pd.DataFrame]:
    """
    Parse Dhan P&L .xls file (regular P&L, not tax).
    
    Returns dict with:
    - 'equity_holdings': current equity positions with unrealized P&L
    - 'fno_trades': F&O trade-level data (realized P&L per contract)
    - 'summary': segment-wise P&L summary
    
    The file has multiple sections:
    1. Segment summary (rows ~6-10)
    2. Equity holdings (rows ~13+)
    3. F&O trades (rows after "F&O Segment" header)
    """
    df = pd.read_excel(filepath, header=None, engine='xlrd')
    
    # Find section headers by scanning all rows
    segment_summary_rows = []
    equity_holdings_rows = []
    fno_trades_rows = []
    
    # First pass: detect section boundaries
    in_summary = False
    in_equity = False
    in_fno = False
    
    for idx, row in df.iterrows():
        first_cell = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        second_cell = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        
        # Detect section headers - match actual Dhan P&L format
        if first_cell == "Segment" and second_cell == "":
            in_summary = True
            in_equity = False
            in_fno = False
            continue
        elif "Equity Segment" in first_cell:
            # Next row will be "Sr." | "Security Name" header, then data rows
            in_summary = False
            in_equity = True
            in_fno = False
            continue
        elif "F&O Segment" in first_cell:
            in_summary = False
            in_equity = False
            in_fno = True
            continue
        elif first_cell in ("Commodities", "Currency") and second_cell == "":
            in_summary = True
            in_equity = False
            in_fno = False
            continue
        
        # Collect rows based on active section
        if in_summary and first_cell in ("Equity", "Futures and Options", "Commodities", "Currency"):
            segment_summary_rows.append(idx)
        elif in_equity and first_cell.isdigit():
            equity_holdings_rows.append(idx)
        elif in_fno and first_cell.isdigit():
            fno_trades_rows.append(idx)
    
    # Parse segment summary
    summary_data = []
    for idx in segment_summary_rows:
        row = df.iloc[idx]
        summary_data.append({
            'segment': str(row.iloc[0]).strip(),
            'buy_value': pd.to_numeric(row.iloc[4], errors='coerce'),
            'sell_value': pd.to_numeric(row.iloc[5], errors='coerce'),
            'gross_pnl': pd.to_numeric(row.iloc[6], errors='coerce'),
            'brokerage': pd.to_numeric(row.iloc[7], errors='coerce'),
            'exchange_charges': pd.to_numeric(row.iloc[8], errors='coerce'),
            'sebi_fees': pd.to_numeric(row.iloc[9], errors='coerce'),
            'gst': pd.to_numeric(row.iloc[10], errors='coerce'),
            'stt': pd.to_numeric(row.iloc[11], errors='coerce'),
            'stamp_duty': pd.to_numeric(row.iloc[12], errors='coerce'),
            'other_charges': pd.to_numeric(row.iloc[13], errors='coerce'),
            'total_charges': pd.to_numeric(row.iloc[14], errors='coerce'),
            'net_pnl': pd.to_numeric(row.iloc[15], errors='coerce'),
        })
    
    # Parse equity holdings (unrealized P&L)
    equity_cols = ['sr', 'security_name', 'isin', 'buy_qty', 'avg_buy_price', 'buy_value',
                   'sell_qty', 'avg_sell_price', 'sell_value', 'realized_pnl', 'realized_pnl_pct',
                   'open_qty', 'open_avg_price', 'closing_rate', 'unrealized_pnl', 'unrealized_pnl_pct']
    equity_data = []
    for idx in equity_holdings_rows:
        row = df.iloc[idx]
        vals = [row.iloc[i] for i in range(min(18, len(row)))]
        if len(vals) >= 15 and pd.notna(vals[1]) and str(vals[1]).strip():
            equity_data.append(dict(zip(equity_cols[:len(vals)], vals)))
    
    # Parse F&O trades
    fno_cols = ['sr', 'security_name', 'isin', 'buy_qty', 'avg_buy_price', 'buy_value',
                'sell_qty', 'avg_sell_price', 'sell_value', 'realized_pnl', 'realized_pnl_pct',
                'open_qty', 'open_avg_price', 'closing_rate', 'unrealized_pnl', 'unrealized_pnl_pct']
    fno_data = []
    for idx in fno_trades_rows:
        row = df.iloc[idx]
        vals = [row.iloc[i] for i in range(min(18, len(row)))]
        if len(vals) >= 15 and pd.notna(vals[1]) and str(vals[1]).strip():
            fno_data.append(dict(zip(fno_cols[:len(vals)], vals)))
    
    return {
        'summary': pd.DataFrame(summary_data) if summary_data else pd.DataFrame(),
        'equity_holdings': pd.DataFrame(equity_data) if equity_data else pd.DataFrame(),
        'fno_trades': pd.DataFrame(fno_data) if fno_data else pd.DataFrame(),
    }


def parse_dhan_tax_pnl(filepath: str) -> dict[str, pd.DataFrame]:
    """
    Parse Dhan Tax P&L .xls file.
    
    Returns dict with:
    - 'equity_trades': equity trade-level data (buy/sell dates, prices, P&L)
    - 'summary': tax category summary (ST, LT, Intraday)
    
    Contains:
    1. Tax summary by holding period (ST, LT, Intraday)
    2. Equity trades with buy/sell dates, quantities, prices, P&L
    """
    df = pd.read_excel(filepath, header=None, engine='xlrd')
    
    # Find the equity trades section
    trades_rows = []
    summary_rows = []
    current_section = None
    
    for idx, row in df.iterrows():
        first_cell = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
        second_cell = str(row.iloc[1]).strip() if pd.notna(row.iloc[1]) else ""
        
        # Check for summary category rows
        if first_cell in ("Intraday / Speculation", "Short Term", "Long Term") and second_cell == "":
            summary_rows.append(idx)
            current_section = "summary"
            continue
        
        # Detect sections - match actual Tax P&L format
        if "Equity Segment" in first_cell and ("Short Term" in first_cell or "Intraday" in first_cell):
            current_section = "trades"
            continue
        elif "Sr." in first_cell and "Security Name" in str(row.iloc[1]):
            current_section = "trades"
            continue
        
        if current_section == "trades" and first_cell.isdigit():
            trades_rows.append(idx)
    
    # Parse tax summary
    summary_data = []
    for idx in summary_rows:
        row = df.iloc[idx]
        cat = str(row.iloc[0]).strip()
        summary_data.append({
            'category': cat,
            'buy_value': pd.to_numeric(row.iloc[3], errors='coerce'),
            'sell_value': pd.to_numeric(row.iloc[4], errors='coerce'),
            'gross_pnl': pd.to_numeric(row.iloc[5], errors='coerce'),
            'brokerage': pd.to_numeric(row.iloc[6], errors='coerce'),
            'exchange_charges': pd.to_numeric(row.iloc[7], errors='coerce'),
            'sebi_fees': pd.to_numeric(row.iloc[8], errors='coerce'),
            'gst': pd.to_numeric(row.iloc[9], errors='coerce'),
            'stt': pd.to_numeric(row.iloc[10], errors='coerce'),
            'stamp_duty': pd.to_numeric(row.iloc[11], errors='coerce'),
            'other_charges': pd.to_numeric(row.iloc[12], errors='coerce'),
            'total_charges': pd.to_numeric(row.iloc[13], errors='coerce'),
            'net_pnl': pd.to_numeric(row.iloc[14], errors='coerce'),
            'turnover': pd.to_numeric(row.iloc[15], errors='coerce'),
        })
    
    # Parse equity trades
    trade_cols = ['sr', 'security_name', 'isin', 'buy_date', 'buy_qty', 'avg_buy_price', 'buy_value',
                  'sell_date', 'sell_qty', 'avg_sell_price', 'sell_value', 'holding_period',
                  'gross_pnl', 'total_charges', 'net_pnl']
    trades_data = []
    for idx in trades_rows:
        row = df.iloc[idx]
        vals = [row.iloc[i] for i in range(min(16, len(row)))]
        if len(vals) >= 14 and pd.notna(vals[1]) and str(vals[1]).strip():
            trades_data.append(dict(zip(trade_cols[:len(vals)], vals)))
    
    return {
        'summary': pd.DataFrame(summary_data) if summary_data else pd.DataFrame(),
        'equity_trades': pd.DataFrame(trades_data) if trades_data else pd.DataFrame(),
    }


def pnl_to_standard_trades(pnl_result: dict) -> pd.DataFrame:
    """
    Convert parsed P&L data to standard trade format (like parse_dhan_trades output).
    
    Combines:
    - F&O trades from regular P&L
    - Equity trades from Tax P&L
    """
    standard_trades = []
    
    # F&O trades from regular P&L
    fno_df = pnl_result.get('fno_trades', pd.DataFrame())
    if not fno_df.empty:
        for _, row in fno_df.iterrows():
            sec_name = str(row.get('security_name', '')).strip()
            if not sec_name:
                continue
            
            # Parse buy side
            buy_qty = pd.to_numeric(row.get('buy_qty', 0), errors='coerce')
            if pd.notna(buy_qty) and buy_qty > 0:
                standard_trades.append({
                    'date': pd.NaT,  # P&L doesn't have dates per trade
                    'time': pd.NaT,
                    'symbol': sec_name,
                    'side': 'Buy',
                    'quantity': int(buy_qty),
                    'price': pd.to_numeric(row.get('avg_buy_price', 0), errors='coerce'),
                    'value': pd.to_numeric(row.get('buy_value', 0), errors='coerce'),
                    'segment': 'Derivative',
                    'order_type': 'MARGIN',
                    'exchange': 'NSE',
                    'status': 'Traded',
                })
            
            # Parse sell side
            sell_qty = pd.to_numeric(row.get('sell_qty', 0), errors='coerce')
            if pd.notna(sell_qty) and sell_qty > 0:
                standard_trades.append({
                    'date': pd.NaT,
                    'time': pd.NaT,
                    'symbol': sec_name,
                    'side': 'Sell',
                    'quantity': int(sell_qty),
                    'price': pd.to_numeric(row.get('avg_sell_price', 0), errors='coerce'),
                    'value': pd.to_numeric(row.get('sell_value', 0), errors='coerce'),
                    'segment': 'Derivative',
                    'order_type': 'MARGIN',
                    'exchange': 'NSE',
                    'status': 'Traded',
                })
    
    return pd.DataFrame(standard_trades)


def tax_pnl_to_standard_trades(tax_pnl_result: dict) -> pd.DataFrame:
    """
    Convert parsed Tax P&L equity trades to standard trade format.
    """
    trades_df = tax_pnl_result.get('equity_trades', pd.DataFrame())
    if trades_df.empty:
        return pd.DataFrame()
    
    standard_trades = []
    for _, row in trades_df.iterrows():
        sec_name = str(row.get('security_name', '')).strip()
        if not sec_name:
            continue
        
        buy_date = row.get('buy_date')
        sell_date = row.get('sell_date')
        buy_qty = pd.to_numeric(row.get('buy_qty', 0), errors='coerce')
        sell_qty = pd.to_numeric(row.get('sell_qty', 0), errors='coerce')
        
        if pd.notna(buy_qty) and buy_qty > 0:
            standard_trades.append({
                'date': pd.to_datetime(buy_date, format='%d-%m-%Y', errors='coerce').date() if pd.notna(buy_date) else pd.NaT,
                'time': pd.NaT,
                'symbol': sec_name,
                'side': 'Buy',
                'quantity': int(buy_qty),
                'price': pd.to_numeric(row.get('avg_buy_price', 0), errors='coerce'),
                'value': pd.to_numeric(row.get('buy_value', 0), errors='coerce'),
                'segment': 'Equity',
                'order_type': 'DELIVERY',
                'exchange': 'NSE',
                'status': 'Traded',
            })
        
        if pd.notna(sell_qty) and sell_qty > 0:
            standard_trades.append({
                'date': pd.to_datetime(sell_date, format='%d-%m-%Y', errors='coerce').date() if pd.notna(sell_date) else pd.NaT,
                'time': pd.NaT,
                'symbol': sec_name,
                'side': 'Sell',
                'quantity': int(sell_qty),
                'price': pd.to_numeric(row.get('avg_sell_price', 0), errors='coerce'),
                'value': pd.to_numeric(row.get('sell_value', 0), errors='coerce'),
                'segment': 'Equity',
                'order_type': 'DELIVERY',
                'exchange': 'NSE',
                'status': 'Traded',
            })
    
    return pd.DataFrame(standard_trades)