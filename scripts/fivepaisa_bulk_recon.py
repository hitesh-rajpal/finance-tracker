"""Reconcile the whole local 5paisa contract-note archive against the ledger.

    python scripts/fivepaisa_bulk_recon.py
    python scripts/fivepaisa_bulk_recon.py --password "<PAN>"

Checks (see core/fivepaisa_reconcile.py for the details):

  * every note parses and its date matches the filename,
  * each day's note total equals that day's ledger bill movement
    (F&O + cash + commodities notes summed, since a day can have both),
  * B/F opening prices chain from the previous note day's closing price,
  * new positions are backed by a same-day trade, and closed ones are either
    traded out or have reached expiry.

Writes data/fivepaisa_recon.json and docs/fivepaisa_recon_status.md.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import note_archive  # noqa: E402
from core import fivepaisa_reconcile as recon  # noqa: E402
from parsers import fivepaisa_parser  # noqa: E402

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


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

    cfg = note_archive.BROKERS["5paisa"]
    note_paths = _find(args.root, cfg["notes_glob"])
    ledger_paths = _find(args.root, cfg["ledger_glob"])
    if not note_paths:
        print("No 5paisa contract notes found.")
        return 1
    if not ledger_paths:
        print("No 5paisa ledger .xlsx found.")
        return 1

    password = _pick_password(note_paths, args.password + _saved_passwords())
    if not password:
        print("Could not open the notes — supply the PDF password with --password.")
        return 1

    notes: dict[str, list[dict]] = {}
    failures = []
    date_mismatches = []
    for path in note_paths:
        filename = os.path.basename(path)
        try:
            note = fivepaisa_parser.parse_contract_note(Path(path).read_bytes(), password=password)
        except Exception as exc:
            failures.append({"file": filename, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not note["trade_date"]:
            failures.append({"file": filename, "error": "no trade date found"})
            continue
        m = DATE_RE.search(filename)
        if m and m.group(1) != note["trade_date"]:
            date_mismatches.append({
                "file": filename, "filename_date": m.group(1), "note_date": note["trade_date"],
            })
        note["file"] = filename
        notes.setdefault(note["trade_date"], []).append(note)

    raw_note_count = sum(len(v) for v in notes.values())
    notes = recon.dedupe_notes(notes)
    deduped_count = sum(len(v) for v in notes.values())

    ledger_df = fivepaisa_parser.parse_ledger(Path(ledger_paths[0]).read_bytes())
    ledger_table = recon.ledger_match_table(notes, ledger_df)
    continuity = recon.bfcf_continuity_table(notes)
    closed_positions = recon.closed_position_table(notes)
    summary = recon.summarize(ledger_table, continuity, closed_positions)

    def _bad(df: pd.DataFrame, statuses) -> pd.DataFrame:
        return df[df["status"].isin(statuses)] if len(df) else df

    ledger_bad = _bad(ledger_table, {"MISMATCH", "SIGN ANOMALY", "NO LEDGER BILL"})
    breaks = _bad(continuity, {"ADJUSTMENT/ERROR"})
    closed_bad = _bad(closed_positions, {"NO CLOSING TRADE FOUND"})

    print(f"Notes parsed: {raw_note_count} "
          f"({len(notes)} dates; failed {len(failures)}; filename-date mismatches {len(date_mismatches)})")
    if raw_note_count != deduped_count:
        print(f"Duplicate copies of the same note ignored: {raw_note_count - deduped_count}")
    print(f"Ledger days checked: {summary['days_checked']} — mismatches {summary['ledger_mismatches']}, "
          f"sign anomalies {summary['sign_anomalies']}, no ledger bill {summary['ledger_missing']}")
    print(f"BF/CF links: {summary['continuity_links_checked']} — breaks {summary['continuity_breaks']}")
    print(f"Closed positions: {summary['closed_positions']} — unmatched "
          f"{summary['closed_positions_unmatched']} (expired {summary['closed_positions_expired']})")
    for f in failures[:10]:
        print(f"  FAIL {f['file']}: {f['error']}")

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "archive_root": args.root,
        "ledger_file": os.path.basename(ledger_paths[0]),
        "notes_found": len(note_paths),
        "notes_parsed": sum(len(v) for v in notes.values()),
        "duplicate_notes_ignored": raw_note_count - deduped_count,
        "failures": failures,
        "filename_date_mismatches": date_mismatches,
        "range_start": min(notes) if notes else None,
        "range_end": max(notes) if notes else None,
        "summary": summary,
        "ledger_mismatches": ledger_bad.to_dict("records") if len(ledger_bad) else [],
        "continuity_breaks": breaks.to_dict("records") if len(breaks) else [],
        "unexplained_closures": closed_bad.to_dict("records") if len(closed_bad) else [],
    }

    data_dir = ROOT / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "fivepaisa_recon.json").write_text(
        json.dumps(result, indent=2, default=str), encoding="utf-8"
    )

    def _table(df: pd.DataFrame) -> str:
        return "```\n" + df.to_string(index=False) + "\n```"

    lines = [
        "# 5paisa contract note reconciliation",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} by `scripts/fivepaisa_bulk_recon.py`.",
        "",
        f"- Notes found: **{len(note_paths)}**",
        f"- Notes parsed: **{result['notes_parsed']}** over **{len(notes)}** dates",
        f"- Duplicate copies of the same note ignored: **{raw_note_count - deduped_count}**",
        f"- Parse failures: **{len(failures)}**",
        f"- Filename/date mismatches: **{len(date_mismatches)}**",
        f"- Downloaded range: **{result['range_start']} → {result['range_end']}**",
        f"- Ledger: `{result['ledger_file']}`",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---|",
        f"| Ledger day totals | {summary['days_checked']} days, **{summary['ledger_mismatches']} mismatch(es)**, "
        f"**{summary['sign_anomalies']} sign anomaly(ies)**, {summary['ledger_missing']} day(s) with no bill |",
        f"| B/F continuity | {summary['continuity_links_checked']} links, **{summary['continuity_breaks']} break(s)** |",
        f"| Closed positions explained | {summary['closed_positions']} positions, "
        f"**{summary['closed_positions_unmatched']} unexplained**, {summary['closed_positions_expired']} expired |",
        "",
    ]
    if failures:
        lines += ["## Parse failures", ""] + [f"- `{f['file']}` — {f['error']}" for f in failures] + [""]
    if date_mismatches:
        lines += ["## Filename/date mismatches", ""] + [
            f"- `{m['file']}` — filename says {m['filename_date']}, note says {m['note_date']}"
            for m in date_mismatches
        ] + [""]
    if len(ledger_bad):
        lines += ["## Ledger mismatches and sign anomalies", ""]
        lines.append(
            "A **sign anomaly** is an exact-magnitude, opposite-sign difference. "
            "`note_obligation` is the note's own Payin/Payout Obligation line: where "
            "it matches the ledger, the note's Net Amount line is the one with the "
            "wrong direction, so the amount itself is fine and only the note "
            "presentation is inconsistent."
        )
        lines += ["", _table(ledger_bad), ""]
    if len(breaks):
        lines += ["## B/F continuity breaks", "",
                  "Either an exchange price adjustment (most often an extraordinary "
                  "dividend, per SEBI circular SEBI/HO/MRD2/MRD2_DCAP/P/CIR/2022/90) "
                  "or a position that was closed and re-opened.", "", _table(breaks), ""]
    if len(closed_bad):
        lines += ["## Unexplained position closures", "",
                  "A carried position that disappeared without a closing trade on "
                  "either note and before its expiry.", "", _table(closed_bad), ""]
    if not any((failures, date_mismatches, len(ledger_bad), len(breaks), len(closed_bad))):
        lines += ["**Everything checked matched — no gaps, breaks or unexplained closures.**", ""]

    docs_dir = ROOT / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "fivepaisa_recon_status.md").write_text("\n".join(lines), encoding="utf-8")
    print("Wrote data/fivepaisa_recon.json and docs/fivepaisa_recon_status.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
