# app/api/auth.py
# ------------------------------------------------------------
# /api/auth/*: registration and login through the API, both return a JWT.
#   POST /api/auth/register   public, JSON body, returns a token right away
#   POST /api/auth/login      public, OAuth2 password flow (form: username + password)
#   GET  /api/auth/me         who owns this token
# ------------------------------------------------------------
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import require_user, token_response
from app.api.schemas import RegisterIn
from app.core.logs import audit, mask_login
from app.core.rate_limit import AUTH_LIMIT, limiter
from app.db import database
from app.services import clients
from app.services.auth import check_credentials
from app.services.errors import RuleError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", status_code=201)
@limiter.limit(AUTH_LIMIT)
def api_register(body: RegisterIn, request: Request):
    # Registration through the API: creates the client (+ job) and returns a JWT right away.
    try:
        client_id = clients.register_client(body.iin, body.full_name, body.birth_date, body.phone,
                                            body.address, body.email, body.password, body.marital_status,
                                            enrollment_token=body.enrollment_token)
    except RuleError as error:
        audit("register_failed", request, logging.WARNING, via="api_jwt", reason=error.reason)
        raise HTTPException(400, error.detail)
    if body.employer_name.strip():
        clients.add_employment(client_id, body.employer_name, body.position, body.monthly_income)
    audit("register_ok", request, via="api_jwt", client_id=client_id)
    return {**token_response("client", client_id), "client": database.get_client(client_id)}


@router.post("/login")
@limiter.limit(AUTH_LIMIT)
def api_login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    # OAuth2 password flow: form fields "username" (client e-mail or employee login) and "password".
    user = check_credentials(form.username, form.password)
    if user is None:
        audit("login_failed", request, logging.WARNING, via="api_jwt", login=mask_login(form.username))
        raise HTTPException(401, "Wrong login or password", headers={"WWW-Authenticate": "Bearer"})
    role, user_id = user
    audit("login_ok", request, via="api_jwt", role=role, user_id=user_id)
    return token_response(role, user_id)


@router.get("/me")
def api_me(user: dict = Depends(require_user)):
    # Who owns this token. Requires the header "Authorization: Bearer <token>".
    # (require_user has already refused tokens of deleted accounts.)
    if user["role"] == "employee":
        profile = database.get_employee(user["uid"])
    else:
        profile = database.get_client(user["uid"])
    return {"role": user["role"], "user_id": user["uid"], "expires_at": user["exp"], "profile": profile}
