# repayment_rules - WHAT the client pays and WHEN (overdue, penalty, early payoff).
# payments/ is HOW the client pays (card / PayPal / Kaspi QR). See docs/REPAYMENT_RULES.md.
#
#   rules.py    pure business rules BR-1..BR-5 (no database, no web)
#   storage.py  tables repayment_penalties, early_payoffs, early_payoff_items
#   router.py   GET/POST /api/loans/{id}/early-payoff, enrich() for the repayment schedule
#
# main.py imports the router as "from repayment_rules.router import ..." (not here),
# so payments/ can use rules.py without an import cycle.
from repayment_rules import rules, storage

__all__ = ["rules", "storage"]
