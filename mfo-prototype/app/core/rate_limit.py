# app/core/rate_limit.py
# ------------------------------------------------------------
# Protection from request spam (rate limiting) with the "slowapi" library.
# Every visitor is identified by IP address. If one IP sends too many requests to one endpoint,
# the server answers 429 Too Many Requests (app/main.py writes a WARNING to the log).
#
# The limits themselves are settings: MFO_RATE_LIMIT_AUTH / _SUBMIT / _DEFAULT in app/core/config.py.
#
# Every endpoint has its OWN counter per IP: /api/clients/1 and /api/clients/2 share one counter
# (same endpoint), but /api/loans counts separately.
#
# Counters live in the server's memory: they reset on restart, and with several uvicorn workers
# every worker counts separately. A real server would keep them in shared storage (Redis).
# ------------------------------------------------------------
import time

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core import config

AUTH_LIMIT = config.AUTH_LIMIT
SUBMIT_LIMIT = config.SUBMIT_LIMIT
DEFAULT_LIMIT = config.DEFAULT_LIMIT

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
