# app/payments/router.py
# ------------------------------------------------------------
# Web layer of the payment methods (FastAPI). app/main.py adds it with app.include_router(...).
#
#   GET    /api/payment-methods/cards                  my saved cards (default first)
#   POST   /api/payment-methods/cards                  add a card {holder, number, expiry "MM/YY", cvv, make_default}
#   POST   /api/payment-methods/cards/{card_id}/default make this card the default one
#   DELETE /api/payment-methods/cards/{card_id}         delete a card (the next one becomes default)
#   POST   /api/repayments/{payment_id}/kaspi-qr        a one-time Kaspi QR (SVG) for this repayment
#
# Paying itself is in app/api/loans.py + app/services/loans.py: POST /api/repayments/{payment_id}/pay with an optional body
# {"method": "card"|"paypal"|"kaspi_qr", ...} (see service.py). Only clients have cards.
# ------------------------------------------------------------
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.deps import ensure_own_client, require_user
from app.core.logs import audit
from app.core.rate_limit import SUBMIT_LIMIT, limiter
from app.db import database
from app.payments import cards, qr, service, storage


class CardIn(BaseModel):
    # Plain strings: the checks are in cards.validate_card, so the error messages are clear and
    # the card number is never echoed back in a validation error.
    holder: str = ""
    number: str = ""
    expiry: str = ""
    cvv: str = ""
    make_default: bool = False


def create_router():
    """The payment-method routes. Login checks come from app/api/deps.py."""
    router = APIRouter()

    def client_only(user):
        if user["role"] != "client":
            raise HTTPException(403, "Only clients have saved payment cards")
        return user["uid"]

    @router.get("/api/payment-methods/cards", tags=["payments"])
    def api_list_cards(user: dict = Depends(require_user)):
        return storage.list_cards(client_only(user))

    @router.post("/api/payment-methods/cards", tags=["payments"], status_code=201)
    @limiter.limit(SUBMIT_LIMIT)
    def api_add_card(body: CardIn, request: Request, user: dict = Depends(require_user)):
        client_id = client_only(user)
        try:
            card = cards.validate_card(body.holder, body.number, body.expiry, body.cvv)
        except cards.CardError as error:
            # Only the reason is logged, never the number or the CVV.
            audit("card_add_failed", request, logging.WARNING, client_id=client_id, reason=str(error))
            raise HTTPException(400, str(error))
        if storage.find_same_card(client_id, card):
            raise HTTPException(409, "Эта карта уже добавлена")
        saved = storage.add_card(client_id, card, body.make_default)
        audit("card_added", request, client_id=client_id, card_id=saved["card_id"],
              brand=saved["brand"], last4=saved["last4"], default=saved["is_default"])
        return saved

    @router.post("/api/payment-methods/cards/{card_id}/default", tags=["payments"])
    def api_default_card(card_id: int, request: Request, user: dict = Depends(require_user)):
        client_id = client_only(user)
        if not storage.set_default(client_id, card_id):
            raise HTTPException(404, "Card not found")
        audit("card_default_set", request, client_id=client_id, card_id=card_id)
        return storage.list_cards(client_id)

    @router.delete("/api/payment-methods/cards/{card_id}", tags=["payments"])
    def api_delete_card(card_id: int, request: Request, user: dict = Depends(require_user)):
        client_id = client_only(user)
        if not storage.delete_card(client_id, card_id):
            raise HTTPException(404, "Card not found")
        audit("card_deleted", request, client_id=client_id, card_id=card_id)
        return storage.list_cards(client_id)

    @router.post("/api/repayments/{payment_id}/kaspi-qr", tags=["payments"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_kaspi_qr(payment_id: int, request: Request, user: dict = Depends(require_user)):
        payment = database.get_repayment(payment_id)
        if payment is None:
            raise HTTPException(404, "Payment not found")
        client_id = database.get_loan(payment["loan_id"])["client_id"]
        ensure_own_client(user, client_id)
        if user["role"] != "client":
            raise HTTPException(403, "Kaspi QR is only for the client who owns the loan")
        if payment["status"] == "Paid":
            raise HTTPException(400, "This payment is already paid")
        amount = float(payment["amount_due"])
        ref = storage.create_qr_session(payment_id, client_id, amount, service.QR_MINUTES)
        text = service.kaspi_qr_text(payment_id, amount, ref)
        audit("kaspi_qr_created", request, client_id=client_id, payment_id=payment_id, amount=amount)
        return {"qr_ref": ref, "amount": amount, "qr_text": text, "svg": qr.svg(text),
                "expires_in": service.QR_MINUTES * 60}

    return router


router = create_router()
