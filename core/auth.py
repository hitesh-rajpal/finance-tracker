"""Real email/password login via Supabase Auth (GoTrue's REST API), used
instead of a single hardcoded shared password. Talks to Auth directly over
HTTP rather than pulling in the full supabase-py client, since this app only
needs sign-in and password-recovery, not the rest of that SDK.
"""
import requests

TIMEOUT = 10


def _token_error(resp) -> str:
    try:
        detail = resp.json().get("error_description") or resp.json().get("msg") or resp.text
    except ValueError:
        detail = resp.text
    return detail or "Incorrect email or password."


def sign_in(url: str, anon_key: str, email: str, password: str) -> tuple[bool, dict | str]:
    """Returns (success, session_dict) on success — session_dict has
    access_token/refresh_token/user, the refresh_token being what "Remember
    me" persists — or (False, error_message) on failure."""
    try:
        resp = requests.post(
            f"{url}/auth/v1/token?grant_type=password",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email, "password": password},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Could not reach the login service: {e}"

    if resp.status_code == 200:
        return True, resp.json()
    return False, _token_error(resp)


def refresh_session(url: str, anon_key: str, refresh_token: str) -> tuple[bool, dict | str]:
    """Silently re-authenticates using a persisted refresh_token (Remember
    me), instead of asking for email/password again. Returns the same shape
    as sign_in — a fresh refresh_token comes back too, since Supabase
    rotates them on use, so the caller should re-save it."""
    try:
        resp = requests.post(
            f"{url}/auth/v1/token?grant_type=refresh_token",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"refresh_token": refresh_token},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Could not reach the login service: {e}"

    if resp.status_code == 200:
        return True, resp.json()
    return False, _token_error(resp)


def send_password_reset(url: str, anon_key: str, email: str) -> tuple[bool, str]:
    """Triggers Supabase's password-recovery email. Always returns success
    from Supabase's side even for an unknown email (it doesn't leak which
    emails are registered), so the UI should show a generic "check your
    inbox" message regardless."""
    try:
        resp = requests.post(
            f"{url}/auth/v1/recover",
            headers={"apikey": anon_key, "Content-Type": "application/json"},
            json={"email": email},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Could not reach the login service: {e}"
    return resp.status_code == 200, "" if resp.status_code == 200 else resp.text
