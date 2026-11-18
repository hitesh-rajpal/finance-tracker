"""Reconcile the whole local Dhan contract-note archive against the ledger.

    python scripts/dhan_bulk_recon.py
    python scripts/dhan_bulk_recon.py --password "<PAN>"

Unlike the app's Dhan Recon tab (which takes uploads one batch at a time),
this walks the archive folder directly, so it can check every downloaded
note in one go and record the result in docs/dhan_recon_status.md.

Checks:
  * every contract note parses (and its filename date matches the note),
  * each day's note total equals that day's ledger "Trades Executed" total,
  * BF/CF prices chain exactly from one day to the next,
  * every new/closed F&O position traces to a real order.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import note_archive  # noqa: E402
from core import dhan_reconcile  # noqa: E402
from parsers import dhan_parser  # noqa: E402


def _saved_passwords() -> list[str]:
    try:
        from core.storage import get_pdf_passwords

        return [p["password"] for p in get_pdf_passwords() if p.get("password")]
    except Exception:
        return []


def _find(root: str, pattern: str) -> list[str]:
    return sorted(glob.glob(os.path.join(root, pattern), recursive=True))


def _pick_password(notes: list[str], passwords: list[str]) -> str | None:
    if not notes:
        return None
    import pdfplumber

    for password in passwords:
        if not password:
            continue
        try:
            with pdfplumber.open(notes[0], password=password):
                return password
        except Exception:
            continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=note_archive.ARCHIVE_ROOT)
    parser.add_argument("--password", action="append", default=[])
    args = parser.parse_args()

    notes_paths = _find(args.root, note_archive.BROKERS["Dhan"]["notes_glob"])
    notes_paths = [p for p in notes_paths if os.path.basename(p).startswith("ContractNote_")]
    ledger_paths = _find(args.root, r"Dhan_*\Ledger\*.xlsx")
    if not notes_paths:
        print("No Dhan contract notes found.")
        return 1
    if not ledger_paths:
        print("No Dhan ledger .xlsx found.")
        return 1

    password = _pick_password(notes_paths, args.password + _saved_passwords())
    if not password:
        print("Could not open the notes — supply a password with --password.")
        return 1

    notes = {}
    failures = []
    date_mismatches = []
    for path in notes_paths:
        filename = os.path.basename(path)
        try:
            note = dhan_parser.parse_contract_note(Path(path).read_bytes(), password=password)
        except Exception as exc:
            failures.append({"file": filename, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not note["trade_date"]:
            failures.append({"file": filename, "error": "no trade date found"})
            continue
        date_from_name = os.path.splitext(filename)[0].split("_")[1]
        if note["trade_date"] != date_from_name:
            date_mismatches.append({
                "file": filename, "filename_date": date_from_name, "note_date": note["trade_date"],
            })
        notes[note["trade_date"]] = note

    ledger_df = dhan_parser.parse_ledger(Path(ledger_paths[0]).read_bytes())
    ledger_table = dhan_reconcile.ledger_match_table(notes, ledger_df)
    continuity = dhan_reconcile.bfcf_continuity_table(notes)
    new_positions = dhan_reconcile.new_position_table(notes)
    closed_positions = dhan_reconcile.closed_position_table(notes)
    summary = dhan_reconcile.summarize(
        ledger_table, continuity, new_positions, closed_positions
    )

    ledger_bad = ledger_table[ledger_table["status"] != "OK"] if len(ledger_table) else ledger_table
    breaks = continuity[continuity["status"] != "OK"] if len(continuity) else continuity
    new_bad = new_positions[new_positions["status"] != "OK - matched to order"] \
        if len(new_positions) else new_positions
    closed_bad = closed_positions[closed_positions["status"] != "OK - matched to order"] \
        if len(closed_positions) else closed_positions

    print(f"Notes parsed: {len(notes)} (failed {len(failures)}, filename-date mismatches {len(date_mismatches)})")
    print(f"Ledger days checked: {summary['days_checked']} — mismatches {summary['ledger_mismatches']}")
    print(f"BF/CF links: {summary['continuity_links_checked']} — breaks {summary['continuity_breaks']}")
    print(f"New positions: {summary['new_positions']} — unmatched {summary['new_positions_unmatched']}")
    print(f"Closed positions: {summary['closed_positions']} — unmatched {summary['closed_positions_unmatched']}")
    for f in failures[:10]:
        print(f"  FAIL {f['file']}: {f['error']}")

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "archive_root": args.root,
        "ledger_file": os.path.basename(ledger_paths[0]),
        "notes_found": len(notes_paths),
        "notes_parsed": len(notes),
        "failures": failures,
        "filename_date_mismatches": date_mismatches,
        "range_start": min(notes) if notes else None,
        "range_end": max(notes) if notes else None,
        "summary": summary,
        "ledger_mismatches": ledger_bad.to_dict("records") if len(ledger_bad) else [],
        "continuity_breaks": breaks.to_dict("records") if len(breaks) else [],
        "new_positions_unmatched": new_bad.to_dict("records") if len(new_bad) else [],
        "closed_positions_unmatched": closed_bad.to_dict("records") if len(closed_bad) else [],
    }

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "dhan_recon.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )

    lines = [
        "# Dhan contract note reconciliation",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/dhan_bulk_recon.py`.",
        "",
        f"- Notes found: **{len(notes_paths)}**",
        f"- Notes parsed: **{len(notes)}**",
        f"- Parse failures: **{len(failures)}**",
        f"- Filename/date mismatches: **{len(date_mismatches)}**",
        f"- Downloaded range: **{result['range_start']} → {result['range_end']}**",
        f"- Ledger: `{result['ledger_file']}`",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Ledger day totals | {summary['days_checked']} days, **{summary['ledger_mismatches']} mismatch(es)** |",
        f"| BF/CF price continuity | {summary['continuity_links_checked']} links, **{summary['continuity_breaks']} break(s)** |",
        f"| New F&O positions matched to an order | {summary['new_positions']} positions, "
        f"**{summary['new_positions_unmatched']} unmatched** |",
        f"| Closed F&O positions matched to an order | {summary['closed_positions']} positions, "
        f"**{summary['closed_positions_unmatched']} unmatched** |",
        "",
    ]
    if failures:
        lines += ["## Parse failures", ""] + [
            f"- `{f['file']}` — {f['error']}" for f in failures
        ] + [""]
    if date_mismatches:
        lines += ["## Filename/date mismatches", ""] + [
            f"- `{m['file']}` — filename says {m['filename_date']}, note says {m['note_date']}"
            for m in date_mismatches
        ] + [""]
    if len(ledger_bad):
        lines += ["## Ledger mismatches", "", "```", ledger_bad.to_string(index=False), "```", ""]
    if len(breaks):
        lines += ["## BF/CF breaks", "", "```", breaks.to_string(index=False), "```", ""]
    if not any((failures, date_mismatches, len(ledger_bad), len(breaks), len(new_bad), len(closed_bad))):
        lines += ["**Everything checked matched — no gaps, breaks or unmatched positions.**", ""]

    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "dhan_recon_status.md").write_text("\n".join(lines), encoding="utf-8")
    print("Wrote data/dhan_recon.json and docs/dhan_recon_status.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
