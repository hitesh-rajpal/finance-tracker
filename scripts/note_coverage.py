"""Scan the local contract-note archive and record what's present vs missing.

Run from the project folder:

    python scripts/note_coverage.py
    python scripts/note_coverage.py --broker 5paisa --password "..." 
    python scripts/note_coverage.py --no-db          # local files only

It:
  * counts every contract note / commodity note / SOF-SOS file by date,
  * compares those dates with the broker's own trade history,
  * flags dates where the broker traded but no note is on disk,
  * checks whether the PDFs open with any saved PDF password,
  * saves the result to Supabase (note_scan_runs / note_scan_days),
  * writes data/note_coverage.json and docs/contract_notes_status.md.

Re-run it whenever more notes are downloaded — each run is kept separately,
so progress over time is visible in the app's Note Coverage tab.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import note_archive  # noqa: E402
from core import note_coverage  # noqa: E402


def _saved_passwords() -> list[str]:
    try:
        from core.storage import get_pdf_passwords

        return [p["password"] for p in get_pdf_passwords() if p.get("password")]
    except Exception as exc:  # DB unavailable / not configured
        print(f"  (could not read saved PDF passwords: {type(exc).__name__}: {exc})")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=note_archive.ARCHIVE_ROOT,
                        help="Archive root (default: config/note_archive.py)")
    parser.add_argument("--broker", action="append", choices=sorted(note_archive.BROKERS),
                        help="Broker to scan (repeatable; default: all)")
    parser.add_argument("--password", action="append", default=[],
                        help="Extra PDF password to try (repeatable)")
    parser.add_argument("--no-db", action="store_true",
                        help="Only write the local JSON/Markdown report")
    args = parser.parse_args()

    brokers = args.broker or list(note_archive.BROKERS)
    passwords = list(dict.fromkeys(args.password + _saved_passwords()))

    if not args.no_db:
        try:
            from core.storage import init_db, save_note_scan

            init_db()
        except Exception as exc:
            print(f"Database unavailable ({type(exc).__name__}: {exc}); continuing without it.")
            save_note_scan = None
    else:
        save_note_scan = None

    reports = []
    for broker in brokers:
        print(f"Scanning {broker}…")
        report = note_coverage.scan_broker(broker, root=args.root, passwords=passwords)
        reports.append(report)

        readable = report["readable"]
        readable_text = "opens" if readable else ("BLOCKED (password)" if readable is False else "not checked")
        print(
            f"  {report['note_count']} contract notes / {report['note_days']} dates, "
            f"{report['expected_trade_days']} trade days, "
            f"{report['missing_trade_days']} missing, "
            f"{report['quiet_weekdays']} quiet weekdays, "
            f"{report['pre_range_trade_days']} pre-range trade days, PDF {readable_text}"
        )
        for note in report["pending_notes"]:
            print(f"  pending: {note}")

        if save_note_scan:
            run_id = save_note_scan(report)
            print(f"  saved scan {run_id}")

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    json_path = data_dir / "note_coverage.json"
    json_path.write_text(
        json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "reports": reports}, indent=2, default=str),
        encoding="utf-8",
    )

    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    md_path = docs_dir / "contract_notes_status.md"
    md_path.write_text(note_coverage.report_to_markdown(reports), encoding="utf-8")

    print(f"\nWrote {json_path.relative_to(ROOT)} and {md_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
