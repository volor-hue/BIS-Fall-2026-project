# main.py
# ------------------------------------------------------------
# The web application (FastAPI).
# Run it with:   uvicorn main:app --reload
# Pages:         http://127.0.0.1:8000
# API docs:      http://127.0.0.1:8000/docs   (Swagger, try every endpoint there)
# ------------------------------------------------------------
import hashlib
import os
import sqlite3
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import database
from approval import INTEREST_RATE, build_schedule, decide_application, run_checks

app = FastAPI(title="Microfinance IS", description="Loan applications, checks, loans and repayments")
templates = Jinja2Templates(directory="templates")

# Uploaded documents are saved into this folder.
UPLOAD_DIR = "uploads"

# Create the tables and the default employee when the app starts.
database.init_db()


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Request bodies (Pydantic models). FastAPI uses them to check the
# JSON that comes in and to show example bodies in /docs.
# Field(gt=0) means "must be greater than 0".
# ============================================================

class ClientIn(BaseModel):
    iin: str = Field(min_length=12, max_length=12, pattern=r"^\d{12}$", examples=["050101500123"])
    full_name: str = Field(examples=["Test Client"])
    birth_date: str = Field(examples=["2005-01-01"])
    phone: str = Field(examples=["+77011234567"])
    address: str = Field("", examples=["Almaty"])
    email: str = Field(examples=["client@test.kz"])
    password: str = Field(min_length=4, examples=["pass123"])
    marital_status: str = Field("Single", examples=["Single"])


class EmploymentIn(BaseModel):
    employer_name: str = Field(examples=["Kaspi Bank"])
    position: str = Field("", examples=["Analyst"])
    monthly_income: float = Field(ge=0, examples=[300000])


class ApplicationIn(BaseModel):
    client_id: int = Field(examples=[1])
    requested_amount: float = Field(gt=0, examples=[400000])
    term_months: int = Field(gt=0, le=120, examples=[12])
    purpose: str = Field("", examples=["Small business"])


class DecisionIn(BaseModel):
    employee_id: int = Field(examples=[1])
    decision: str = Field(pattern="^(approve|reject)$", examples=["approve"])


# ============================================================
# Helper functions used by both the API and the HTML pages
# ============================================================

def save_document(client_id, doc_type, upload):
    # 1) read the file bytes, 2) compute SHA-256 of the content,
    # 3) save the file into uploads/, 4) write a row into "documents".
    content = upload.file.read()
    file_hash = hashlib.sha256(content).hexdigest()
    # Keep only safe characters of the original name (no "../" tricks).
    original = os.path.basename(upload.filename or "file")
    safe_name = "".join(ch for ch in original if ch.isalnum() or ch in "._-") or "file"
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, f"client{client_id}_{doc_type}_{file_hash[:8]}_{safe_name}")
    with open(file_path, "wb") as f:
        f.write(content)
    document_id = database.create_document(client_id, doc_type, file_path, file_hash)
    return {"document_id": document_id, "client_id": client_id, "doc_type": doc_type,
            "file_path": file_path, "file_hash": file_hash}


def issue_loan(application):
    # Called when an application becomes "Approved" (auto or by an employee).
    # The application "becomes" a loan, and the loan gets its monthly schedule.
    today = date.today()
    schedule = build_schedule(application["requested_amount"], INTEREST_RATE,
                              application["term_months"], today)
    return database.create_loan_with_schedule(application["application_id"],
                                              application["requested_amount"],
                                              INTEREST_RATE, today.isoformat(), schedule)


def submit_application(client_id, requested_amount, term_months, purpose):
    client = database.get_client(client_id)
    if client is None:
        raise HTTPException(404, "Client not found")

    # Income = SUM of monthly_income over ALL jobs in the employment table.
    income = database.get_total_income(client_id)

    # The business rule (approval.py) decides the status.
    status, approval_type = decide_application(income, requested_amount, term_months)
    app_id = database.create_application(client_id, requested_amount, term_months,
                                         purpose, status, approval_type, now_text())

    # Save the automatic checks for this application.
    checks = run_checks(client["iin"], client["birth_date"], income, requested_amount, term_months)
    database.create_checks(app_id, checks, now_text())

    application = database.get_application(app_id)
    # Only an APPROVED application becomes a loan.
    if status == "Approved":
        application["loan_id"] = issue_loan(application)
    return application


def current_client_id(request):
    # PROTOTYPE LOGIN: we keep the id in a plain cookie. Anyone can edit a cookie,
    # so a real system must use signed sessions/JWT (Architecture & Security task).
    value = request.cookies.get("client_id")
    if value and value.isdigit():
        return int(value)
    return None


