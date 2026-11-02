# approval.py
# ------------------------------------------------------------
# Business rules of our Microfinance IS (no database, no web code here,
# so they are easy to explain and to test with pytest):
#   1. decide_application() - the credit approval rule
#   2. run_checks()          - automatic checks saved to client_checks
#   3. build_schedule()      - monthly repayment schedule for a new loan
# ------------------------------------------------------------
from datetime import date

# Loans up to this amount (in KZT) are decided automatically.
AUTO_LIMIT_KZT = 500000

# A client may spend at most 40% of monthly income on the monthly payment.
MAX_PAYMENT_SHARE = 0.4

# Interest rate for every new loan, % per year (prototype: one fixed rate).
INTEREST_RATE = 24.0


def decide_application(monthly_income, requested_amount, term_months):
    """
    Decide what happens with a new loan application.
    monthly_income = SUM of all the client's jobs (employment table).

    Returns a pair (status, approval_type):
        ("Approved", "Auto") / ("Rejected", "Auto") / ("Pending Admin Review", "Manual")
    """
    # STEP 1. Big loans (more than 500 000 KZT) are NEVER approved automatically.
    # If the condition is True we "return" immediately and stop here.
    if requested_amount > AUTO_LIMIT_KZT:
        return "Pending Admin Review", "Manual"

    # STEP 2. Small loans: simple monthly payment = amount / months (no interest
    # in the rule itself, as agreed in the requirements).
    monthly_payment = requested_amount / term_months
    max_affordable_payment = monthly_income * MAX_PAYMENT_SHARE

    # STEP 3. Approve if 40% of income covers the monthly payment.
    if max_affordable_payment >= monthly_payment:
        return "Approved", "Auto"
    else:
        return "Rejected", "Auto"


def age_in_years(birth_date_text, today):
    # birth_date_text looks like "2005-01-31". Returns None if it is empty/wrong.
    try:
        born = date.fromisoformat(birth_date_text)
    except (TypeError, ValueError):
        return None
    years = today.year - born.year
    # If the birthday has not happened yet this year, subtract 1.
    if (today.month, today.day) < (born.month, born.day):
        years = years - 1
    return years


def run_checks(iin, birth_date, monthly_income, requested_amount, term_months, today=None):
    """
    Automatic checks for one application. Returns a list of (check_type, result_status).
    They are saved in the client_checks table so an employee can see WHY a decision was made.
    NOTE: in this prototype the checks are informational; the decision itself is made by
    decide_application(). (Making AGE_18 a hard rule is a possible next requirement.)
    """
    if today is None:
        today = date.today()
    checks = []

    # IIN_FORMAT: exactly 12 digits.
    if iin is not None and len(iin) == 12 and iin.isdigit():
        checks.append(("IIN_FORMAT", "PASS"))
    else:
        checks.append(("IIN_FORMAT", "FAIL"))

    # AGE_18: the client must be at least 18 years old.
    age = age_in_years(birth_date, today)
    if age is not None and age >= 18:
        checks.append(("AGE_18", "PASS"))
    else:
        checks.append(("AGE_18", "FAIL"))

    # INCOME_DTI: "debt-to-income" 40% rule.
    if monthly_income * MAX_PAYMENT_SHARE >= requested_amount / term_months:
        checks.append(("INCOME_DTI", "PASS"))
    else:
        checks.append(("INCOME_DTI", "FAIL"))

    # AMOUNT_LIMIT: above 500 000 an employee must decide.
    if requested_amount <= AUTO_LIMIT_KZT:
        checks.append(("AMOUNT_LIMIT", "PASS"))
    else:
        checks.append(("AMOUNT_LIMIT", "MANUAL_REVIEW"))

    return checks


def add_months(start, months):
    # Move a date forward by N months. Day is limited to 28 so that
    # every month has that day (simple and good enough for a prototype).
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(start.day, 28))


def annuity_payment(amount, annual_rate_percent, term_months):
    # Annuity = the SAME payment every month (principal + interest).
    # Formula: P = S * r / (1 - (1 + r) ** -n)
    #   S = loan amount, n = number of months, r = monthly rate (24% / 12 / 100 = 0.02)
    r = annual_rate_percent / 12 / 100
    if r == 0:
        return amount / term_months
    return amount * r / (1 - (1 + r) ** (-term_months))


def build_schedule(amount, annual_rate_percent, term_months, issue_date):
    """Returns a list of (due_date 'YYYY-MM-DD', amount_due), one row per month."""
    payment = round(annuity_payment(amount, annual_rate_percent, term_months), 2)
    schedule = []
    for month_number in range(1, term_months + 1):
        due = add_months(issue_date, month_number)
        schedule.append((due.isoformat(), payment))
    return schedule
