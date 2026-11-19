"""Upload source documents (bank statements, CAS, broker ledgers) to the
private Supabase Storage bucket.

    python scripts/bulk_upload_documents.py --dry-run
    python scripts/bulk_upload_documents.py
    python scripts/bulk_upload_documents.py --kind bank_statement
    python scripts/bulk_upload_documents.py --kind cas

The contract-note archiver (`bulk_upload_archive.py`) only handles contract
notes.  The reconciliation also needs the original IndusInd statements, the
CDSL/NSDL CAS PDFs and the 5paisa ledger, so this script uploads those to the
same bucket under stable prefixes:

    bank_statements/IndusInd/...
    cas/CDSL/...
    ledgers/5paisa/...

Re-running is safe: files whose exact Storage path is already recorded in
`upload_batches` are skipped, and uploads use x-upsert.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import note_archive  # noqa: E402
from core import file_storage  # noqa: E402
from core.storage import init_db, get_upload_batches, save_upload_batch  # noqa: E402

ARCHIVE_ROOT = Path(note_archive.ARCHIVE_ROOT)

SOURCES = {
    "bank_statement": {
        "dest": "bank_statements/IndusInd",
        "root": ARCHIVE_ROOT / "Bank_Statement" / "IndusInd",
        "glob": "**/*.pdf",
        "content_type": "application/pdf",
    },
    "cas": {
        "dest": "cas/CDSL",
        "root": ARCHIVE_ROOT / "Email_Downloads" / "CDSL_NSDL_CAS" / "CDSL",
        "glob": "**/*HUF*.pdf",
        "content_type": "application/pdf",
    },
    "ledger": {
        "dest": "ledgers/5paisa",
        "root": ARCHIVE_ROOT,
        "glob": "5paisa_*/Ledger/*.xlsx",
        "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
}


def _storage_cfg():
    import streamlit as st
    try:
        return {
            "url": st.secrets["supabase_storage"]["url"],
            "service_role_key": st.secrets["supabase_storage"]["service_role_key"],
        }
    except Exception:
        import tomllib
        secrets_path = ROOT / ".streamlit" / "secrets.toml"
        if not secrets_path.exists():
            raise RuntimeError("Supabase storage settings not found in .streamlit/secrets.toml")
        with secrets_path.open("rb") as f:
            s = tomllib.load(f)
        return {
            "url": s["supabase_storage"]["url"],
            "service_role_key": s["supabase_storage"]["service_role_key"],
        }


def _safe_component(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")


def _safe_path(rel: str) -> str:
    parts = [_safe_component(p) for p in Path(rel).parts]
    return "/".join(p for p in parts if p)


def _collect(kind: str, cfg: dict) -> list[dict]:
    root = cfg["root"]
    if not root.exists():
        return []
    files = []
    for path in glob.glob(str(root / cfg["glob"]), recursive=True):
        rel = os.path.relpath(path, root)
        storage_path = f"{cfg['dest']}/{_safe_path(rel)}"
        files.append({
            "local_path": path,
            "filename": os.path.basename(path),
            "storage_path": storage_path,
            "kind": kind,
        })
    return sorted(files, key=lambda f: f["storage_path"])


def _recorded_paths() -> set[str]:
    return {b.get("file_storage_path") for b in get_upload_batches() if b.get("file_storage_path")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", action="append", choices=sorted(SOURCES),
                        help="Document kind to upload (repeatable; default: all)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    init_db()
    storage = _storage_cfg()
    file_storage.ensure_bucket(storage["url"], storage["service_role_key"])

    recorded = _recorded_paths()
    kinds = args.kind or list(SOURCES)
    print(f"Already recorded paths: {len(recorded)}")

    total_uploaded = total_skipped = total_failed = 0
    errors = []
    for kind in kinds:
        cfg = SOURCES[kind]
        files = _collect(kind, cfg)
        print(f"\n=== {kind} ({cfg['dest']}) — {len(files)} local file(s) ===")
        for f in files:
            if f["storage_path"] in recorded:
                total_skipped += 1
                continue
            if args.dry_run:
                print(f"  would upload {f['storage_path']}")
                total_uploaded += 1
                continue
            with open(f["local_path"], "rb") as fh:
                data = fh.read()
            ok, msg = file_storage.upload_file(
                storage["url"], storage["service_role_key"],
                f["storage_path"], data, cfg["content_type"],
            )
            if not ok:
                total_failed += 1
                errors.append(f"{f['filename']}: {msg}")
                print(f"  FAILED {f['filename']}: {msg}")
                continue
            save_upload_batch({
                "id": uuid.uuid4().hex[:16],
                "source_file": f["filename"],
                "source_type": f"document_archive:{kind}",
                "parsed_count": 0,
                "saved_count": 0,
                "skipped_count": 0,
                "file_storage_path": f["storage_path"],
            })
            recorded.add(f["storage_path"])
            total_uploaded += 1
            if total_uploaded % 10 == 0:
                print(f"  uploaded {total_uploaded} so far...")

    print("\n=== Summary ===")
    print(f"Uploaded: {total_uploaded}")
    print(f"Skipped (already recorded): {total_skipped}")
    print(f"Failed: {total_failed}")
    for e in errors[:20]:
        print("  ERR", e)
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