def current_employee_id(request):
    value = request.cookies.get("employee_id")
    if value and value.isdigit():
        return int(value)
    return None


# ============================================================
# REST API (JSON). NOTE: the API is open (no login) so it can be tried in /docs.
# Protecting it with authentication is a planned Security task.
# ============================================================

# ---------- clients ----------
@app.post("/api/clients", tags=["clients"], status_code=201)
def api_create_client(body: ClientIn):
    try:
        client_id = database.create_client(body.iin, body.full_name, body.birth_date, body.phone,
                                           body.address, body.email.strip().lower(), body.password,
                                           body.marital_status, now_text())
    except sqlite3.IntegrityError:
        # UNIQUE constraint on iin or email failed.
        raise HTTPException(400, "Client with this IIN or email already exists")
    return database.get_client(client_id)


@app.get("/api/clients", tags=["clients"])
def api_list_clients():
    return database.get_clients()


@app.get("/api/clients/{client_id}", tags=["clients"])
def api_get_client(client_id: int):
    client = database.get_client(client_id)
    if client is None:
        raise HTTPException(404, "Client not found")
    client["total_monthly_income"] = database.get_total_income(client_id)
    return client


@app.delete("/api/clients/{client_id}", tags=["clients"])
def api_delete_client(client_id: int):
    # Demonstrates ON DELETE CASCADE: all related rows disappear too.
    if database.delete_client(client_id) == 0:
        raise HTTPException(404, "Client not found")
    return {"deleted_client_id": client_id}


# ---------- employment ----------
@app.post("/api/clients/{client_id}/employment", tags=["employment"], status_code=201)
def api_add_employment(client_id: int, body: EmploymentIn):
    try:
        employment_id = database.create_employment(client_id, body.employer_name,
                                                   body.position, body.monthly_income)
    except sqlite3.IntegrityError:
        # The FOREIGN KEY rule stopped us: there is no client with this id.
        raise HTTPException(400, "FOREIGN KEY constraint failed: client does not exist")
    return {"employment_id": employment_id, "client_id": client_id, **body.model_dump()}


@app.get("/api/clients/{client_id}/employment", tags=["employment"])
def api_list_employment(client_id: int):
    return database.get_employment(client_id)


# ---------- documents ----------
@app.post("/api/clients/{client_id}/documents", tags=["documents"], status_code=201)
def api_upload_document(client_id: int, doc_type: str = Form("passport"), file: UploadFile = File(...)):
    if database.get_client(client_id) is None:
        raise HTTPException(404, "Client not found")
    return save_document(client_id, doc_type, file)


@app.get("/api/clients/{client_id}/documents", tags=["documents"])
def api_list_documents(client_id: int):
    return database.get_documents(client_id)


# ---------- employees ----------
@app.get("/api/employees", tags=["employees"])
def api_list_employees():
    return database.get_employees()


# ---------- loan applications ----------
@app.post("/api/applications", tags=["applications"], status_code=201)
def api_create_application(body: ApplicationIn):
    return submit_application(body.client_id, body.requested_amount, body.term_months, body.purpose)


@app.get("/api/applications", tags=["applications"])
def api_list_applications(client_id: Optional[int] = None):
    # /api/applications            -> all applications
    # /api/applications?client_id=1 -> only this client's applications
    return database.get_applications(client_id)


