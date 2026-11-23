# app/core/config.py
# ------------------------------------------------------------
# ALL settings of the project in ONE place: the web app, the face check and the Telegram bot.
#
# Where a value comes from (the first one that is set wins):
#   1. the process environment         (set MFO_PORT=8002 ...)
#   2. the .env file next to run.py    (see .env.example)
#   3. the default written below
#
# A wrong value never switches a protection off: it is replaced by the default and the problem
# is remembered. check_settings() lists every problem; the web app writes them into the log at
# start (app/main.py) and run.py refuses to start when a value is unusable (port, staff ids...).
#
# Secrets (JWT secret, bot token) are never printed or logged by this module.
# ------------------------------------------------------------
import logging
import os
import re
import secrets
from datetime import date

from limits import parse_many

log = logging.getLogger("mfo.web")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_FILE = os.path.join(PROJECT_DIR, ".env")
TEMPLATES_DIR = os.path.join(PROJECT_DIR, "app", "web", "templates")

# Problems found while the settings were loaded (messages only, never secret values).
_problems = []


# ============================================================
# Reading .env (no extra libraries)
# ============================================================

def read_env_file(path):
    """Return {key: value} from a .env file without changing os.environ. Missing file -> {}."""
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                values.setdefault(key, value)  # a repeated key: the first line wins
    return values


def apply_env_file(path):
    """Copy the values of a .env file into os.environ. Existing variables always win."""
    for key, value in read_env_file(path).items():
        os.environ.setdefault(key, value)


def load_local_env():
    """Bot and operator CLI: read .env from the current folder, then from the project folder."""
    paths = []
    for folder in (os.getcwd(), PROJECT_DIR):
        path = os.path.join(folder, ".env")
        if path not in paths:
            paths.append(path)
    for path in paths:
        if os.path.isfile(path):
            apply_env_file(path)


_file_values = read_env_file(ENV_FILE)


def get(name, default=""):
    """Text value: environment variable -> .env file -> default."""
    return str(os.environ.get(name) or _file_values.get(name) or default).strip()


def _number(name, default, low, high, kind=float):
    """Number inside [low, high]; anything else -> default (and the problem is remembered)."""
    text = get(name, default)
    try:
        value = kind(text)
    except ValueError:
        value = None
    if value is None or not low <= value <= high:
        _problems.append(f"{name} must be a number from {low} to {high}, using the default {default}")
        return default
    return value


def _flag(name, default):
    return get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


# ============================================================
# Paths: database, uploads, logs
# ============================================================

