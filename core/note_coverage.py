"""Works out what contract notes are present on disk, what's missing, and
what's still pending.

Coverage is judged against the broker's own trade history, not a generic
weekday calendar:

* If a trade happened on a date in the downloaded range but no contract note
  is on disk, that date is **missing** — a real gap worth fixing.
* If a weekday has no note and no trade, it's **quiet** — a holiday, or a day
  with nothing carried forward. It is not treated as missing.

The scan result is saved to Supabase (note_scan_runs / note_scan_days) so the
Streamlit app can display it even though the files themselves are only on
this PC. `scripts/note_coverage.py` is the entry point that does the scanning
and persistence.
"""
from __future__ import annotations

import glob
import os
import re
from datetime import date, datetime, timedelta

import pandas as pd

from config import note_archive

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
KIND_LABELS = {
    "contract_note": "Contract note",
    "commodity": "Commodity contract note",
    "sof_sos": "SOF/SOS statement",
}


def _note_kind(filename: str, prefixes: dict[str, str]) -> str | None:
    # Longest prefix first, so "CommodityContract_" can never be mistaken
    # for something shorter.
    for prefix in sorted(prefixes, key=len, reverse=True):
        if filename.startswith(prefix):
            return prefixes[prefix]
    return None


def scan_note_files(broker: str, root: str | None = None) -> list[dict]:
    """Every PDF in the broker's contract-note folders, with date + kind."""
    cfg = note_archive.BROKERS[broker]
    root = root or note_archive.ARCHIVE_ROOT
    pattern = os.path.join(root, cfg["notes_glob"])
    files = []
    for path in glob.glob(pattern, recursive=True):
        name = os.path.basename(path)
        kind = _note_kind(name, cfg["note_prefixes"])
        m = DATE_RE.search(name)
        if not kind or not m:
            continue
        files.append({
            "path": path,
            "file_name": name,
            "kind": kind,
            "note_date": date.fromisoformat(m.group(1)),
        })
    return sorted(files, key=lambda f: (f["note_date"], f["kind"], f["file_name"]))


def _read_dhan_trade_dates(path: str) -> set[date]:
    df = pd.read_csv(path)
    dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
    return set(dates.dt.date)


def _read_5paisa_trade_dates(pattern: str) -> set[date]:
    dates: set[date] = set()
    for path in glob.glob(pattern):
        # Mutual-fund transactions don't produce contract notes.
        if "MF_" in os.path.basename(path).upper():
            continue
        probe = pd.read_excel(path, header=None, nrows=15)
        header_row = None
        for i in range(len(probe)):
            row_text = " ".join(str(v) for v in probe.iloc[i].tolist()).lower()
            if "transaction date" in row_text:
                header_row = i
                break
        if header_row is None:
            continue
        df = pd.read_excel(path, header=header_row)
        df.columns = [str(c).strip() for c in df.columns]
        date_col = next((c for c in df.columns if "date" in c.lower()), None)
        if date_col is None:
            continue
        if "Quantity" in df.columns:
            df = df[pd.to_numeric(df["Quantity"], errors="coerce").notna()]
        parsed = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True).dropna()
        dates |= set(parsed.dt.date)
    return dates


def _read_5paisa_bill_dates(ledger_path: str) -> set[date]:
    """5paisa's ledger has one 'Bill - FNO' / 'Bill - Cash' /
    'Bill - Commodities' row per traded day, which is a better expected-date
    source than the transaction export (it covers segments the export
    omits). Falls back to an empty set if the layout isn't recognised."""
    try:
        probe = pd.read_excel(ledger_path, header=None, nrows=20)
        header_row = None
        for i in range(len(probe)):
            row_text = " ".join(str(v) for v in probe.iloc[i].tolist()).lower()
            if "transaction date" in row_text:
                header_row = i
                break
        if header_row is None:
            return set()
        df = pd.read_excel(ledger_path, header=header_row)
        df.columns = [str(c).strip() for c in df.columns]
        date_col = next((c for c in df.columns if "date" in c.lower()), None)
        particular_col = next((c for c in df.columns if "particular" in c.lower()), None)
        if date_col is None or particular_col is None:
            return set()
        df = df[df[particular_col].astype(str).str.contains("bill", case=False, na=False)]
        parsed = pd.to_datetime(df[date_col], errors="coerce", dayfirst=True).dropna()
        return set(parsed.dt.date)
    except Exception:
        return set()


