# app/api/schemas.py
# ------------------------------------------------------------
# Request bodies (Pydantic models). FastAPI uses them to check the JSON that comes in
# and to show example bodies in /docs. Field(gt=0) means "must be greater than 0".
# ------------------------------------------------------------
from typing import Optional

from pydantic import BaseModel, Field


class ClientIn(BaseModel):
    iin: str = Field(min_length=12, max_length=12, pattern=r"^\d{12}$", examples=["050101500123"])
    full_name: str = Field(examples=["Test Client"])
    birth_date: str = Field(examples=["2005-01-01"])
    phone: str = Field(examples=["+77011234567"])
    address: str = Field("", examples=["Almaty"])
    email: str = Field(examples=["client@test.kz"])
    password: str = Field(min_length=4, examples=["pass123"])
    marital_status: str = Field("Single", examples=["Single"])


class RegisterIn(ClientIn):
    # Registration through the API: the client and (optionally) their job in one request.
    employer_name: str = Field("", examples=["Kaspi Bank"])
    position: str = Field("", examples=["Analyst"])
    monthly_income: float = Field(0, ge=0, examples=[300000])
    # One-time token from POST /api/face/enroll/finish ("Пройти биометрию"). Required for clients
    # unless FACE_ENROLL_REQUIRED=0.
    enrollment_token: Optional[str] = Field(None, max_length=200)


class EmploymentIn(BaseModel):
    employer_name: str = Field(examples=["Kaspi Bank"])
    position: str = Field("", examples=["Analyst"])
    monthly_income: float = Field(ge=0, examples=[300000])


class ApplicationIn(BaseModel):
    client_id: int = Field(examples=[1])
    requested_amount: float = Field(gt=0, examples=[400000])
    term_months: int = Field(gt=0, le=120, examples=[12])
    purpose: str = Field("", examples=["Small business"])
    # Clients must send the one-time token from POST /api/face/verify. Employees do not need it.
    face_token: Optional[str] = Field(None, max_length=200,
                                      description="One-time token from POST /api/face/verify (clients only)")


class DecisionIn(BaseModel):
    # Optional: the employee is taken from the JWT. If it is sent, it must match the token.
    employee_id: Optional[int] = Field(None, examples=[1])
    decision: str = Field(pattern="^(approve|reject)$", examples=["approve"])
