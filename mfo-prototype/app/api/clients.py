# app/api/clients.py
# ------------------------------------------------------------
# Clients, their jobs (employment) and documents, and the list of employees.
#   public:    POST /api/clients (registration without a token in the answer)
#   employee:  list / delete clients, list employees
#   client:    only their own profile, jobs and documents (another client's -> 403)
# ------------------------------------------------------------
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from app.api.deps import authenticate, ensure_own_client, oauth2_scheme, require_employee, require_user
from app.api.schemas import ClientIn, EmploymentIn
from app.core.logs import audit
from app.core.rate_limit import SUBMIT_LIMIT, limiter
from app.db import database
from app.services import clients, documents
from app.services.errors import RuleError

router = APIRouter()


# ---------- clients ----------
@router.post("/api/clients", tags=["clients"], status_code=201)
def api_create_client(body: ClientIn, request: Request):
    # PUBLIC: this is registration (like /api/auth/register, but without a token in the answer).
    try:
        client_id = clients.register_client(body.iin, body.full_name, body.birth_date, body.phone,
                                            body.address, body.email, body.password, body.marital_status,
                                            check_biometrics=False)
    except RuleError as error:
        audit("register_failed", request, logging.WARNING, via="api", reason=error.reason)
        raise HTTPException(400, error.detail)
    audit("register_ok", request, via="api", client_id=client_id)
    return database.get_client(client_id)


@router.get("/api/clients", tags=["clients"])
def api_list_clients(user: dict = Depends(require_employee)):
    return database.get_clients()


@router.get("/api/clients/{client_id}", tags=["clients"])
def api_get_client(client_id: int, user: dict = Depends(require_user)):
    ensure_own_client(user, client_id)
    client = database.get_client(client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    client["total_monthly_income"] = database.get_total_income(client_id)
    return client


@router.delete("/api/clients/{client_id}", tags=["clients"])
def api_delete_client(client_id: int, request: Request, user: dict = Depends(require_employee)):
    clients.delete_client(client_id)
    audit("client_deleted", request, logging.WARNING, client_id=client_id)
    return {"deleted_client_id": client_id}


# ---------- employment ----------
@router.post("/api/clients/{client_id}/employment", tags=["employment"], status_code=201)
def api_add_employment(client_id: int, body: EmploymentIn, user: dict = Depends(require_user)):
    ensure_own_client(user, client_id)
    employment_id = clients.add_employment(client_id, body.employer_name, body.position, body.monthly_income)
    return {"employment_id": employment_id, "client_id": client_id, **body.model_dump()}


@router.get("/api/clients/{client_id}/employment", tags=["employment"])
def api_list_employment(client_id: int, user: dict = Depends(require_user)):
    ensure_own_client(user, client_id)
    return database.get_employment(client_id)


# ---------- documents ----------
@router.post("/api/clients/{client_id}/documents", tags=["documents"], status_code=201)
@limiter.limit(SUBMIT_LIMIT)
def api_upload_document(request: Request, client_id: int, doc_type: str = Form("passport"),
                        file: UploadFile = File(...), token: Optional[str] = Depends(oauth2_scheme)):
    user = authenticate(request, token)   # inside: the limiter counts requests without a token too
    ensure_own_client(user, client_id)
    if database.get_client(client_id) is None:
        raise HTTPException(404, "Client not found")
    return documents.save_document(client_id, doc_type, file)


@router.get("/api/clients/{client_id}/documents", tags=["documents"])
def api_list_documents(client_id: int, user: dict = Depends(require_user)):
    ensure_own_client(user, client_id)
    return database.get_documents(client_id)


# ---------- employees ----------
@router.get("/api/employees", tags=["employees"])
def api_list_employees(user: dict = Depends(require_employee)):
    return database.get_employees()
