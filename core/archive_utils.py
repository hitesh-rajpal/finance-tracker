"""Returns list of {local_path, filename, kind, broker, date} for upload."""
import glob, re, os
from config import note_archive


def find_archive_files(broker: str, root_path: str) -> list[dict]:
    """Returns list of {local_path, filename, kind, broker, date} for upload."""
    cfg = note_archive.BROKERS[broker]
    pattern = os.path.join(root_path, cfg["notes_glob"])
    files = []
    for path in glob.glob(pattern, recursive=True):
        name = os.path.basename(path)
        kind = None
        for prefix, k in cfg["note_prefixes"].items():
            if name.startswith(prefix):
                kind = k
                break
        if not kind:
            continue
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