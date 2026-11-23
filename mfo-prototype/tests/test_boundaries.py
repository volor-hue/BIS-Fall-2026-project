# tests/test_boundaries.py
# Boundary-value and negative tests for the business rules in approval.py.
# Pure logic: no database, no web server. Run with:  pytest tests/test_boundaries.py
# Test case IDs (BV-xx) match docs/TEST_PLAN.md.
from datetime import date

import pytest

from approval import (AUTO_LIMIT_KZT, add_months, age_in_years, annuity_payment,
                      build_schedule, decide_application, run_checks)

TODAY = date(2026, 11, 2)


# ---------- BV-01..03: the 500 000 KZT limit (auto / manual) ----------
@pytest.mark.parametrize("amount, expected", [
    (499999.99, ("Approved", "Auto")),
    (500000, ("Approved", "Auto")),                 # the limit itself is still automatic
    (500000.01, ("Pending Admin Review", "Manual")),  # one tiyn above -> employee decides
    (500001, ("Pending Admin Review", "Manual")),
])
def test_bv_auto_limit(amount, expected):
    assert AUTO_LIMIT_KZT == 500000
    assert decide_application(10_000_000, amount, 12) == expected


# ---------- BV-04..06: the 40% income rule ----------
@pytest.mark.parametrize("income, expected", [
    (100000, ("Approved", "Auto")),   # payment 40 000 == 40% of 100 000
    (99999, ("Rejected", "Auto")),    # 40% = 39 999.6 < 40 000
    (100001, ("Approved", "Auto")),
])
def test_bv_dti_boundary(income, expected):
    assert decide_application(income, 120000, 3) == expected


# ---------- BV-07..08: loan term limits ----------
def test_bv_one_month_term_means_full_amount_at_once():
    # 100 000 in 1 month needs income >= 250 000 (40% rule)
    assert decide_application(250000, 100000, 1) == ("Approved", "Auto")
    assert decide_application(249999, 100000, 1) == ("Rejected", "Auto")


def test_bv_max_term_120_months():
    # 500 000 / 120 = 4 166.67 per month -> income 10 417 is enough (40% = 4 166.8)
    assert decide_application(10417, 500000, 120) == ("Approved", "Auto")
    assert decide_application(10416, 500000, 120) == ("Rejected", "Auto")


def test_negative_zero_term_raises():
    # The API blocks term_months=0 (Pydantic), but the function itself must not
    # silently accept it: it divides by term_months.
    with pytest.raises(ZeroDivisionError):
        decide_application(300000, 100000, 0)


# ---------- BV-09..13: age 18 ----------
@pytest.mark.parametrize("birth, today, expected_age", [
    ("2008-11-02", date(2026, 11, 2), 18),  # 18th birthday is TODAY -> already 18
    ("2008-11-03", date(2026, 11, 2), 17),  # birthday is tomorrow -> still 17
    ("2008-11-01", date(2026, 11, 2), 18),  # birthday was yesterday
    ("2008-02-29", date(2026, 2, 28), 17),  # leap-day birth, 28 Feb: not yet
    ("2008-02-29", date(2026, 3, 1), 18),   # leap-day birth, 1 Mar: 18
])
def test_bv_age_in_years(birth, today, expected_age):
    assert age_in_years(birth, today) == expected_age


@pytest.mark.parametrize("birth, expected", [
    ("2008-11-02", "PASS"),
    ("2008-11-03", "FAIL"),
])
def test_bv_age_18_check(birth, expected):
    checks = dict(run_checks("050101500123", birth, 300000, 100000, 12, today=TODAY))
    assert checks["AGE_18"] == expected


@pytest.mark.parametrize("bad_birth", ["", None, "not-a-date", "2005-13-45", "01.01.2005"])
def test_negative_invalid_birth_date_fails_age_check(bad_birth):
    assert age_in_years(bad_birth, TODAY) is None
    checks = dict(run_checks("050101500123", bad_birth, 300000, 100000, 12, today=TODAY))
    assert checks["AGE_18"] == "FAIL"


# ---------- BV-14..18: IIN format ----------
@pytest.mark.parametrize("iin, expected", [
    ("050101500123", "PASS"),    # 12 digits
    ("05010150012", "FAIL"),     # 11 digits
    ("0501015001234", "FAIL"),   # 13 digits
    ("05010150012a", "FAIL"),    # letter inside
    ("", "FAIL"),
    (None, "FAIL"),
    ("0501 1500123", "FAIL"),    # space inside
])
def test_bv_iin_format(iin, expected):
    checks = dict(run_checks(iin, "2000-01-01", 300000, 100000, 12, today=TODAY))
    assert checks["IIN_FORMAT"] == expected


# ---------- BV-19: AMOUNT_LIMIT check agrees with decide_application ----------
@pytest.mark.parametrize("amount, expected", [
    (500000, "PASS"), (500000.01, "MANUAL_REVIEW"),
])
def test_bv_amount_limit_check_matches_decision_rule(amount, expected):
    checks = dict(run_checks("050101500123", "2000-01-01", 10_000_000, amount, 12, today=TODAY))
    assert checks["AMOUNT_LIMIT"] == expected


def test_checks_always_return_the_four_types_in_order():
    checks = run_checks("050101500123", "2000-01-01", 300000, 100000, 12, today=TODAY)
    assert [name for name, _ in checks] == ["IIN_FORMAT", "AGE_18", "INCOME_DTI", "AMOUNT_LIMIT"]


# ---------- BV-20..26: repayment schedule ----------
def test_schedule_single_month():
    schedule = build_schedule(100000, 24.0, 1, TODAY)
    assert schedule == [("2026-12-02", 102000.0)]  # amount + one month of 2%


def test_schedule_crosses_year_boundary():
    schedule = build_schedule(120000, 0, 3, date(2026, 11, 15))
    assert [d for d, _ in schedule] == ["2026-12-15", "2027-01-15", "2027-02-15"]


def test_schedule_december_issue_goes_to_next_january():
    assert add_months(date(2026, 12, 10), 1) == date(2027, 1, 10)
    assert add_months(date(2026, 12, 10), 12) == date(2027, 12, 10)


@pytest.mark.parametrize("day", [29, 30, 31])
def test_schedule_day_is_limited_to_28(day):
    # prototype rule from approval.add_months: day > 28 becomes 28
    assert add_months(date(2026, 1, day), 1) == date(2026, 2, 28)


def test_schedule_due_dates_strictly_increase():
    dates = [d for d, _ in build_schedule(300000, 24.0, 120, TODAY)]
    assert len(dates) == 120 and dates == sorted(set(dates))


def test_schedule_payment_is_rounded_to_two_decimals():
    for _, payment in build_schedule(333333, 24.0, 7, TODAY):
        assert payment == round(payment, 2)


def test_schedule_total_is_close_to_exact_annuity():
    # rounding the payment to 2 decimals may shift the total by at most n * 0.005
    n = 36
    exact_total = annuity_payment(250000, 24.0, n) * n
    total = sum(p for _, p in build_schedule(250000, 24.0, n, TODAY))
    assert abs(total - exact_total) <= n * 0.005 + 1e-6


def test_schedule_interest_is_positive_for_positive_rate():
    assert sum(p for _, p in build_schedule(100000, 24.0, 12, TODAY)) > 100000
