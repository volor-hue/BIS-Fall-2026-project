# payments - payment methods for loan repayments (DEMO: no real money moves).
#
#   cards.py    card checks: Luhn, brand, MM/YY expiry, CVV (the number and CVV are never stored)
#   storage.py  tables payment_cards, kaspi_qr_sessions, payment_transactions
#   qr.py       tiny pure-Python QR generator -> SVG (Kaspi QR demo)
#   service.py  which method pays a repayment (card / PayPal / Kaspi QR / manual)
#   router.py   /api/payment-methods/cards..., /api/repayments/{id}/kaspi-qr
from app.payments import storage
from app.payments.service import PayIn, choose_method

# The routes (app/payments/router.py) are NOT imported here on purpose: app/services/loans.py
# imports this package, and the router depends on the API layer. Only app/main.py imports it.
__all__ = ["storage", "PayIn", "choose_method"]
