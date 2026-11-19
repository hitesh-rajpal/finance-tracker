"""Bulk upload local contract-note archive to Supabase Storage.

    python scripts/bulk_upload_archive.py
    python scripts/bulk_upload_archive.py --broker 5paisa
    python scripts/bulk_upload_archive.py --dry-run

Uploads every PDF in the configured archive folders (config/note_archive.py),
records each file in the `upload_batches` table with its Storage path, and
prints a summary. Re-running is safe — already-uploaded files are skipped
(based on the Storage path already being in the DB).

The Note Coverage tab already shows what's on disk vs missing; this script
makes those same files available in the app for parsing/reconciliation.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import note_archive  # noqa: E402
from core import file_storage  # noqa: E402
from core.storage import (  # noqa: E402
    init_db, get_conn, save_upload_batch, get_upload_batches,
)

BROKER_FOLDER_MAP = {
    "Dhan": "contract_note",
    "5paisa_huf": "contract_note",
    "5paisa_ind": "contract_note",
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


def _already_uploaded_paths() -> set[str]:
    """Storage paths already recorded in upload_batches for contract notes."""
    batches = get_upload_batches()
    paths = set()
    for b in batches:
        p = b.get("file_storage_path")
        if p and p.startswith("contract_notes/"):
            paths.add(p)
    return paths


def _find_local_files(broker: str, root: str) -> list[dict]:
    """Returns list of {local_path, filename, kind, broker, date} for upload."""
    cfg = note_archive.BROKERS[broker]
    pattern = os.path.join(root, cfg["notes_glob"])
    files = []
    for path in glob.glob(pattern, recursive=True):
        name = os.path.basename(path)
        # Determine kind from filename prefix
        kind = None
        for prefix, k in cfg["note_prefixes"].items():
            if name.startswith(prefix):
                kind = k
                break
        if not kind:
            continue
        # Extract date from filename
        import re
        m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
        if not m:
            continue
        files.append({
            "local_path": path,
            "filename": name,
            "kind": kind,
            "broker": broker,
            "date": m.group(1),
        })
    return sorted(files, key=lambda f: (f["date"], f["kind"], f["filename"]))


def _upload_one(cfg: dict, local_path: str, storage_path: str) -> tuple[bool, str]:
    with open(local_path, "rb") as f:
        data = f.read()
    ok, msg = file_storage.upload_file(
        cfg["url"], cfg["service_role_key"], storage_path, data,
        "application/pdf",
    )
    return ok, msg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=note_archive.ARCHIVE_ROOT)
    parser.add_argument("--broker", action="append", choices=sorted(note_archive.BROKERS),
                        help="Broker to upload (repeatable; default: both)")
    parser.add_argument("--dry-run", action="store_true",
                        help="List files that would be uploaded, don't actually upload")
    args = parser.parse_args()

    init_db()
    storage_cfg = _storage_cfg()
    file_storage.ensure_bucket(storage_cfg["url"], storage_cfg["service_role_key"])

    already = _already_uploaded_paths()
    print(f"Already in DB: {len(already)} files")

    brokers = args.broker or list(note_archive.BROKERS)
    total_uploaded = 0
    total_skipped = 0
    total_failed = 0
    errors = []

    for broker in brokers:
        print(f"\n=== {broker} ===")
        local_files = _find_local_files(broker, args.root)
        print(f"Local files found: {len(local_files)}")

        # Group by date for batching
        from collections import defaultdict
        by_date = defaultdict(list)
        for f in local_files:
            by_date[f["date"]].append(f)

        for date_str in sorted(by_date):
            day_files = by_date[date_str]
            batch_id = uuid.uuid4().hex[:16]
            batch_path = f"contract_notes/{broker}/{date_str}/"

            # Check which files in this batch are already uploaded
            to_upload = []
            skipped = 0
            for f in day_files:
                storage_path = batch_path + f["filename"]
                if storage_path in already:
                    skipped += 1
                    continue
                to_upload.append((f, storage_path))

            if not to_upload:
                print(f"  {date_str}: {len(day_files)} files, all already uploaded")
                total_skipped += skipped
                continue

            if args.dry_run:
                print(f"  {date_str}: would upload {len(to_upload)} files")
                for f, _ in to_upload:
                    print(f"    {f['filename']} ({f['kind']})")
                total_uploaded += len(to_upload)
                continue

            # Upload files
            uploaded_paths = []
            for f, storage_path in to_upload:
                ok, msg = _upload_one(storage_cfg, f["local_path"], storage_path)
                if ok:
                    uploaded_paths.append(storage_path)
                else:
                    total_failed += 1
                    errors.append(f"{f['filename']}: {msg}")

            if uploaded_paths:
                # Record the batch with the first file's path as representative
                save_upload_batch({
                    "id": batch_id,
                    "source_file": f"{broker} contract notes {date_str}",
                    "source_type": f"contract_note_archive:{broker}",
                    "parsed_count": 0,
                    "saved_count": 0,
                    "skipped_count": 0,
                    "file_storage_path": batch_path,  # folder path
                })
                total_uploaded += len(uploaded_paths)
                print(f"  {date_str}: uploaded {len(uploaded_paths)} files, skipped {skipped}")
            else:
                print(f"  {date_str}: ALL FAILED ({len(to_upload)} files)")
                total_failed += len(to_upload)

            total_skipped += skipped

    print(f"\n=== Summary ===")
    print(f"Uploaded: {total_uploaded}")
    print(f"Skipped (already in DB): {total_skipped}")
    print(f"Failed: {total_failed}")
    if errors:
        print("Errors:")
        for e in errors[:20]:
            print(f"  {e}")

    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())