# app/api/deps.py
# ------------------------------------------------------------
# WHO is calling and WHAT they may do. Shared by every router (API, web pages, face, payments).
#
#   authenticate / require_user   a valid JWT (Bearer header or the access_token cookie), else 401
#   require_employee              only employees, a client gets 403
#   ensure_own_client             a client only touches their own data, else 403
#   client_filter                 list endpoints: a client always sees only their own rows
#
# The token itself (signature, expiry) is checked in app/core/security.py.
# ------------------------------------------------------------
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer

from app.core import security
from app.db import database

# Swagger (/docs) shows the "Authorize" button: enter login + password, it calls /api/auth/login.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def request_token(request):
    # The JWT of this request: "Authorization: Bearer <token>" (API) or the HttpOnly cookie (pages).
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(security.COOKIE_NAME)


def current_client_id(request):
    # Who is logged in is taken ONLY from a JWT signed by the server.
    # An edited or expired token gives None, so the user is sent back to /login.
    return security.user_from_token(request_token(request), "client")


def current_employee_id(request):
    return security.user_from_token(request_token(request), "employee")


def who(request):
    # A short name of the current user for the logs: "employee:1", "client:3" or "anon".
    employee_id = current_employee_id(request)
    if employee_id:
        return f"employee:{employee_id}"
    client_id = current_client_id(request)
    if client_id:
        return f"client:{client_id}"
    return "anon"


def _unauthorized(detail):
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


def authenticate(request, token):
    # Check the JWT (Bearer header from the API or the cookie from the web pages) -> its payload.
    # The account must still exist: a token of a deleted client is refused.
    #
    # Endpoints with their own rate limit call this INSIDE the function instead of using
    # Depends(require_user): this way the limiter counts the request first, so requests
    # without a token are limited too.
    try:
        payload = security.decode_token(token or request.cookies.get(security.COOKIE_NAME))
    except security.TokenError:
        raise _unauthorized("Not authenticated or token expired")
    if payload["role"] == "employee":
        exists = database.get_employee(payload["uid"]) is not None
    else:
        exists = database.get_client(payload["uid"]) is not None
    if not exists:
        raise _unauthorized("User no longer exists")
    return payload


def require_user(request: Request, token: Optional[str] = Depends(oauth2_scheme)):
    # Dependency: any logged-in user (client or employee), otherwise 401.
    return authenticate(request, token)


def require_employee(user: dict = Depends(require_user)):
    # Dependency: only employees, a client gets 403.
    if user["role"] != "employee":
        raise HTTPException(403, "Only employees can do this")
    return user


def ensure_own_client(user, client_id):
    # An employee may work with any client, a client only with their own data.
    if user["role"] != "employee" and user["uid"] != client_id:
        raise HTTPException(403, "Access denied: this is another client's data")


def client_filter(user, client_id):
    # For list endpoints (?client_id=...): a client always sees only their own rows.
    if user["role"] == "employee":
        return client_id
    if client_id is not None and client_id != user["uid"]:
        raise HTTPException(403, "Access denied: this is another client's data")
    return user["uid"]


def token_response(role, user_id):
    # The JSON the API returns after login/registration (OAuth2 "password flow" format).
    return {"access_token": security.create_token(role, user_id), "token_type": "bearer",
            "expires_in": security.EXPIRE_SECONDS, "role": role, "user_id": user_id}


def set_auth_cookie(response, role, user_id):
    # HttpOnly: JavaScript cannot read the token. SameSite=Lax: other sites cannot send forms with it.
    response.set_cookie(security.COOKIE_NAME, security.create_token(role, user_id),
                        max_age=security.EXPIRE_SECONDS, httponly=True, samesite="lax")
    # Remove the old plain-id cookies of the previous prototype version.
    response.delete_cookie("client_id")
    response.delete_cookie("employee_id")
    return response
