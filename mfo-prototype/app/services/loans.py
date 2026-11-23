# app/services/loans.py
# ------------------------------------------------------------
# The life of a loan: application -> decision -> loan with a schedule -> repayments.
# The rules themselves (approve or not, schedule math) are in approval.py;
# this module applies them and saves the results through the database layer.
# ------------------------------------------------------------
from datetime import date, datetime

from app import payments, repayment_rules
from app.core.logs import audit
from app.db import database
from app.services.approval import INTEREST_RATE, build_schedule, decide_application, run_checks
from app.services.errors import NotFoundError, RuleError


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def issue_loan(application):
    # Called when an application becomes "Approved" (auto or by an employee).
    # The application "becomes" a loan, and the loan gets its monthly schedule.
    today = date.today()
    schedule = build_schedule(application["requested_amount"], INTEREST_RATE,
                              application["term_months"], today)
    loan_id = database.create_loan_with_schedule(application["application_id"],
                                                 application["requested_amount"],
                                                 INTEREST_RATE, today.isoformat(), schedule)
    audit("loan_issued", loan_id=loan_id, application_id=application["application_id"],
          amount=application["requested_amount"], term_months=application["term_months"])
    return loan_id


def submit_application(client_id, requested_amount, term_months, purpose):
    client = database.get_client(client_id)
    if client is None:
        raise NotFoundError("Client not found")

    # Income = SUM of monthly_income over ALL jobs in the employment table.
    income = database.get_total_income(client_id)

    # The business rule (approval.py) decides the status.
    status, approval_type = decide_application(income, requested_amount, term_months)
    app_id = database.create_application(client_id, requested_amount, term_months,
                                         purpose, status, approval_type, now_text())

    # Save the automatic checks for this application.
    checks = run_checks(client["iin"], client["birth_date"], income, requested_amount, term_months)
    database.create_checks(app_id, checks, now_text())

    application = database.get_application(app_id)
    # Only an APPROVED application becomes a loan.
    if status == "Approved":
        application["loan_id"] = issue_loan(application)
    return application


def decide(application_id, employee_id, decision):
    """An employee approves or rejects a MANUAL application. decision: "approve" | "reject"."""
    application = database.get_application(application_id)
    if application is None:
        raise NotFoundError("Application not found")
    # Only MANUAL applications that are still waiting can be decided by an employee.
    if application["approval_type"] != "Manual" or application["status"] != "Pending Admin Review":
        raise RuleError("Only pending manual applications can be decided")

    new_status = "Approved" if decision == "approve" else "Rejected"
    # Save the decision AND who made it (employee_id).
    database.set_application_decision(application_id, new_status, employee_id)
    application = database.get_application(application_id)
    if new_status == "Approved":
        application["loan_id"] = issue_loan(application)
    return application


def repayments_with_methods(loan_id):
    rows = database.get_repayments(loan_id)
    # Added field: how a paid repayment was paid (card •••• 4242 / PayPal / Kaspi QR), see app/payments/.
    methods = payments.storage.methods_for_loan(loan_id)
    for row in rows:
        row["paid_method"] = methods.get(row["payment_id"])
    # Added fields: Overdue status, days of delay, penalty, total to pay, closed early (app/repayment_rules/).
    return repayment_rules.service.enrich(loan_id, rows)


def pay(payment, client_id, user, body):
    """Mark one repayment Paid. `body` = optional payment window choice (card / PayPal / Kaspi QR).
    An overdue repayment is paid together with its penalty (app/repayment_rules/rules.py, BR-1..BR-3).
    Returns (repayment, method, charge), e.g. method "card", charge {"penalty": ..., "total_to_pay": ...}."""
    if payment["status"] == "Paid":
        raise RuleError("This payment is already paid")
    payment_id = payment["payment_id"]
    charge = repayment_rules.rules.charge(payment)
    method, method_detail = payments.choose_method(user, client_id, payment_id, body)
    database.mark_repayment_paid(payment_id)
    if charge["penalty"] > 0:
        repayment_rules.storage.record_penalty(payment_id, charge["days_overdue"], charge["penalty"])
    payments.storage.record_transaction(payment_id, client_id, method, method_detail,
                                        charge["total_to_pay"], user["role"])
    result = database.get_repayment(payment_id)
    result["paid_method"] = method_detail
    result.update(days_overdue=charge["days_overdue"], penalty=charge["penalty"],
                  total_paid=charge["total_to_pay"])
    return result, method, charge
