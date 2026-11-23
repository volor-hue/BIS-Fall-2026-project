# repayment_rules - WHAT the client pays and WHEN (overdue, penalty, early payoff).
# app/payments/ is HOW the client pays (card / PayPal / Kaspi QR). See docs/REPAYMENT_RULES.md.
#
#   rules.py    pure business rules BR-1..BR-5 (no database, no web)
#   storage.py  tables repayment_penalties, early_payoffs, early_payoff_items
#   service.py  enrich(): rule fields (Overdue, penalty...) for the repayment schedule
#   router.py   GET/POST /api/loans/{id}/early-payoff
#
# The router is NOT imported here: only app/main.py imports it, so the services and
# app/payments/ can use the rules without importing the API layer.
from app.repayment_rules import rules, service, storage

__all__ = ["rules", "service", "storage"]