def read_trade_dates(broker: str, root: str | None = None) -> tuple[set[date], list[str]]:
    """Dates on which the broker actually traded, plus the files used."""
    cfg = note_archive.BROKERS[broker]
    root = root or note_archive.ARCHIVE_ROOT

    if cfg.get("ledger_glob"):
        ledger_paths = sorted(glob.glob(os.path.join(root, cfg["ledger_glob"])))
        if ledger_paths:
            dates = _read_5paisa_bill_dates(ledger_paths[0])
            if dates:
                return dates, [os.path.basename(ledger_paths[0])]

    pattern = os.path.join(root, cfg["trades_glob"])
    paths = sorted(glob.glob(pattern))
    if cfg["trades_format"] != "dhan_csv":
        # Mutual-fund transactions don't produce contract notes.
        paths = [p for p in paths if "MF_" not in os.path.basename(p).upper()]
    if not paths:
        return set(), []
    if cfg["trades_format"] == "dhan_csv":
        dates = _read_dhan_trade_dates(paths[0])
    else:
        dates = _read_5paisa_trade_dates(pattern)
    return dates, [os.path.basename(p) for p in paths]


def business_days(start: date, end: date) -> list[date]:
    days = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def probe_readable(pdf_path: str | None, passwords: list[str] | None) -> bool | None:
    """True if a password opens the first note, False if every supplied
    password failed, None if we had nothing to try / no file to try it on."""
    if not pdf_path or not passwords:
        return None
    try:
        import pdfplumber
    except ImportError:
        return None
    for password in passwords:
        if not password:
            continue
        try:
            with pdfplumber.open(pdf_path, password=password):
                return True
        except Exception:
            continue
    return False


def scan_broker(
    broker: str,
    root: str | None = None,
    passwords: list[str] | None = None,
    today: date | None = None,
) -> dict:
    """Returns the full coverage report for one broker (dates + counts)."""
    cfg = note_archive.BROKERS[broker]
    root = root or note_archive.ARCHIVE_ROOT
    files = scan_note_files(broker, root)

    contract_kinds = cfg["contract_kinds"]
    contract_files = [f for f in files if f["kind"] in contract_kinds]
    note_dates = sorted({f["note_date"] for f in contract_files})
    if not note_dates:
        return {
            "broker": broker,
            "label": cfg["label"],
            "archive_root": root,
            "scanned_at": datetime.now().isoformat(timespec="seconds"),
            "range_start": None,
            "range_end": None,
            "note_count": 0,
            "note_days": 0,
            "commodity_count": sum(1 for f in files if f["kind"] == "commodity"),
            "sof_sos_count": sum(1 for f in files if f["kind"] == "sof_sos"),
            "duplicate_days": 0,
            "expected_trade_days": 0,
            "missing_trade_days": 0,
            "quiet_weekdays": 0,
            "pre_range_trade_days": 0,
            "readable": None,
            "pending_notes": ["No contract notes found in the configured folder."],
            "trade_source": [],
            "days": [],
            "missing_dates": [],
            "duplicate_dates": [],
            "quiet_dates": [],
        }

    range_start, range_end = note_dates[0], note_dates[-1]
    trade_dates, trade_source = read_trade_dates(broker, root)
    expected = {d for d in trade_dates if range_start <= d <= range_end}
    present = set(note_dates)
    missing = sorted(expected - present)
    quiet = sorted(set(business_days(range_start, range_end)) - present - expected)
    pre_range = sorted(d for d in trade_dates if d < range_start)

    counts_by_date: dict[date, dict[str, int]] = {}
    for f in contract_files:
        by_kind = counts_by_date.setdefault(f["note_date"], {})
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
    duplicates = {
        d: kinds for d, kinds in counts_by_date.items() if any(n > 1 for n in kinds.values())
    }

    first_contract = next((f for f in contract_files if f["kind"] == "contract_note"), None)
    readable = probe_readable(first_contract["path"] if first_contract else None, passwords)

    pending = []
    if note_archive.KNOWN_PENDING.get(broker):
        pending.append(note_archive.KNOWN_PENDING[broker])
    if readable is False:
        pending.append(
            "PDFs are password-protected and none of the saved passwords opens them — "
            "parsing/import is blocked until the correct password is available."
        )
    if missing:
        pending.append(f"{len(missing)} traded date(s) have no contract note on disk.")
    if pre_range:
        pending.append(
            f"{len(pre_range)} traded date(s) before the earliest downloaded note "
            f"({range_start}) are visible in the local statements; the older-notes total "
            "above can include earlier years those statements don't cover."
        )

    days = []
    for d in sorted(present | set(missing) | set(quiet)):
        kinds = sorted(counts_by_date.get(d, {}))
        if d in present:
            status = "present"
        elif d in expected:
            status = "missing"
        else:
            status = "quiet"
        days.append({
            "note_date": d,
            "status": status,
            "traded": d in expected,
            "kinds": kinds,
            "file_count": sum(counts_by_date.get(d, {}).values()),
        })

    return {
        "broker": broker,
        "label": cfg["label"],
        "archive_root": root,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "range_start": range_start,
        "range_end": range_end,
        "note_count": sum(1 for f in contract_files if f["kind"] == "contract_note"),
        "note_days": len(present),
        "commodity_count": sum(1 for f in files if f["kind"] == "commodity"),
        "sof_sos_count": sum(1 for f in files if f["kind"] == "sof_sos"),
        "duplicate_days": len(duplicates),
        "expected_trade_days": len(expected),
        "missing_trade_days": len(missing),
        "quiet_weekdays": len(quiet),
        "pre_range_trade_days": len(pre_range),
        "readable": readable,
        "pending_notes": pending,
        "trade_source": trade_source,
        "days": days,
        "missing_dates": [d.isoformat() for d in missing],
        "duplicate_dates": [
            {
                "date": d.isoformat(),
                "counts": counts_by_date[d],
            }
            for d in sorted(duplicates)
        ],
        "quiet_dates": [d.isoformat() for d in quiet],
    }


