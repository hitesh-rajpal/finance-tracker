"""Parses 5paisa order/trade transaction files (Equity, F&O, Mutual Funds).

These are xlsx files exported from 5paisa with a standard format:
- Rows 0-6: Header metadata (report name, client code, etc.)
- Row 7: Column headers
- Row 8+: Data rows
"""
from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import pandas as pd


class FivePaisaOrderParser:
    """Parse 5paisa transaction report xlsx files."""

    FILE_TYPES = {
        "equity": "Equity_Transactions",
        "fno": "FnO_Transactions",
        "mf": "MF_Transactions",
    }

    def __init__(self, file_path: str | Path):
        self.file_path = Path(file_path)
        self.file_type = self._detect_file_type()

    def _detect_file_type(self) -> Literal["equity", "fno", "mf"]:
        name = self.file_path.name.upper()
        # Match both 5paisa web export names and the older script names
        if "EQUITY_TRANSACTION" in name:
            return "equity"
        elif "DERIVATIVES_TRANSACTION" in name or "FNO_TRANSACTION" in name or "FNO_TRANSACTIONS" in name:
            return "fno"
        elif "MUTUALFUNDS_TRANSACTION" in name or "MF_TRANSACTION" in name:
            return "mf"
        elif "COMMODITY_TRANSACTION" in name:
            return "fno"  # commodity uses F&O parser
        else:
            # Try to infer from content
            return "equity"

    def parse(self) -> pd.DataFrame:
        """Parse the transaction file and return a cleaned DataFrame."""
        if self.file_type == "equity":
            return self._parse_equity()
        elif self.file_type == "fno":
            return self._parse_fno()
        elif self.file_type == "mf":
            return self._parse_mf()
        else:
            raise ValueError(f"Unknown file type: {self.file_type}")

    def _read_raw(self) -> pd.DataFrame:
        """Read the Excel file with header at row 7 (0-indexed)."""
        df = pd.read_excel(
            self.file_path,
            sheet_name="TransactionReport",
            header=7,
            engine="openpyxl",
        )
        # Clean column names
        df.columns = [str(c).strip() for c in df.columns]
        # Drop completely empty rows and columns
        df = df.dropna(how="all", axis=0).dropna(how="all", axis=1)
        return df

    def _parse_equity(self) -> pd.DataFrame:
        df = self._read_raw()
        # Standardize column names
        df = df.rename(columns={
            "Transaction Date": "date",
            "Company Name": "symbol",
            "Exchange": "exchange",
            "Type": "side",
            "Quantity": "quantity",
            "Price": "price",
            "Tax/Charges": "tax_charges",
            "Brokerage": "brokerage",
            "Remarks": "remarks",
        })
        df["file_type"] = "equity"
        df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        return self._clean_common(df)

    def _parse_fno(self) -> pd.DataFrame:
        df = self._read_raw()
        # F&O has more columns
        df = df.rename(columns={
            "Transaction Date": "date",
            "Contract": "contract",
            "Expiry Date": "expiry_date",
            "Option Type": "option_type",
            "Strike Price": "strike_price",
            "Type": "side",
            "Quantity": "quantity",
            "Price": "price",
            "Tax/Charges": "tax_charges",
            "Brokerage": "brokerage",
            "Remarks": "remarks",
        })
        df["file_type"] = "fno"
        df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["expiry_parsed"] = pd.to_datetime(df["expiry_date"], errors="coerce", dayfirst=True).dt.date
        return self._clean_common(df)

    def _parse_mf(self) -> pd.DataFrame:
        df = self._read_raw()
        df = df.rename(columns={
            "Transaction Date": "date",
            "Scheme Name": "scheme_name",
            "Type": "side",
            "Quantity": "quantity",
            "Price": "price",
            "Remarks": "remarks",
        })
        df["file_type"] = "mf"
        df["parsed_date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        return self._clean_common(df)

    def _clean_common(self, df: pd.DataFrame) -> pd.DataFrame:
        # Convert numeric columns
        for col in ["quantity", "price", "tax_charges", "brokerage", "strike_price"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        # Parse dates
        if "parsed_date" in df.columns:
            df["date"] = df["parsed_date"]

        # Normalize side
        if "side" in df.columns:
            df["side"] = df["side"].astype(str).str.strip().str.capitalize()
            df["side"] = df["side"].replace({"B": "Buy", "S": "Sell"})

        # Add source file
        df["source_file"] = self.file_path.name

        # Drop completely empty rows
        df = df.dropna(subset=["date"], how="all")

        return df.reset_index(drop=True)


def parse_5paisa_orders(file_path: str | Path) -> pd.DataFrame:
    """Convenience function to parse a 5paisa order file."""
    parser = FivePaisaOrderParser(file_path)
    return parser.parse()


def parse_5paisa_orders_dir(dir_path: str | Path) -> dict[str, pd.DataFrame]:
    """Parse all 5paisa order files in a directory."""
    dir_path = Path(dir_path)
    results = {}
    # Match actual 5paisa export filenames (case-insensitive glob on Windows)
    patterns = [
        "*EQUITY_Transaction*.xlsx",
        "*DERIVATIVES_Transaction*.xlsx",
        "*FNO_Transaction*.xlsx",
        "*MUTUALFUNDS_Transaction*.xlsx",
        "*MF_Transaction*.xlsx",
        "*COMMODITY_Transaction*.xlsx",
    ]
    for pattern in patterns:
        for file_path in dir_path.glob(pattern):
            try:
                results[file_path.name] = parse_5paisa_orders(file_path)
            except Exception as e:
                print(f"Error parsing {file_path.name}: {e}")
    return results