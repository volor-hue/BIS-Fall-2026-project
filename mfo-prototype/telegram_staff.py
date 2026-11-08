# telegram_staff.py
# Staff access is an allowlist of numeric Telegram user ids.
# Empty or invalid configuration fails closed. Usernames are never consulted.
import os
import re
import secrets

STAFF_ENV = "MFO_TELEGRAM_STAFF_IDS"
_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{8}$")
_SEARCHES = {}
_SEARCH_LIMIT = 200


def parse_staff_ids(raw):
    """Return a frozenset of ids, or None when the text is not a valid list.

    An empty value is valid and means nobody is staff.
    One bad token rejects the whole list (fail closed), including any good ids.
    """
    if raw is None:
        return frozenset()
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return frozenset()
    found = []
    for part in re.split(r"[\s,;]+", text):
        if not part:
            continue
        if _ID_RE.fullmatch(part) is None:
            return None
        found.append(int(part))
    return frozenset(found)


def current_staff_ids():
    parsed = parse_staff_ids(os.environ.get(STAFF_ENV, ""))
    if parsed is None:
        return frozenset()
    return parsed


def allowlist_is_valid():
    return parse_staff_ids(os.environ.get(STAFF_ENV, "")) is not None


def is_staff(user_id):
    """True only for a numeric id present in a valid allowlist. Checked live."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    return user_id in current_staff_ids()


def remember_search(user_id, folded_query):
    """Short token so the next page does not put the name into callback_data."""
    token = secrets.token_hex(4)
    _SEARCHES[(user_id, token)] = folded_query
    if len(_SEARCHES) > _SEARCH_LIMIT:
        for key in list(_SEARCHES)[: len(_SEARCHES) - _SEARCH_LIMIT]:
            _SEARCHES.pop(key, None)
    return token


def recall_search(user_id, token):
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        return None
    return _SEARCHES.get((user_id, token))


def clear_search_memory():
    _SEARCHES.clear()


def parse_staff_callback(data):
    """fst / fsa / fss / fp callbacks. None when the payload is not ours."""
    if not isinstance(data, str) or len(data) > 64 or ":" not in data:
        return None
    parts = data.split(":")
    if any(part == "" for part in parts):
        return None
    kind = parts[0]
    if kind == "fst" and len(parts) == 2:
        client_id = _positive(parts[1])
        if client_id is None:
            return None
        return ("fst", client_id)
    if kind in {"fsa", "fss"} and len(parts) == 3:
        client_id = _positive(parts[1])
        entity_id = _positive(parts[2])
        if client_id is None or entity_id is None:
            return None
        return (kind, client_id, entity_id)
    if kind == "fp" and len(parts) == 3 and _TOKEN_RE.fullmatch(parts[1]):
        page = _positive(parts[2])
        if page is None or page > 20:
            return None
        return ("fp", parts[1], page)
    return None


def split_find_args(raw):
    """('/find Нурбек 2') -> ('Нурбек', 2). A trailing page number is optional."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return "", 1
    parts = text.split()
    page = 1
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) <= 3:
        number = int(parts[-1])
        if 1 <= number <= 20:
            page = number
            parts = parts[:-1]
    return " ".join(parts), page


def _positive(raw):
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 15:
        return None
    if raw.startswith("0"):
        return None
    value = int(raw)
    if value <= 0:
        return None
    return value
