"""Keeps the original uploaded files (statement/contract-note PDFs, SMS
export text) in a private Supabase Storage bucket, so they can be viewed or
re-downloaded later — not just the transactions parsed out of them. Talks to
Storage's REST API directly with the service_role key (full read/write/
delete access, bypassing bucket policies) since this is a single-user
backend app doing its own file management, not a client needing per-user
RLS-scoped access.
"""
import requests

TIMEOUT = 20
BUCKET = "uploads"


def _headers(service_key: str) -> dict:
    return {"Authorization": f"Bearer {service_key}", "apikey": service_key}


def ensure_bucket(url: str, service_key: str):
    """Creates the private 'uploads' bucket if it doesn't already exist. Safe
    to call every time — a 409 (already exists) is not an error here."""
    try:
        requests.post(
            f"{url}/storage/v1/bucket",
            headers={**_headers(service_key), "Content-Type": "application/json"},
            json={"id": BUCKET, "name": BUCKET, "public": False},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        pass  # non-fatal — uploads will just fail loudly later if the bucket truly isn't there


def upload_file(url: str, service_key: str, path: str, data: bytes, content_type: str) -> tuple[bool, str]:
    try:
        resp = requests.post(
            f"{url}/storage/v1/object/{BUCKET}/{path}",
            headers={**_headers(service_key), "Content-Type": content_type},
            data=data,
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, str(e)
    if resp.status_code in (200, 201):
        return True, path
    return False, resp.text


def delete_file(url: str, service_key: str, path: str) -> bool:
    if not path:
        return True
    try:
        resp = requests.delete(
            f"{url}/storage/v1/object/{BUCKET}/{path}",
            headers=_headers(service_key),
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return False
    return resp.status_code in (200, 204)


def signed_url(url: str, service_key: str, path: str, expires_in: int = 3600) -> str | None:
    """A temporary (expires_in seconds) download link for a private file —
    since the bucket isn't public, this is the only way to fetch a file back."""
    if not path:
        return None
    try:
        resp = requests.post(
            f"{url}/storage/v1/object/sign/{BUCKET}/{path}",
            headers={**_headers(service_key), "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
            timeout=TIMEOUT,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    signed_path = resp.json().get("signedURL")
    return f"{url}/storage/v1{signed_path}" if signed_path else None
