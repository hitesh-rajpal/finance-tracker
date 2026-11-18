"""Where the downloaded broker documents live on this PC, and what counts as
a contract note for each broker.

This is intentionally a plain config file rather than something stored in the
database: the paths only make sense on the machine doing the scanning, while
the *results* of a scan (what's present, what's missing) are stored in
Supabase so the deployed Streamlit app can show them too.

Override the root without editing this file by setting the
FINANCE_TRACKER_ARCHIVE_ROOT environment variable, or by passing --root to
scripts/note_coverage.py.
"""
import os

ARCHIVE_ROOT = os.environ.get(
    "FINANCE_TRACKER_ARCHIVE_ROOT",
    r"C:\Users\hp\OneDrive\Personal\Investment\Grok\Investment",
)

# Glob patterns are relative to ARCHIVE_ROOT. Filename prefixes decide what
# kind of document a PDF is: only contract_note / commodity files count as a
# contract note for coverage purposes; SOF/SOS are margin statements that
# happen to live in the same folder.
BROKERS = {
    "Dhan": {
        "label": "Dhan — Yashima (1108852293)",
        "notes_glob": r"Email_Downloads\Dhan_*\Contract_Notes\**\*.pdf",
        "trades_glob": r"Dhan_*\Trades\Trade_History_*.csv",
        "trades_format": "dhan_csv",
        "note_prefixes": {
            "SOF_SOS_": "sof_sos",
            "ContractNote_": "contract_note",
        },
        "contract_kinds": {"contract_note"},
    },
    "5paisa": {
        "label": "5paisa — HUF (52598262)",
        "notes_glob": r"Email_Downloads\5paisa_*\Contract_Notes\**\*.pdf",
        "trades_glob": r"5paisa_*\Transactions\*.xlsx",
        "ledger_glob": r"5paisa_*\Ledger\*.xlsx",
        "trades_format": "5paisa_xlsx",
        "note_prefixes": {
            "CommodityContract_": "commodity",
            "ContractNote_": "contract_note",
        },
        "contract_kinds": {"contract_note", "commodity"},
    },
}

# Things we know are still outstanding but can't be listed date-by-date from
# the local files (e.g. because the notes are still sitting in Gmail). Shown
# as a pending note against the broker until they're downloaded and a new
# scan finds them.
KNOWN_PENDING = {
    "5paisa": (
        "706 older contract notes (before 2025-03-04) are still in Gmail and "
        "have not been downloaded yet."
    ),
}
