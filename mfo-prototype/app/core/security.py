# app/core/security.py
# ------------------------------------------------------------
# Security helpers of the whole project:
#   1. passwords: hash_password() / password_matches()
#   2. JWT (JSON Web Token) login tokens, algorithm HS256, standard library only.
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
#   - API:        in the header "Authorization: Bearer <token>" (see app/api/auth.py).
#
# The secret and the lifetime are settings: MFO_JWT_SECRET, MFO_JWT_EXPIRE_MINUTES (app/core/config.py).
# Who may do what with a valid token is decided in app/api/deps.py.
# ------------------------------------------------------------
import base64
import hashlib
import hmac
import json
import time

from app.core import config

ALGORITHM = "HS256"
COOKIE_NAME = "access_token"
ROLES = ("client", "employee")

SECRET = config.JWT_SECRET
EXPIRE_MINUTES = config.JWT_EXPIRE_MINUTES
EXPIRE_SECONDS = EXPIRE_MINUTES * 60


# ============================================================
# Passwords
# ============================================================

def hash_password(password):
    # We never store the password itself, only its SHA-256 "fingerprint".
    # NOTE: simplified for the prototype. A real system must use bcrypt/argon2.
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def password_matches(password, stored_hash):
    # compare_digest takes the same time for any input, so the hash cannot be guessed byte by byte.
    return hmac.compare_digest(hash_password(password), stored_hash)


# ============================================================
# JWT
# ============================================================

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
