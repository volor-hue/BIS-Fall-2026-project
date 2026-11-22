# payments - payment methods for loan repayments (DEMO: no real money moves).
#
#   cards.py    card checks: Luhn, brand, MM/YY expiry, CVV (the number and CVV are never stored)
#   storage.py  tables payment_cards, kaspi_qr_sessions, payment_transactions
#   qr.py       tiny pure-Python QR generator -> SVG (Kaspi QR demo)
#   service.py  which method pays a repayment (card / PayPal / Kaspi QR / manual)
#   router.py   /api/payment-methods/cards..., /api/repayments/{id}/kaspi-qr
from payments.router import create_router
from payments import storage
from payments.service import PayIn, choose_method

__all__ = ["create_router", "PayIn", "choose_method"]
