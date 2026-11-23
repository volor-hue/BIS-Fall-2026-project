# app/repayment_rules/rules.py
# ------------------------------------------------------------
# Business rules of repayment: WHAT the client must pay and WHEN.
# (app/payments/ answers a different question: HOW the client pays - card / PayPal / Kaspi QR.)
#
#   BR-1  Overdue:   a repayment that is not paid after its due date is "Overdue".
#   BR-2  Penalty:   0.5% of the overdue payment for every day of delay,
#                    but never more than 50% of that payment (the debt cannot grow forever).
#   BR-3  Pay with penalty: an overdue repayment is paid together with its penalty.
#   BR-4  Early payoff: the client can close the whole loan at once by paying only the
#                    remaining principal (no interest for the future months, no fee).
#   BR-5  Early payoff is NOT allowed while the loan has overdue payments (pay them first).
#
# Everything here is pure Python (no database, no web), so it is easy to test and to explain.
# The status "Overdue" is NOT stored in the database: it is calculated from due_date and today.
# So the old 'Pending' / 'Paid' values and the teammates' code stay exactly as they were.
# ------------------------------------------------------------
from datetime import date

from app.core import config

PENALTY_RATE_PER_DAY = 0.005      # BR-2: 0.5% per day
PENALTY_CAP_SHARE = 0.50          # BR-2: at most 50% of the overdue payment

PAID, PENDING, OVERDUE = "Paid", "Pending", "Overdue"


def today():
    # The "current date" of the system. For the defense demo it can be moved forward:
    #   set MFO_TODAY=2027-03-15  -> the app behaves as if today were 15.03.2027.
    # The setting is read in app/core/config.py; a wrong value falls back to the real date.
    return config.demo_today() or date.today()


def as_date(value):
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def days_overdue(due_date, on_day):
    # 0 on the due date itself; 1 the day after, and so on.
    return max(0, (as_date(on_day) - as_date(due_date)).days)


def penalty_for(amount_due, days):
    # BR-2: amount * 0.5% * days, capped at 50% of the amount. Rounded to tiyn (2 digits).
    amount = float(amount_due)
    return round(min(amount * PENALTY_RATE_PER_DAY * days, amount * PENALTY_CAP_SHARE), 2)


def status_of(repayment, on_day):
    # BR-1: Paid stays Paid; an unpaid payment after its due date is Overdue.
    if repayment["status"] == PAID:
        return PAID
    return OVERDUE if days_overdue(repayment["due_date"], on_day) > 0 else PENDING


def charge(repayment, on_day=None):
    """What the client has to pay for one repayment today (BR-1..BR-3)."""
    on_day = on_day or today()
    status = status_of(repayment, on_day)
    days = days_overdue(repayment["due_date"], on_day) if status == OVERDUE else 0
    penalty = penalty_for(repayment["amount_due"], days)
    return {"display_status": status, "days_overdue": days, "penalty": penalty,
            "total_to_pay": round(float(repayment["amount_due"]) + penalty, 2)}


def total_to_pay(repayment, on_day=None):
    return charge(repayment, on_day)["total_to_pay"]


def remaining_principal(amount, annual_rate_percent, term_months, payments_made):
    # BR-4: how much of the loan body is still owed after k annuity payments.
    #   B_k = S * (1 + r)^k - P * ((1 + r)^k - 1) / r,   r = monthly rate, P = monthly payment
    # (the same annuity formula as approval.build_schedule uses to make the schedule).
    from app.services.approval import annuity_payment

    if payments_made >= term_months:
        return 0.0
    r = annual_rate_percent / 12 / 100
    payment = round(annuity_payment(amount, annual_rate_percent, term_months), 2)
    if r == 0:
        balance = amount - payment * payments_made
    else:
        growth = (1 + r) ** payments_made
        balance = amount * growth - payment * (growth - 1) / r
    return round(max(0.0, balance), 2)


def early_payoff_quote(loan, repayments, on_day=None):
    """BR-4 / BR-5: can the loan be closed today, and for how much."""
    on_day = on_day or today()
    unpaid = [r for r in repayments if r["status"] != PAID]
    overdue = [r for r in unpaid if status_of(r, on_day) == OVERDUE]
    paid_count = len(repayments) - len(unpaid)
    scheduled_left = round(sum(float(r["amount_due"]) for r in unpaid), 2)

    quote = {"loan_id": loan["loan_id"], "unpaid_payments": len(unpaid), "overdue_payments": len(overdue),
             "scheduled_left": scheduled_left, "payoff_amount": 0.0, "saving": 0.0,
             "allowed": False, "reason": None}
    if not unpaid:
        quote["reason"] = "Кредит уже полностью погашен"
        return quote
    if overdue:
        quote["reason"] = "Сначала оплатите просроченные платежи (вместе с пеней)"
        return quote

    principal = remaining_principal(float(loan["approved_amount"]), float(loan["interest_rate"]),
                                    len(repayments), paid_count)
    # Never ask for more than the schedule itself (rounding safety for the last payment).
    principal = min(principal, scheduled_left)
    quote.update(payoff_amount=principal, saving=round(scheduled_left - principal, 2), allowed=True)
    return quote
