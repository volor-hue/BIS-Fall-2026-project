# tests/test_approval.py
# Unit tests for the business rules in approval.py. Run with:  pytest
from datetime import date

from approval import build_schedule, decide_application, run_checks


def test_small_loan_affordable_is_auto_approved():
    # 400 000 / 12 = 33 333 per month; 40% of 300 000 = 120 000 -> enough
    assert decide_application(300000, 400000, 12) == ("Approved", "Auto")


def test_small_loan_not_affordable_is_auto_rejected():
    # 400 000 / 1 = 400 000 per month; 120 000 is not enough
    assert decide_application(300000, 400000, 1) == ("Rejected", "Auto")


def test_exactly_500000_is_still_automatic():
    assert decide_application(1000000, 500000, 10) == ("Approved", "Auto")


def test_big_loan_goes_to_employee():
    assert decide_application(300000, 1000000, 12) == ("Pending Admin Review", "Manual")


def test_big_loan_goes_to_employee_even_with_huge_income():
    assert decide_application(100000000, 500001, 12) == ("Pending Admin Review", "Manual")


def test_payment_exactly_40_percent_is_approved():
    # 40% of 100 000 = 40 000; 120 000 / 3 = 40 000 -> equal means approved (>=)
    assert decide_application(100000, 120000, 3) == ("Approved", "Auto")


def test_zero_income_is_rejected():
    assert decide_application(0, 100000, 12) == ("Rejected", "Auto")


def test_checks_all_pass():
    checks = run_checks("050101500123", "2005-01-01", 300000, 400000, 12, today=date(2026, 11, 2))
    assert checks == [("IIN_FORMAT", "PASS"), ("AGE_18", "PASS"),
                      ("INCOME_DTI", "PASS"), ("AMOUNT_LIMIT", "PASS")]


def test_checks_detect_problems():
    # 17-year-old, bad IIN, payment too high, amount above the limit
    checks = dict(run_checks("12345", "2009-12-01", 100000, 1000000, 2, today=date(2026, 11, 2)))
    assert checks["IIN_FORMAT"] == "FAIL"
    assert checks["AGE_18"] == "FAIL"
    assert checks["INCOME_DTI"] == "FAIL"
    assert checks["AMOUNT_LIMIT"] == "MANUAL_REVIEW"


def test_schedule_has_one_row_per_month():
    schedule = build_schedule(400000, 24.0, 12, date(2026, 11, 2))
    assert len(schedule) == 12
    assert schedule[0][0] == "2026-12-02"
    assert schedule[-1][0] == "2027-11-02"
    # Annuity: 12 equal payments, total is more than the amount (interest)
    assert schedule[0][1] == schedule[-1][1] == 37823.84
    assert sum(p for _, p in schedule) > 400000


def test_schedule_with_zero_rate_is_equal_parts():
    schedule = build_schedule(120000, 0, 3, date(2026, 1, 31))
    assert [p for _, p in schedule] == [40000, 40000, 40000]
    assert schedule[0][0] == "2026-02-28"  # day limited to 28
