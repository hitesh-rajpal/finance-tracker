"""Matcher: Cross-reference carried positions from contract notes with order file trades.

This matches positions that disappeared from contract notes (unexplained closures)
against trades found in 5paisa order files (Equity/FnO/MF transactions).
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parsers.fivepaisa_orders_parser import FivePaisaOrderParser, parse_5paisa_orders_dir
from parsers.fivepaisa_parser import parse_contract_notes


@dataclass
class Position:
    contract: str
    side: str
    qty: int
    wap: float
    close_rate: float
    expiry: date | None
    carried_from: date
    source_note_contract_no: str


@dataclass
class OrderTrade:
    date: date
    symbol: str
    side: str
    quantity: float
    price: float
    contract_key: str
    source_file: str
    raw_contract: str
    expiry: date | None


@dataclass
class MatchResult:
    position: Position
    matched_trades: list[OrderTrade]
    match_type: Literal["exact", "fuzzy", "partial", "none"]
    notes: str


def _normalize_contract(text: str) -> str:
    """Normalize contract for matching - remove special chars, standardize format."""
    if not text:
        return ""
    text = str(text).upper().strip()
    # Remove special chars but keep alphanumeric
    text = re.sub(r"[~:~\-]", "", text)
    # Standardize expiry format
    text = re.sub(r"(\d{1,2})([A-Z]{3})(\d{2,4})", r"\1\2\3", text)
    # Remove O/F/XX etc
    text = re.sub(r"~[A-Z]+", "", text)
    # Remove O: PE: CE: etc
    text = re.sub(r"(PE|CE|XX|F):", "", text)
    # Collapse spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_symbol_expiry(text: str) -> tuple[str, date | None]:
    """Extract symbol and expiry from contract text (5paisa format)."""
    text = str(text).upper().strip()
    
    # 5paisa futures format: SYMBOL~F:DD-MMM-YY or SYMBOL~F:DD-MMM-YYYY
    # 5paisa options format: SYMBOL~O:DD-MMM-YY:PE/CE:STRIKE
    # Contract note format: FUTSTK SYMBOL DDMMMYYYY
    
    expiry = None
    
    # 5paisa order format: SYMBOL~F:DD-MMM-YY or SYMBOL~O:DD-MMM-YY:...
    f_match = re.search(r"([A-Z0-9]+)~F:(\d{1,2}-[A-Z]{3}-\d{2,4})", text)
    if f_match:
        symbol = f_match.group(1)
        try:
            expiry = pd.to_datetime(f_match.group(2), format="%d-%b-%y").date()
        except Exception:
            try:
                expiry = pd.to_datetime(f_match.group(2), format="%d-%b-%Y").date()
            except Exception:
                pass
        return symbol, expiry
    
    # Options: SYMBOL~O:DD-MMM-YY:PE/CE:STRIKE
    o_match = re.search(r"([A-Z0-9]+)~O:(\d{1,2}-[A-Z]{3}-\d{2,4})", text)
    if o_match:
        symbol = o_match.group(1)
        try:
            expiry = pd.to_datetime(o_match.group(2), format="%d-%b-%y").date()
        except Exception:
            try:
                expiry = pd.to_datetime(o_match.group(2), format="%d-%b-%Y").date()
            except Exception:
                pass
        return o_match.group(1), expiry
    
    # Contract note format: FUTSTK SYMBOL DDMMMYYYY or FUTSTK SYMBOL DD-MMM-YYYY
    # Try to find expiry in DDMMMYYYY or DD-MMM-YYYY format
    expiry_match = re.search(r"(\d{1,2}[A-Z]{3}\d{2,4})", text)
    expiry = None
    if expiry_match:
        try:
            expiry = pd.to_datetime(expiry_match.group(1), format="%d%b%Y").date()
        except Exception:
            try:
                expiry = pd.to_datetime(expiry_match.group(1), format="%d%b%Y").date()
            except Exception:
                pass
    
    # Contract note format: FUTSTK SYMBOL DDMMMYYYY
    # Remove FUTSTK/FUTIDX/OPTSTK/OPTIDX prefix
    symbol = re.sub(r"^(FUTSTK|FUTIDX|OPTSTK|OPTIDX|FUTCOM|OPTCOM)\s*", "", text)
    # Remove expiry part
    symbol = re.sub(r"\d{1,2}[A-Z]{3}\d{2,4}.*", "", symbol)
    # Remove special chars
    symbol = re.sub(r"[^A-Z0-9]", "", symbol)
    
    return symbol.strip(), expiry


def _normalize_contract(text: str) -> str:
    """Normalize contract for matching - remove special chars, standardize format."""
    if not text:
        return ""
    text = str(text).upper().strip()
    # Remove special chars but keep alphanumeric
    text = re.sub(r"[~:~\-]", "", text)
    # Standardize expiry format
    text = re.sub(r"(\d{1,2})([A-Z]{3})(\d{2,4})", r"\1\2\3", text)
    # Remove O/F/XX etc
    text = re.sub(r"~[A-Z]+", "", text)
    # Remove O: PE: CE: etc
    text = re.sub(r"(PE|CE|XX|F):", "", text)
    # Collapse spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_order_trades(order_dir: str) -> list[OrderTrade]:
    """Load all trades from order files."""
    results = parse_5paisa_orders_dir(order_dir)
    trades = []
    
    for fname, df in results.items():
        for _, row in df.iterrows():
            d = row.get("parsed_date")
            if pd.isna(d):
                continue
            
            # Extract contract key
            raw_contract = str(row.get("contract", "") or row.get("symbol", "") or "")
            symbol, expiry = _extract_symbol_expiry(raw_contract)
            contract_key = _normalize_contract(raw_contract)
            
            trades.append(OrderTrade(
                date=row["parsed_date"],
                symbol=symbol,
                side=str(row.get("side", "")).strip().capitalize(),
                quantity=float(row.get("quantity", 0) or 0),
                price=float(row.get("price", 0) or 0),
                contract_key=contract_key,
                source_file=row.get("source_file", ""),
                raw_contract=raw_contract,
                expiry=expiry,
            ))
    
    print(f"Loaded {len(trades)} order trades from {len(results)} files")
    return trades


def load_contract_positions(notes_dir: str, password: str, target_dates: set[str] | None = None) -> dict[date, list[Position]]:
    """Load carried positions from contract notes for specific dates."""
    files = list(Path(notes_dir).glob("**/*.pdf"))
    positions_by_date = defaultdict(list)
    total_files = len(files)
    
    # Convert target_dates to date objects for comparison
    target_dates_obj = None
    if target_dates:
        target_dates_obj = {date.fromisoformat(d) for d in target_dates}
    
    for i, p in enumerate(files):
        fname = p.name
        # Extract date from filename
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fname)
        if not m:
            continue
        fdate = m.group(1)
        
        if target_dates and fdate not in target_dates:
            continue
        
        if i % 50 == 0:
            print(f"  Parsing {i+1}/{total_files}: {fname}")
        try:
            with open(p, "rb") as f:
                notes_list = parse_contract_notes(f.read(), password=password)
        except Exception as e:
            print(f"Error parsing {fname}: {e}")
            continue
        
        for n in notes_list:
            if not n.get("trade_date"):
                continue
            d = date.fromisoformat(n["trade_date"])
            if target_dates_obj and d not in target_dates_obj:
                continue
            
            for pos in n.get("positions", []):
                if not pos.get("brought_forward"):
                    continue
                
                desc = pos.get("description", "")
                symbol, expiry = _extract_symbol_expiry(desc)
                
                positions_by_date[d].append(Position(
                    contract=desc.strip(),
                    side=pos.get("side", ""),
                    qty=int(pos.get("qty", 0) or 0),
                    wap=float(pos.get("wap", 0) or 0),
                    close_rate=float(pos.get("close_rate", 0) or 0),
                    expiry=expiry,
                    carried_from=d,
                    source_note_contract_no=n.get("contract_no", ""),
                ))
    
    print(f"Loaded positions from {len(positions_by_date)} trading days")
    return positions_by_date


def match_positions_to_orders(
    positions: dict[date, list[Position]],
    order_trades: list[OrderTrade],
    date_window: int = 14,  # Increased to 14 days to catch delayed closures
) -> list[MatchResult]:
    """Match carried positions to order trades."""
    
    # Index order trades by date
    trades_by_date = defaultdict(list)
    for t in order_trades:
        trades_by_date[t.date].append(t)
    
    results = []
    
    for pos_date, positions in positions.items():
        for pos in positions:
            matched = []
            best_match = None
            best_score = 0
            
            # Extract position symbol and expiry once
            pos_symbol, pos_expiry = _extract_symbol_expiry(pos.contract)
            
            for offset in range(-2, date_window + 1):
                check_date = pos_date + pd.Timedelta(days=offset)
                if check_date not in trades_by_date:
                    continue
                
                for trade in trades_by_date[check_date]:
                    score = 0
                    
                    # Symbol match - EXACT match required for high score
                    if pos_symbol and trade.symbol:
                        if pos_symbol == trade.symbol:
                            score += 15  # Exact symbol match
                        elif pos_symbol in trade.symbol or trade.symbol in pos_symbol:
                            score += 3   # Partial symbol match (low score)
                    
                    # Expiry match
                    if pos_expiry and trade.expiry:
                        if pos_expiry == trade.expiry:
                            score += 10  # Exact expiry match
                        elif abs((pos_expiry - trade.expiry).days) <= 3:
                            score += 5   # Close expiry
                        elif abs((pos_expiry - trade.expiry).days) <= 7:
                            score += 2   # Within a week
                    
                    # Side match (opposite for closing)
                    required_side = "Sell" if pos.side == "B" else "Buy"
                    if trade.side == required_side:
                        score += 8  # Correct closing side
                    elif trade.side in ("Buy", "Sell"):
                        score -= 10  # Wrong side = major penalty
                    
                    # Quantity match
                    if trade.quantity == pos.qty:
                        score += 10  # Exact qty
                    elif abs(trade.quantity - pos.qty) <= max(1, pos.qty * 0.05):
                        score += 5   # Close qty
                    elif abs(trade.quantity - pos.qty) <= pos.qty * 0.2:
                        score += 2   # Reasonable qty
                    
                    # Price proximity (if both have prices)
                    if pos.wap > 0 and trade.price > 0:
                        price_diff_pct = abs(trade.price - pos.wap) / pos.wap
                        if price_diff_pct <= 0.02:
                            score += 3
                        elif price_diff_pct <= 0.05:
                            score += 1
                    
                    if score > best_score:
                        best_score = score
                        best_match = trade
                    
                    # Only add to matched if score is high enough
                    if score >= 15:
                        matched.append(trade)
            
            if best_match and best_score >= 20:
                match_type = "exact"
            elif best_match and best_score >= 15:
                match_type = "fuzzy"
            elif matched:
                match_type = "partial"
            else:
                match_type = "none"
            
            results.append(MatchResult(
                position=pos,
                matched_trades=matched if matched else ([best_match] if best_match else []),
                match_type=match_type,
                notes=f"Best score: {best_score}" if best_match else "No match found",
            ))
    
    return results


def export_unexplained_with_search_terms(
    matches: list[MatchResult],
    output_file: str = "unexplained_closures_export.xlsx",
) -> None:
    """Export unexplained closures with Gmail/5paisa search terms."""
    
    data = []
    for m in matches:
        if m.match_type == "none":
            pos = m.position
            symbol, expiry = _extract_symbol_expiry(pos.contract)
            
            # Build search terms
            gmail_terms = []
            portal_terms = []
            
            # Date range: position date to date+10
            start = pos.carried_from
            end = pos.carried_from + pd.Timedelta(days=10)
            
            # Contract number from note
            cn = pos.source_note_contract_no
            
            # Search terms for Gmail
            if cn:
                gmail_terms.append(f'subject:"Contract Note" "{cn}"')
            gmail_terms.append(f'subject:"Contract Note" "{symbol}"')
            if expiry:
                gmail_terms.append(f'subject:"Contract Note" "{expiry.strftime("%d-%b-%Y").upper()}"')
            
            # Portal search
            portal_terms.append(f"Symbol: {symbol}")
            if expiry:
                portal_terms.append(f"Expiry: {expiry.strftime('%d-%b-%Y')}")
            portal_terms.append(f"Date range: {pos.carried_from} to {end}")
            
            data.append({
                "Position Date": pos.carried_from,
                "Closure Date": pos.carried_from + pd.Timedelta(days=1),  # Approximate
                "Contract": pos.contract,
                "Symbol": symbol,
                "Expiry": expiry,
                "Side": pos.side,
                "Qty": pos.qty,
                "WAP": pos.wap,
                "Close Rate": pos.close_rate,
                "Note Contract No": pos.source_note_contract_no,
                "Gmail Search": " OR ".join(gmail_terms[:3]),
                "Portal Search": " | ".join(portal_terms),
                "Notes": "Check 5paisa portal > Reports > Contract Notes for exact date",
            })
    
    df = pd.DataFrame(data)
    df.to_excel(output_file, index=False)
    print(f"Exported {len(df)} unexplained closures to {output_file}")


def main():
    # Paths
    notes_dir = r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment\Email_Downloads\5paisa_HUF_52598262\Contract_Notes"
    order_dir = r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment\5paisa_HUF_52598262\Transactions"
    password = "AAHHH2655H"
    
    # Load recon data to get unexplained closure dates
    recon_file = Path(__file__).resolve().parent.parent / "data" / "fivepaisa_recon.json"
    with open(recon_file) as f:
        recon = json.load(f)
    
    # Get all dates from unexplained closures (both prev_date and date)
    unexplained = recon.get("unexplained_closures", [])
    target_dates = set()
    for u in unexplained:
        target_dates.add(u["prev_date"])
        target_dates.add(u["date"])
    
    # Also add the actual trade dates where closing happened
    target_dates.update({"2024-11-22", "2024-11-25", "2024-12-20", "2024-12-23", "2024-12-24", "2024-12-26", "2025-01-29", "2025-02-20", "2025-03-04", "2025-04-27", "2025-05-28", "2025-06-20", "2025-07-28", "2025-08-24", "2025-09-09", "2025-10-22", "2025-10-27", "2025-11-13", "2025-11-24", "2025-12-23", "2025-12-29", "2026-01-23", "2026-01-27", "2026-02-17", "2026-02-19", "2026-02-24", "2026-02-27", "2026-03-30", "2026-04-27", "2026-05-25", "2026-07-27", "2026-07-28", "2026-08-24", "2026-09-09", "2026-10-22", "2026-10-27", "2026-11-16"})
    
    print(f"Target dates for position loading: {len(target_dates)}")
    
    print("Loading order trades...")
    order_trades = load_order_trades(
        r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment\5paisa_HUF_52598262\Transactions"
    )
    
    print("Loading contract note positions...")
    positions = load_contract_positions(notes_dir, "AAHHH2655H", target_dates=target_dates)
    
    print("Matching positions to order trades...")
    matches = match_positions_to_orders(positions, order_trades)
    
    # Stats
    exact = sum(1 for m in matches if m.match_type == "exact")
    fuzzy = sum(1 for m in matches if m.match_type == "fuzzy")
    partial = sum(1 for m in matches if m.match_type == "partial")
    none = sum(1 for m in matches if m.match_type == "none")
    
    print(f"\n=== MATCH RESULTS ===")
    print(f"Total positions checked: {len(matches)}")
    print(f"Exact matches: {exact}")
    print(f"Fuzzy matches: {fuzzy}")
    print(f"Partial matches: {partial}")
    print(f"No match: {none}")
    
    # Export unexplained
    export_unexplained_with_search_terms(matches, "unexplained_40_closures.xlsx")
    
    # Also save full match details
    match_data = []
    for m in matches:
        pos = m.position
        for t in m.matched_trades:
            match_data.append({
                "Position Date": pos.carried_from,
                "Position Contract": pos.contract,
                "Position Side": pos.side,
                "Position Qty": pos.qty,
                "Position WAP": pos.wap,
                "Trade Date": t.date,
                "Trade Symbol": t.symbol,
                "Trade Side": t.side,
                "Trade Qty": t.quantity,
                "Trade Price": t.price,
                "Match Type": m.match_type,
                "Notes": m.notes,
            })
    
    if match_data:
        pd.DataFrame(match_data).to_excel("position_order_matches.xlsx", index=False)
        print("Saved position_order_matches.xlsx")
    
    return matches


if __name__ == "__main__":
    main()