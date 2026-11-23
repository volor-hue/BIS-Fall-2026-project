# app/web/pages.py
# ------------------------------------------------------------
# HTML pages (include_in_schema=False hides them from /docs).
# The forms use the SAME services as the API (app/services/), so the rules are the same.
# The page JavaScript then calls /api/* with the access_token cookie.
# ------------------------------------------------------------
import logging

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from app.api.deps import current_client_id, current_employee_id, set_auth_cookie
from app.core import config, security
from app.core.logs import audit, mask_login
from app.core.rate_limit import AUTH_LIMIT, limiter
from app.db import database
from app.services import clients, documents
from app.services.auth import check_credentials
from app.services.errors import RuleError
from app.web.templates import templates

router = APIRouter(include_in_schema=False)

# The web form shows its own, more friendly duplicate message.
WEB_MESSAGES = {"duplicate": "A client with this IIN or email already exists."}


def register_form(request, error, enrollment_token, status_code=200, headers=None):
    return templates.TemplateResponse(request, "register.html",
                                      {"error": error, "enrollment_token": enrollment_token,
                                       "enroll_required": config.face.ENROLL_REQUIRED},
                                      status_code=status_code, headers=headers)


@router.get("/")
def home(request: Request):
    if current_employee_id(request):
        return RedirectResponse("/admin", status_code=303)
    if current_client_id(request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/register")
def register_page(request: Request):
    return register_form(request, None, "")


@router.post("/register")
@limiter.limit(AUTH_LIMIT)
def register(
    request: Request,
    full_name: str = Form(...), iin: str = Form(...), phone: str = Form(...),
    email: str = Form(...), password: str = Form(...), address: str = Form(""),
    birth_date: str = Form(""), marital_status: str = Form("Single"),
    employer_name: str = Form(...), position: str = Form(""), monthly_income: float = Form(...),
    passport_photo: UploadFile = File(None), enrollment_token: str = Form(""),
):
    iin = iin.strip()
    error = None
    reason = None  # short code for the log (we never log the IIN or the e-mail itself)
    # Check 1: IIN must be exactly 12 characters and all of them digits.
    if len(iin) != 12 or not iin.isdigit():
        error, reason = "IIN must contain exactly 12 digits.", "bad_iin"
    # Check 2: income cannot be negative.
    elif monthly_income < 0:
        error, reason = "Monthly income cannot be negative.", "negative_income"

    client_id = None
    if error is None:
        try:
            # One registration fills the tables clients, employment, documents (+ face_references).
            # Check 3 (biometrics) is inside the service, like for /api/auth/register.
            client_id = clients.register_client(iin, full_name, birth_date, phone, address, email,
                                                password, marital_status, enrollment_token=enrollment_token)
        except RuleError as failed:
            error, reason = WEB_MESSAGES.get(failed.reason, failed.detail), failed.reason

    if error is not None:
        audit("register_failed", request, logging.WARNING, via="web", reason=reason)
        # A still valid biometric enrollment is kept, so the user does not have to turn the head again.
        try:
            clients.check_registration_biometrics(enrollment_token)
            kept = enrollment_token
        except RuleError:
            kept = ""
        return register_form(request, error, kept, status_code=400)

    clients.add_employment(client_id, employer_name, position, monthly_income)
    audit("register_ok", request, via="web", client_id=client_id)
    if passport_photo is not None and passport_photo.filename:
        documents.save_document(client_id, "passport", passport_photo)
    return RedirectResponse("/login?registered=1", status_code=303)


@router.get("/login")
def login_page(request: Request):
    registered = request.query_params.get("registered") == "1"
    return templates.TemplateResponse(request, "login.html", {"error": None, "registered": registered})


@router.post("/login")
@limiter.limit(AUTH_LIMIT)
def login(request: Request, login: str = Form(...), password: str = Form(...)):
    user = check_credentials(login, password)
    if user is not None:
        role, user_id = user
        audit("login_ok", request, role=role, user_id=user_id)
        # The browser gets a signed JWT in an HttpOnly cookie (not a plain id anyone could edit).
        response = RedirectResponse("/admin" if role == "employee" else "/dashboard", status_code=303)
        return set_auth_cookie(response, role, user_id)

    # Failed logins are WARNING: many of them in a row = someone is guessing passwords.
    # The password is NEVER written to the log, the login is masked.
    audit("login_failed", request, logging.WARNING, login=mask_login(login.strip().lower()))
    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Wrong login or password.", "registered": False},
                                      status_code=401)


@router.get("/logout")
def logout(request: Request):
    audit("logout", request)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.COOKIE_NAME)
    response.delete_cookie("client_id")
    response.delete_cookie("employee_id")
    return response


@router.get("/dashboard")
def client_dashboard(request: Request):
    client_id = current_client_id(request)
    client = database.get_client(client_id) if client_id else None
    if client is None:
        return RedirectResponse("/login", status_code=303)
    income = database.get_total_income(client_id)
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"client": client, "income": income,
                                       "user_name": client["full_name"]})


@router.get("/admin")
def admin_dashboard(request: Request):
    employee_id = current_employee_id(request)
    employee = database.get_employee(employee_id) if employee_id else None
    if employee is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html",
                                      {"employee": employee,
                                       "user_name": employee["full_name"] + " (" + employee["role"] + ")"})
