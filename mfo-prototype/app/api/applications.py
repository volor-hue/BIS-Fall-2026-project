# app/api/applications.py
# ------------------------------------------------------------
# Loan applications, their automatic checks and the employee's decision.
#   client:    submits only for themselves (with a fresh face_token), sees only their own
#   employee:  sees all, enters applications for any client, sees checks, decides manual ones
# ------------------------------------------------------------
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app import face_auth
from app.api.deps import (authenticate, client_filter, ensure_own_client, oauth2_scheme,
                          require_employee, require_user)
from app.api.schemas import ApplicationIn, DecisionIn
from app.core.logs import audit
from app.core.rate_limit import SUBMIT_LIMIT, limiter
from app.db import database
from app.services import loans

router = APIRouter(prefix="/api/applications")


@router.post("", tags=["applications"], status_code=201)
@limiter.limit(SUBMIT_LIMIT)
def api_create_application(body: ApplicationIn, request: Request,
                           token: Optional[str] = Depends(oauth2_scheme)):
    user = authenticate(request, token)   # inside: the limiter counts requests without a token too
    # A client can apply only for themselves; an employee can enter an application for any client.
    ensure_own_client(user, body.client_id)
    if user["role"] == "client":
        # Server-side face gate: without a fresh one-time face_token the application is refused (403).
        # An employee entering an application for a client in the office does not need it.
        face_auth.require_fresh_face_token(request, body.face_token, body.client_id)
    application = loans.submit_application(body.client_id, body.requested_amount, body.term_months, body.purpose)
    audit("application_submitted", request, application_id=application["application_id"],
          client_id=body.client_id, amount=body.requested_amount, term_months=body.term_months,
          status=application["status"], approval_type=application["approval_type"])
    return application


@router.get("", tags=["applications"])
def api_list_applications(client_id: Optional[int] = None, user: dict = Depends(require_user)):
    # Employee: /api/applications -> all, ?client_id=1 -> only this client's applications.
    # Client: always only their own applications.
    return database.get_applications(client_filter(user, client_id))


@router.get("/{application_id}", tags=["applications"])
def api_get_application(application_id: int, user: dict = Depends(require_user)):
    application = database.get_application(application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    ensure_own_client(user, application["client_id"])
    return application


@router.get("/{application_id}/checks", tags=["client_checks"])
def api_get_checks(application_id: int, user: dict = Depends(require_employee)):
    # The internal scoring checks are for employees only.
    if database.get_application(application_id) is None:
        raise HTTPException(404, "Application not found")
    return database.get_checks(application_id)


@router.post("/{application_id}/decision", tags=["applications"])
def api_decide(application_id: int, body: DecisionIn, request: Request,
               user: dict = Depends(require_employee)):
    # WHO decides is taken from the token, not from the request body:
    # an employee cannot make a decision in the name of a colleague.
    employee_id = user["uid"]
    if body.employee_id is not None and body.employee_id != employee_id:
        raise HTTPException(403, "employee_id does not match the logged-in employee")
    application = loans.decide(application_id, employee_id, body.decision)
    audit("application_decided", request, application_id=application_id,
          decision=body.decision, employee_id=employee_id)
    return application
