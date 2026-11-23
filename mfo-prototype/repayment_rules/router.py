# repayment_rules/router.py
# ------------------------------------------------------------
# Web layer of the repayment rules (FastAPI). main.py adds it with app.include_router(...).
#
#   GET  /api/loans/{loan_id}/early-payoff   how much to pay today to close the loan (BR-4, BR-5)
#   POST /api/loans/{loan_id}/early-payoff   close the loan early {"method": "card"|"paypal"|"manual", ...}
#
# Paying ONE monthly repayment stays in main.py (POST /api/repayments/{id}/pay):
# it now adds the penalty of an overdue repayment (charge() in rules.py).
# ------------------------------------------------------------
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

import database
import payments
from log_config import audit
from repayment_rules import rules, storage


def enrich(loan_id, rows, on_day=None):
    """Add the rule fields to the rows of GET /api/loans/{id}/repayments:
    display_status (Pending / Overdue / Paid), days_overdue, penalty, total_to_pay, closed_early."""
    on_day = on_day or rules.today()
    paid_penalties = storage.penalties_for_loan(loan_id)
    closed_early = storage.closed_early_for_loan(loan_id)
    payoff = storage.get_payoff(loan_id) if closed_early else None
    for row in rows:
        row.update(rules.charge(row, on_day))
        row["closed_early"] = row["payment_id"] in closed_early
        if row["status"] == rules.PAID:
            paid = paid_penalties.get(row["payment_id"])
            row["days_overdue"] = paid["days_overdue"] if paid else 0
            row["penalty"] = paid["penalty_amount"] if paid else 0.0
            row["total_to_pay"] = 0.0
        if row["closed_early"] and not row.get("paid_method"):
            row["paid_method"] = "Досрочно: " + payoff["method_detail"]
    return rows


def create_router(require_user, ensure_own_client):
    router = APIRouter()

    def own_loan(loan_id, user):
        loan = database.get_loan(loan_id)
        if loan is None:
            raise HTTPException(404, "Loan not found")
        ensure_own_client(user, loan["client_id"])
        return loan

    @router.get("/api/loans/{loan_id}/early-payoff", tags=["repayment rules"])
    def api_payoff_quote(loan_id: int, user: dict = Depends(require_user)):
        loan = own_loan(loan_id, user)
        return rules.early_payoff_quote(loan, database.get_repayments(loan_id))

    @router.post("/api/loans/{loan_id}/early-payoff", tags=["repayment rules"])
    def api_payoff(loan_id: int, request: Request, body: Optional[payments.PayIn] = None,
                   user: dict = Depends(require_user)):
        loan = own_loan(loan_id, user)
        repayments = database.get_repayments(loan_id)
        quote = rules.early_payoff_quote(loan, repayments)
        if not quote["allowed"]:
            raise HTTPException(400, quote["reason"])
        if body and (body.method or "").strip().lower() == "kaspi_qr":
            raise HTTPException(400, "Kaspi QR работает только для одного платежа. Выберите карту или PayPal.")

        unpaid_ids = [r["payment_id"] for r in repayments if r["status"] != rules.PAID]
        # The SAME method check as a normal payment (owner, card exists and not expired, PayPal e-mail).
        method, method_detail = payments.choose_method(user, loan["client_id"], unpaid_ids[0], body)
        try:
            storage.close_loan_early(loan_id, unpaid_ids, quote["payoff_amount"], quote["scheduled_left"],
                                     method_detail, user["role"])
        except ValueError as error:
            raise HTTPException(409, str(error))
        audit("loan_closed_early", request, loan_id=loan_id, client_id=loan["client_id"],
              principal=quote["payoff_amount"], saving=quote["saving"], payments_closed=len(unpaid_ids),
              paid_with=method)
        return dict(quote, allowed=False, reason="Кредит погашен досрочно", paid_method=method_detail,
                    payments_closed=len(unpaid_ids))

    return router