# SQLite file. Relative paths are relative to the folder the process runs in;
# run.py turns it into one absolute path for both the site and the bot.
DB_FILE = os.environ.get("MFO_DB", "mfo.db")
UPLOAD_DIR = get("MFO_UPLOAD_DIR", os.path.join(PROJECT_DIR, "uploads"))
LOG_DIR = os.environ.get("MFO_LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("MFO_LOG_LEVEL", "INFO").upper()


# ============================================================
# Web server (run.py)
# ============================================================

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001  # 8000 is often already taken


def parse_port(raw):
    """Port 1..65535; empty -> DEFAULT_PORT; anything else -> None."""
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PORT
    text = str(raw).strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < 1 or value > 65535:
        return None
    return value


def parse_host(raw):
    """A host name or IP without spaces, slashes or a port; empty -> DEFAULT_HOST; bad -> None."""
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HOST
    host = str(raw).strip()
    if len(host) > 253 or any(char.isspace() for char in host):
        return None
    if "/" in host or "\\" in host or ":" in host:
        return None
    return host


# ============================================================
# Rate limits (app/core/rate_limit.py)
#   MFO_RATE_LIMIT_AUTH     POST /login, /register, /api/auth/*   (password guessing, fake accounts)
#   MFO_RATE_LIMIT_SUBMIT   loan applications, uploads, face check, cards
#   MFO_RATE_LIMIT_DEFAULT  every other endpoint
# Format "<number>/<second|minute|hour|day>", several: "5/minute;50/hour".
# ============================================================

DEFAULT_LIMITS = {
    "MFO_RATE_LIMIT_AUTH": "5/minute",
    "MFO_RATE_LIMIT_SUBMIT": "10/minute",
    "MFO_RATE_LIMIT_DEFAULT": "60/minute",
}


def read_limit(name, file_values=None, problems=None):
    """One rate limit. A broken value falls back to the default: a typo must not switch protection off."""
    if file_values is None:
        file_values = _file_values
    value = (os.environ.get(name) or file_values.get(name) or DEFAULT_LIMITS[name]).strip()
    try:
        parse_many(value)  # check the format now
    except ValueError:
        message = f"bad rate limit {name}, using the default {DEFAULT_LIMITS[name]}"
        if problems is None:
            log.warning(message)
        else:
            problems.append(message)
        return DEFAULT_LIMITS[name]
    return value


AUTH_LIMIT = read_limit("MFO_RATE_LIMIT_AUTH", problems=_problems)
SUBMIT_LIMIT = read_limit("MFO_RATE_LIMIT_SUBMIT", problems=_problems)
DEFAULT_LIMIT = read_limit("MFO_RATE_LIMIT_DEFAULT", problems=_problems)


# ============================================================
# JWT login (app/core/security.py)
#   MFO_JWT_SECRET          REQUIRED on a real server, at least 32 characters.
#       Empty -> a random secret for this run: everybody is logged out on restart.
#       Generate: python -c "import secrets; print(secrets.token_urlsafe(48))"
#   MFO_JWT_EXPIRE_MINUTES  token lifetime, 5..10080 (default 60)
# ============================================================

JWT_MIN_SECRET_LENGTH = 32


def _jwt_secret():
    secret = get("MFO_JWT_SECRET")
    if len(secret) >= JWT_MIN_SECRET_LENGTH:
        return secret
    if secret:
        _problems.append("MFO_JWT_SECRET is shorter than 32 characters, using a random secret instead")
    else:
        _problems.append("MFO_JWT_SECRET is not set: using a random secret, users are logged out on restart")
    return secrets.token_urlsafe(48)


JWT_SECRET = _jwt_secret().encode("utf-8")
JWT_EXPIRE_MINUTES = _number("MFO_JWT_EXPIRE_MINUTES", 60, 5, 10080, int)


# ============================================================
# Face check (app/face_auth/). Thresholds are explained in docs/FACE_AUTH_RU.md.
# ============================================================

class FaceSettings:
    # ---------- models ----------
    MODEL_DIR = get("FACE_MODEL_DIR", os.path.join(PROJECT_DIR, "models"))
    DETECT_SCORE = 0.8   # how sure YuNet must be that a box is a face
    # Big photos are shrunk to this size first: faster, and YuNet is MORE sure on a medium-size face.
    MAX_SIDE = 640

    # ---------- step 1: "is it the same person as at the enrollment?" ----------
    # Cosine similarity threshold recommended by the SFace authors (>= means same person).
    COSINE_THRESHOLD = _number("FACE_THRESHOLD", 0.363, 0.0, 1.0)

    # ---------- step 2: liveness (head turn) ----------
    # FACE_LIVENESS=0 turns the head-turn step off (the token is issued right after step 1).
    LIVENESS_ENABLED = _flag("FACE_LIVENESS", True)
    # How long the client has to finish the head turns after step 1 (seconds).
    CHALLENGE_SECONDS = _number("FACE_CHALLENGE_SECONDS", 120, 20, 600, int)
    # "Yaw" = how far the nose is shifted sideways, in eye distances (see liveness.py).
    #   straight face ~ 0.0, head turned ~25 degrees ~ 0.2, ~45 degrees ~ 0.45
    TURN_MIN = _number("FACE_TURN_MIN", 0.20, 0.05, 1.0)
    # The "look straight" frame must be at most this far from 0.
    FRONTAL_MAX = _number("FACE_FRONTAL_MAX", 0.15, 0.02, 0.5)
    # Every turned frame must still be the same person (a turned face looks less like the straight one).
    TURN_SAME_PERSON = _number("FACE_TURN_SAME_PERSON", 0.30, 0.0, 1.0)
    # At least this many frames with a face are needed in each step.
    MIN_FRAMES = _number("FACE_TURN_MIN_FRAMES", 2, 1, 20, int)
    MAX_FRAMES = 12      # frames per step the server accepts (the browser sends 8)
    # Set to 1 only if YOUR camera driver already mirrors the picture (left and right swapped).
    CAMERA_MIRRORED = _flag("FACE_CAMERA_MIRRORED", False)

    # ---------- biometric enrollment at registration ----------
    # 1 = a client can register only after "Пройти биометрию" (web form and /api/auth/register).
    # The automatic tests set 0, so registration tests do not need a camera.
    ENROLL_REQUIRED = _flag("FACE_ENROLL_REQUIRED", True)
    # After a successful enrollment the registration form must be sent within this many minutes.
    ENROLL_TOKEN_MINUTES = _number("FACE_ENROLL_TOKEN_MINUTES", 15, 1, 60, int)

    # ---------- the one-time token for the loan application ----------
    TOKEN_MINUTES = _number("FACE_TOKEN_MINUTES", 5, 1, 60, int)

    # ---------- uploads ----------
    MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024   # one webcam snapshot
    MAX_FRAME_BYTES = 2 * 1024 * 1024      # one liveness frame


face = FaceSettings()


# ============================================================
# Telegram bot (bot/). Read at the moment they are needed, because the bot loads
# .env into the environment at its start (load_local_env).
#   TELEGRAM_BOT_TOKEN             from @BotFather, never printed
#   MFO_TELEGRAM_STAFF_IDS         numeric Telegram ids of employees, comma separated ("" = none)
#   MFO_TELEGRAM_CODE_TTL_SECONDS  lifetime of a one-time pairing code, 30..86400 (default 600)
# ============================================================

TELEGRAM_STAFF_ENV = "MFO_TELEGRAM_STAFF_IDS"
TELEGRAM_CODE_TTL_DEFAULT = 600
TELEGRAM_CODE_TTL_MIN = 30
TELEGRAM_CODE_TTL_MAX = 86400


def telegram_token_raw():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")


def telegram_staff_ids_raw():
    return os.environ.get(TELEGRAM_STAFF_ENV, "")


_STAFF_ID_RE = re.compile(r"^[1-9][0-9]{0,15}$")


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
        if _STAFF_ID_RE.fullmatch(part) is None:
            return None
        found.append(int(part))
    return frozenset(found)


def telegram_code_ttl_seconds():
    raw = os.environ.get("MFO_TELEGRAM_CODE_TTL_SECONDS", "").strip()
    if not raw:
        return TELEGRAM_CODE_TTL_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        return TELEGRAM_CODE_TTL_DEFAULT
    if value < TELEGRAM_CODE_TTL_MIN or value > TELEGRAM_CODE_TTL_MAX:
        return TELEGRAM_CODE_TTL_DEFAULT
    return value


# ============================================================
# Repayment rules (app/repayment_rules/)
#   MFO_TODAY=YYYY-MM-DD   moves the "current date" forward for the defense demo (overdue, penalty).
#                          Empty = the real date. Read at the moment of use.
# ============================================================

def demo_today():
    """The date from MFO_TODAY, or None (= use the real date). A wrong value is ignored."""
    value = get("MFO_TODAY")
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ============================================================
# Check at start
# ============================================================

def check_settings():
    """All problems with the current settings, as short messages without secret values.
    Values that were replaced by defaults while loading + values read at run time."""
    problems = list(_problems)
    if parse_staff_ids(telegram_staff_ids_raw()) is None:
        problems.append(f"{TELEGRAM_STAFF_ENV} is invalid: numeric Telegram ids separated by commas are needed")
    if parse_host(get("MFO_HOST")) is None:
        problems.append("MFO_HOST is invalid, example: 127.0.0.1")
    if parse_port(get("MFO_PORT")) is None:
        problems.append("MFO_PORT must be a number from 1 to 65535")
    if get("MFO_TODAY") and demo_today() is None:
        problems.append("MFO_TODAY must be a date YYYY-MM-DD, using the real date")
    return problems


def log_settings_problems():
    """Write every settings problem into the log as a WARNING (called once when the app starts)."""
    for problem in check_settings():
        log.warning("settings: %s", problem)