def report_to_markdown(reports: list[dict]) -> str:
    lines = [
        "# Contract note archive — coverage",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/note_coverage.py`.",
        "",
        "Coverage is measured against each broker's own trade history. A date with trades "
        "but no note is **missing**; a weekday with neither a note nor a trade is **quiet** "
        "(holiday or no position), not missing.",
        "",
    ]
    for r in reports:
        lines += [
            f"## {r['label']}",
            "",
            f"- Downloaded range: **{r['range_start']} → {r['range_end']}**"
            if r["range_start"] else "- Downloaded range: **none**",
            f"- Contract notes on disk: **{r['note_count']}** "
            f"({r['note_days']} distinct dates"
            + (f", plus {r['commodity_count']} commodity note(s)" if r["commodity_count"] else "")
            + (f", plus {r['sof_sos_count']} SOF/SOS statement(s)" if r["sof_sos_count"] else "")
            + ")",
            f"- Trade dates inside that range: **{r['expected_trade_days']}**",
            f"- Missing — traded but no note: **{r['missing_trade_days']}**",
            f"- Quiet weekdays — no note and no trade: **{r['quiet_weekdays']}**",
            f"- Trade dates before the range: **{r['pre_range_trade_days']}**",
            f"- PDF password: **{'opens' if r['readable'] else ('blocked' if r['readable'] is False else 'not checked')}**",
        ]
        if r["trade_source"]:
            lines.append(f"- Trade history used: `{'`, `'.join(r['trade_source'])}`")
        lines.append("")
        lines.append("### Pending")
        lines.append("")
        if r["pending_notes"]:
            lines += [f"- {p}" for p in r["pending_notes"]]
        else:
            lines.append("- Nothing pending.")
        lines.append("")
        if r["missing_dates"]:
            lines += ["### Missing dates", "", "```", *r["missing_dates"], "```", ""]
        if r["duplicate_dates"]:
            lines.append("### Dates with more than one file")
            lines.append("")
            for d in r["duplicate_dates"]:
                kinds = ", ".join(f"{KIND_LABELS.get(k, k)} ×{n}" for k, n in d["counts"].items())
                lines.append(f"- {d['date']} — {kinds}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
