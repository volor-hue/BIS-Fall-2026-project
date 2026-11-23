# app/api/loans.py
# ------------------------------------------------------------
# Loans, their repayment schedules and paying a repayment.
#   client:    only their own loans; pays with the payment window (card / PayPal / Kaspi QR)
#   employee:  all loans; can mark a repayment paid at the office ("manual")
# Saved cards and the Kaspi QR itself live in app/payments/router.py.
# ------------------------------------------------------------
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app import payments
from app.api.deps import client_filter, ensure_own_client, require_user
from app.core.logs import audit
from app.db import database
from app.services import loans

router = APIRouter()


# ---------- loans ----------
@router.get("/api/loans", tags=["loans"])
def api_list_loans(client_id: Optional[int] = None, user: dict = Depends(require_user)):
    return database.get_loans(client_filter(user, client_id))


@router.get("/api/loans/{loan_id}", tags=["loans"])
def api_get_loan(loan_id: int, user: dict = Depends(require_user)):
    loan = database.get_loan(loan_id)
    if loan is None:
        raise HTTPException(404, "Loan not found")
    ensure_own_client(user, loan["client_id"])
    return loan


# ---------- repayments ----------
@router.get("/api/loans/{loan_id}/repayments", tags=["repayments"])
def api_list_repayments(loan_id: int, user: dict = Depends(require_user)):
    loan = database.get_loan(loan_id)
    if loan is None:
        raise HTTPException(404, "Loan not found")
    ensure_own_client(user, loan["client_id"])
    return loans.repayments_with_methods(loan_id)


@router.post("/api/repayments/{payment_id}/pay", tags=["repayments"])
def api_pay(payment_id: int, request: Request, body: Optional[payments.PayIn] = None,
            user: dict = Depends(require_user)):
    # Optional body {"method": "card"|"paypal"|"kaspi_qr", ...} = the payment window of the dashboard.
    # No body = "manual" (old API calls and employees), works exactly as before.
    payment = database.get_repayment(payment_id)
    if payment is None:
        raise HTTPException(404, "Payment not found")
    client_id = database.get_loan(payment["loan_id"])["client_id"]
    ensure_own_client(user, client_id)
    result, method = loans.pay(payment, client_id, user, body)
    audit("payment_paid", request, payment_id=payment_id, loan_id=payment["loan_id"],
          amount=payment["amount_due"], paid_with=method)
    return result
