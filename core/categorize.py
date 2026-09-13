import re
from config.categories import suggest_category, suggest_is_office, DEFAULT_CATEGORY
from core.storage import get_overrides

_WS_RE = re.compile(r"\s+")


def normalize_key(description: str) -> str:
    """Collapse a description down to a stable merchant-ish key so that
    'SWIGGY*ORDER 92831' and 'SWIGGY*ORDER 77120' share one override."""
    text = (description or "").lower()
    text = re.sub(r"[0-9]{3,}", "", text)          # drop long numbers (refs, order ids)
    text = re.sub(r"[^a-z@. ]", " ", text)
    text = _WS_RE.sub(" ", text).strip()
    return " ".join(text.split()[:4])  # first few tokens is usually the merchant


def categorize(description: str) -> tuple[str, bool]:
    """Returns (category, is_office), preferring a user-saved override."""
    key = normalize_key(description)
    overrides = get_overrides()
    if key in overrides:
        return overrides[key]
    category = suggest_category(description)
    is_office = suggest_is_office(category)
    return category, is_office


def is_office_for_category(category: str) -> bool:
    """For a category that's already known (e.g. set directly from a bank
    statement's own category column, or a freeform SMS tag) rather than
    guessed from the description — still needs an office/personal default."""
    return suggest_is_office(category)


def apply_correction(description: str, category: str, is_office: bool):
    from core.storage import save_override
    key = normalize_key(description)
    save_override(key, category, is_office)
