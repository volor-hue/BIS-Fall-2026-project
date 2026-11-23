# app/payments/service.py
# ------------------------------------------------------------
# "Which method pays this repayment?" - checked BEFORE the repayment is marked Paid.
#
#   card      - a saved card of the client (card_id, or the DEFAULT card if card_id is empty)
#   paypal    - demo: PayPal e-mail (no real PayPal call, the page simulates the redirect)
#   kaspi_qr  - demo: the client got a QR from POST /api/repayments/{id}/kaspi-qr and pressed
#               "Я оплатил"; the one-time QR reference must match this repayment
#   manual    - no method sent (old API calls, an employee marking a payment at the office)
#
# All of this is a DEMO: no money moves, no bank/PayPal/Kaspi API is called.
# ------------------------------------------------------------
import re
from datetime import date
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel

from app.payments import cards, storage

METHODS = ("card", "paypal", "kaspi_qr", "manual")
QR_MINUTES = 10
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


class PayIn(BaseModel):
    method: Optional[str] = None          # card / paypal / kaspi_qr / manual (empty = manual)
    card_id: Optional[int] = None         # card: empty = the default card
    paypal_email: Optional[str] = None    # paypal
    qr_ref: Optional[str] = None          # kaspi_qr: from POST /api/repayments/{id}/kaspi-qr


def kaspi_qr_text(payment_id, amount, ref):
    # What the QR contains. A demo link in the style of Kaspi payment links.
    return f"https://kaspi.kz/pay/MFO-DEMO?payment={payment_id}&amount={amount:.2f}&ref={ref}"


def choose_method(user, client_id, payment_id, body):
    """Check the chosen method -> (method, detail for the history). Raises HTTPException if wrong.
    For kaspi_qr this USES UP the one-time QR, so call it right before marking the payment Paid."""
    method = (body.method if body and body.method else "manual").strip().lower()
    if method not in METHODS:
        raise HTTPException(400, "Unknown payment method. Use: " + ", ".join(METHODS))
    if method == "manual":
        return "manual", "Manual (no online method)"
    if user["role"] != "client" or user["uid"] != client_id:
        raise HTTPException(403, "Online payment methods are only for the client who owns the loan")

    if method == "card":
        card = (storage.get_card(client_id, body.card_id) if body.card_id is not None
                else storage.get_default_card(client_id))
        if card is None:
            raise HTTPException(400, "Card not found. Add a card or choose another method.")
        today = date.today()
        if (card["exp_year"], card["exp_month"]) < (today.year, today.month):
            raise HTTPException(400, "This card has expired. Choose another card.")
        return "card", card["title"]

    if method == "paypal":
        email = (body.paypal_email or "").strip()
        if not EMAIL_RE.match(email):
            raise HTTPException(400, "Enter the PayPal e-mail")
        return "paypal", "PayPal " + cards.mask_email(email)

    # kaspi_qr
    if not storage.consume_qr_session(body.qr_ref, payment_id, client_id):
        raise HTTPException(400, "Kaspi QR is unknown, expired or already used. Show a new QR.")
    return "kaspi_qr", "Kaspi QR"
