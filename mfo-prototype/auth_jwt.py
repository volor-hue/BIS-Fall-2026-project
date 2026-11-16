# auth_jwt.py
# ------------------------------------------------------------
# JWT (JSON Web Token) authentication, algorithm HS256, standard library only.
#
# A token is three base64url parts joined by dots:   header.payload.signature
#   header    {"alg": "HS256", "typ": "JWT"}
#   payload   {"sub": "client:3", "role": "client", "uid": 3, "iat": ..., "exp": ...}
#   signature HMAC-SHA256(header + "." + payload, SECRET)
# The server signs the token with a SECRET only it knows. If anyone changes the payload
# (for example uid=3 -> uid=1), the signature no longer matches and the token is rejected.
# This replaces the old plain "client_id"/"employee_id" cookies that anyone could edit.
#
# Where the token travels:
#   - web pages:  in the HttpOnly cookie "access_token" (JavaScript cannot read it);
#   - API:        in the header "Authorization: Bearer <token>" (see /api/auth/* in main.py).
#
# Settings (environment variable wins over .env next to this file):
#   MFO_JWT_SECRET=<long random string>   REQUIRED on a real server.
#       If it is empty, a random secret is generated at start: it works, but all users are
#       logged out on every restart, and a WARNING is written to the log.
#       Generate one: python -c "import secrets; print(secrets.token_urlsafe(48))"
#   MFO_JWT_EXPIRE_MINUTES=60             token lifetime (5 .. 10080 minutes).
# ------------------------------------------------------------
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import telegram_env
from log_config import log, setup_logging

setup_logging()

ALGORITHM = "HS256"
COOKIE_NAME = "access_token"
DEFAULT_EXPIRE_MINUTES = 60
ROLES = ("client", "employee")

ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _setting(name, file_values):
    return (os.environ.get(name) or file_values.get(name) or "").strip()


def _load_settings():
    file_values = telegram_env.read_env_file(ENV_FILE)
    secret = _setting("MFO_JWT_SECRET", file_values)
    if len(secret) < 32:
        if secret:
            log.warning("MFO_JWT_SECRET is shorter than 32 characters, using a random secret instead")
        else:
            log.warning("MFO_JWT_SECRET is not set: using a random secret, users are logged out on restart")
        secret = secrets.token_urlsafe(48)

    minutes_text = _setting("MFO_JWT_EXPIRE_MINUTES", file_values)
    try:
        minutes = int(minutes_text) if minutes_text else DEFAULT_EXPIRE_MINUTES
        if not 5 <= minutes <= 10080:
            raise ValueError
    except ValueError:
        log.warning("bad MFO_JWT_EXPIRE_MINUTES, using the default %s", DEFAULT_EXPIRE_MINUTES)
        minutes = DEFAULT_EXPIRE_MINUTES
    return secret.encode("utf-8"), minutes


SECRET, EXPIRE_MINUTES = _load_settings()
EXPIRE_SECONDS = EXPIRE_MINUTES * 60


class TokenError(Exception):
    # Raised for any bad token: broken format, wrong signature, expired, wrong role.
    pass


def _b64encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text):
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(signing_input):
    return hmac.new(SECRET, signing_input, hashlib.sha256).digest()


def create_token(role, user_id, now=None):
    # Make a signed token for a client or an employee.
    if role not in ROLES:
        raise ValueError("unknown role")
    issued = int(now if now is not None else time.time())
    header = {"alg": ALGORITHM, "typ": "JWT"}
    payload = {"sub": f"{role}:{user_id}", "role": role, "uid": int(user_id),
               "iat": issued, "exp": issued + EXPIRE_SECONDS}
    parts = [_b64encode(json.dumps(part, separators=(",", ":")).encode("utf-8"))
             for part in (header, payload)]
    signing_input = ".".join(parts).encode("ascii")
    return ".".join(parts + [_b64encode(_sign(signing_input))])


def decode_token(token, now=None):
    # Check the token and return its payload. Raises TokenError if anything is wrong.
    if not token or not isinstance(token, str) or len(token) > 4096:
        raise TokenError("no token")
    pieces = token.split(".")
    if len(pieces) != 3:
        raise TokenError("bad format")
    header_b64, payload_b64, signature_b64 = pieces
    try:
        header = json.loads(_b64decode(header_b64))
        signature = _b64decode(signature_b64)
    except (ValueError, UnicodeDecodeError):
        raise TokenError("bad format")
    # The algorithm is fixed on the server: a token with "alg": "none" or anything else is refused.
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        raise TokenError("bad algorithm")
    expected = _sign(f"{header_b64}.{payload_b64}".encode("ascii"))
    # compare_digest takes the same time for any input, so the signature cannot be guessed byte by byte.
    if not hmac.compare_digest(signature, expected):
        raise TokenError("bad signature")
    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        raise TokenError("bad format")
    if not isinstance(payload, dict):
        raise TokenError("bad payload")
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp <= int(now if now is not None else time.time()):
        raise TokenError("expired")
    if payload.get("role") not in ROLES or not isinstance(payload.get("uid"), int):
        raise TokenError("bad payload")
    return payload


def user_from_token(token, role):
    # The user id from a valid token of this role, otherwise None.
    try:
        payload = decode_token(token)
    except TokenError:
        return None
    if payload["role"] != role:
        return None
    return payload["uid"]