@app.get("/api/applications/{application_id}", tags=["applications"])
def api_get_application(application_id: int):
    application = database.get_application(application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    return application


@app.get("/api/applications/{application_id}/checks", tags=["client_checks"])
def api_get_checks(application_id: int):
    if database.get_application(application_id) is None:
        raise HTTPException(404, "Application not found")
    return database.get_checks(application_id)


@app.post("/api/applications/{application_id}/decision", tags=["applications"])
def api_decide(application_id: int, body: DecisionIn):
    application = database.get_application(application_id)
    if application is None:
        raise HTTPException(404, "Application not found")
    if database.get_employee(body.employee_id) is None:
        raise HTTPException(404, "Employee not found")
    # Only MANUAL applications that are still waiting can be decided by an employee.
    if application["approval_type"] != "Manual" or application["status"] != "Pending Admin Review":
        raise HTTPException(400, "Only pending manual applications can be decided")

    if body.decision == "approve":
        new_status = "Approved"
    else:
        new_status = "Rejected"

    # Save the decision AND who made it (employee_id).
    database.set_application_decision(application_id, new_status, body.employee_id)
    application = database.get_application(application_id)
    if new_status == "Approved":
        application["loan_id"] = issue_loan(application)
    return application


# ---------- loans ----------
@app.get("/api/loans", tags=["loans"])
def api_list_loans(client_id: Optional[int] = None):
    return database.get_loans(client_id)


@app.get("/api/loans/{loan_id}", tags=["loans"])
def api_get_loan(loan_id: int):
    loan = database.get_loan(loan_id)
    if loan is None:
        raise HTTPException(404, "Loan not found")
    return loan


# ---------- repayments ----------
@app.get("/api/loans/{loan_id}/repayments", tags=["repayments"])
def api_list_repayments(loan_id: int):
    if database.get_loan(loan_id) is None:
        raise HTTPException(404, "Loan not found")
    return database.get_repayments(loan_id)


@app.post("/api/repayments/{payment_id}/pay", tags=["repayments"])
def api_pay(payment_id: int):
    payment = database.get_repayment(payment_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    if payment["status"] == "Paid":
        raise HTTPException(400, "This payment is already paid")
    database.mark_repayment_paid(payment_id)
    return database.get_repayment(payment_id)


# ============================================================
# HTML pages (include_in_schema=False hides them from /docs)
# ============================================================

@app.get("/", include_in_schema=False)
def home(request: Request):
    if current_employee_id(request):
        return RedirectResponse("/admin", status_code=303)
    if current_client_id(request):
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/register", include_in_schema=False)
def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html", {"error": None})


@app.post("/register", include_in_schema=False)
def register(
    request: Request,
    full_name: str = Form(...), iin: str = Form(...), phone: str = Form(...),
    email: str = Form(...), password: str = Form(...), address: str = Form(""),
    birth_date: str = Form(""), marital_status: str = Form("Single"),
    employer_name: str = Form(...), position: str = Form(""), monthly_income: float = Form(...),
    passport_photo: UploadFile = File(None),
):
    iin = iin.strip()
    error = None
    # Check 1: IIN must be exactly 12 characters and all of them digits.
    if len(iin) != 12 or not iin.isdigit():
        error = "IIN must contain exactly 12 digits."
    # Check 2: income cannot be negative.
    elif monthly_income < 0:
        error = "Monthly income cannot be negative."

    client_id = None
    if error is None:
        try:
            # One registration fills THREE tables: clients, employment, documents.
            client_id = database.create_client(iin, full_name, birth_date, phone, address,
                                               email.strip().lower(), password, marital_status, now_text())
        except sqlite3.IntegrityError:
            error = "A client with this IIN or email already exists."

    if error is not None:
        return templates.TemplateResponse(request, "register.html", {"error": error}, status_code=400)

    database.create_employment(client_id, employer_name, position, monthly_income)
    if passport_photo is not None and passport_photo.filename:
        save_document(client_id, "passport", passport_photo)
    return RedirectResponse("/login?registered=1", status_code=303)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    registered = request.query_params.get("registered") == "1"
    return templates.TemplateResponse(request, "login.html", {"error": None, "registered": registered})


@app.post("/login", include_in_schema=False)
def login(request: Request, login: str = Form(...), password: str = Form(...)):
    login = login.strip().lower()
    password_hash = database.hash_password(password)

    # First we look for an EMPLOYEE with this login...
    employee = database.get_employee_login(login)
    if employee is not None and employee["password_hash"] == password_hash:
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie("employee_id", str(employee["employee_id"]), httponly=True)
        response.delete_cookie("client_id")
        return response

    # ...then for a CLIENT with this email.
    client = database.get_client_login(login)
    if client is not None and client["password_hash"] == password_hash:
        response = RedirectResponse("/dashboard", status_code=303)
        response.set_cookie("client_id", str(client["client_id"]), httponly=True)
        response.delete_cookie("employee_id")
        return response

    return templates.TemplateResponse(request, "login.html",
                                      {"error": "Wrong login or password.", "registered": False},
                                      status_code=401)


@app.get("/logout", include_in_schema=False)
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("client_id")
    response.delete_cookie("employee_id")
    return response


@app.get("/dashboard", include_in_schema=False)
def client_dashboard(request: Request):
    client_id = current_client_id(request)
    client = database.get_client(client_id) if client_id else None
    if client is None:
        return RedirectResponse("/login", status_code=303)
    income = database.get_total_income(client_id)
    return templates.TemplateResponse(request, "dashboard.html",
                                      {"client": client, "income": income,
                                       "user_name": client["full_name"]})


@app.get("/admin", include_in_schema=False)
def admin_dashboard(request: Request):
    employee_id = current_employee_id(request)
    employee = database.get_employee(employee_id) if employee_id else None
    if employee is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "admin.html",
                                      {"employee": employee,
                                       "user_name": employee["full_name"] + " (" + employee["role"] + ")"})
