# app/repayment_rules/service.py
# ------------------------------------------------------------
# Repayment rules applied to stored rows (no web code). Used by app/services/loans.py
# for GET /api/loans/{id}/repayments, so the business layer never imports the router.
# ------------------------------------------------------------
from app.repayment_rules import rules, storage


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
