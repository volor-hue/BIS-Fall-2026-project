# rate_limit.py
# ------------------------------------------------------------
# Protection from request spam (rate limiting) with the "slowapi" library.
# Every visitor is identified by IP address. If one IP sends too many requests to one endpoint,
# the server answers 429 Too Many Requests (main.py writes a WARNING to the log).
#
# Limits (defaults below; change them in .env or in the environment, see .env.example):
#   MFO_RATE_LIMIT_AUTH=5/minute      POST /login and POST /register (password guessing, fake accounts)
#   MFO_RATE_LIMIT_SUBMIT=10/minute   loan applications and document uploads
#   MFO_RATE_LIMIT_DEFAULT=60/minute  every other endpoint: API and pages
# Format: "<number>/<second|minute|hour|day>", for example "100/hour".
# Several limits at once: "5/minute;50/hour".
#
# Every endpoint has its OWN counter per IP: /api/clients/1 and /api/clients/2 share one counter
# (same endpoint), but /api/loans counts separately.
#
# Counters live in the server's memory: they reset on restart, and with several uvicorn workers
# every worker counts separately. A real server would keep them in shared storage (Redis).
# ------------------------------------------------------------
import os
import time

from limits import parse_many
from slowapi import Limiter
from slowapi.util import get_remote_address

import telegram_env
from log_config import clean, log, setup_logging

setup_logging()  # a warning about a bad limit below must already reach logs/app.log

DEFAULT_LIMITS = {
    "MFO_RATE_LIMIT_AUTH": "5/minute",
    "MFO_RATE_LIMIT_SUBMIT": "10/minute",
    "MFO_RATE_LIMIT_DEFAULT": "60/minute",
}

# .env next to this file (like run.py). We take ONLY the MFO_RATE_LIMIT_* values from it.
# A variable that is already set in the environment wins over the file.
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def read_limit(name, file_values=None):
    if file_values is None:
        file_values = telegram_env.read_env_file(ENV_FILE)
    value = (os.environ.get(name) or file_values.get(name) or DEFAULT_LIMITS[name]).strip()
    try:
        parse_many(value)  # check the format now: a broken limit must not switch protection off
    except ValueError:
        log.warning("bad rate limit %s=%s, using the default %s", name, clean(value), DEFAULT_LIMITS[name])
        return DEFAULT_LIMITS[name]
    return value


_file_values = telegram_env.read_env_file(ENV_FILE)
AUTH_LIMIT = read_limit("MFO_RATE_LIMIT_AUTH", _file_values)
SUBMIT_LIMIT = read_limit("MFO_RATE_LIMIT_SUBMIT", _file_values)
DEFAULT_LIMIT = read_limit("MFO_RATE_LIMIT_DEFAULT", _file_values)

limiter = Limiter(
    key_func=get_remote_address,     # who is counted: the client's IP address
    default_limits=[DEFAULT_LIMIT],  # for every endpoint without its own @limiter.limit(...)
    key_style="endpoint",            # one counter per endpoint, not per URL (see above)
    strategy="moving-window",        # "5 per minute" = at most 5 in ANY 60 seconds in a row
)


# One spammer can send thousands of blocked requests per minute. Writing every one of them into
# audit.log would fill it, and file rotation would delete the old, important audit lines.
# So audit.log gets at most ONE "rate_limited" line per IP and limit per minute.
# (app.log still gets a WARNING line for every blocked request.)
AUDIT_EVERY_SECONDS = 60
_last_audit = {}


def should_audit(ip, limit_text):
    now = time.monotonic()
    key = (ip, limit_text)
    last = _last_audit.get(key)
    if last is not None and now - last < AUDIT_EVERY_SECONDS:
        return False
    if len(_last_audit) > 10_000:
        _last_audit.clear()  # too many different IPs: forget them, so memory stays small
    _last_audit[key] = now
    return True


def reset():
    # Clear all counters (used by the tests, so every test starts from zero).
    limiter.reset()
    _last_audit.clear()
