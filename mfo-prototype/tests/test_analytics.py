# tests/test_analytics.py
# Tests for analytics.portfolio_report(). They use a fresh temporary database and a fixed
# "today", so the numbers are the same on every run. Test case IDs (AN-xx) match docs/TEST_PLAN.md.
from datetime import date

import pytest

import analytics
import database

TODAY = date(2026, 11, 9)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "analytics.db"))
    database.init_db()
    return database


def add_client(db):
    client_id = db.create_client("050101500123", "Test Client", "2000-01-01", "+77010000000",
                                 "Almaty", "a@test.kz", "pass123", "Single", "2026-01-01 10:00:00")
    db.create_employment(client_id, "Kaspi", "Analyst", 300000)
    return client_id


def add_application(db, client_id, amount, status, approval_type):
    return db.create_application(client_id, amount, 12, "test", status, approval_type,
                                 "2026-08-01 10:00:00")


def add_loan(db, application_id, amount, schedule):
    return db.create_loan_with_schedule(application_id, amount, 24.0, "2026-08-01", schedule)


def filled_portfolio(db):
    """4 applications, 2 loans. Expected numbers are written in the tests below."""
    cid = add_client(db)
    a1 = add_application(db, cid, 400000, "Approved", "Auto")
    a2 = add_application(db, cid, 1000000, "Approved", "Manual")
    add_application(db, cid, 400000, "Rejected", "Auto")
    add_application(db, cid, 900000, "Pending Admin Review", "Manual")

    # Loan 1: four payments of 100. Today is 2026-11-09.
    #   09-01 paid; 10-01 unpaid -> OVERDUE; 11-09 is due TODAY -> not overdue yet; 12-01 future.
    loan1 = add_loan(db, a1, 400000, [("2026-09-01", 100), ("2026-10-01", 100),
                                      ("2026-11-09", 100), ("2026-12-01", 100)])
    db.mark_repayment_paid(db.get_repayments(loan1)[0]["payment_id"])
    # Loan 2: two payments of 200. 10-15 unpaid -> OVERDUE; 2027-01-15 future.
    add_loan(db, a2, 1000000, [("2026-10-15", 200), ("2027-01-15", 200)])


# AN-01: an empty database must not crash (division by zero) and gives zeros.
def test_empty_portfolio_is_all_zeros(db):
    report = analytics.portfolio_report(TODAY)
    assert report["applications"]["total"] == 0
    assert report["applications"]["approval_rate"] == 0.0
    assert report["loans"] == {"count": 0, "total_issued": 0.0, "average_amount": 0.0}
    assert report["repayments"]["overdue_count"] == 0
    assert report["repayments"]["overdue_rate"] == 0.0


# AN-02: application counters
def test_application_counts_and_approval_rate(db):
    filled_portfolio(db)
    apps = analytics.portfolio_report(TODAY)["applications"]
    assert apps["total"] == 4
    assert (apps["approved"], apps["rejected"], apps["pending_review"]) == (2, 1, 1)
    assert (apps["auto"], apps["manual"]) == (2, 2)
    # Pending is not a decision: 2 approved out of 3 decided = 0.6667
    assert apps["approval_rate"] == pytest.approx(0.6667, abs=0.0001)


# AN-03: loan totals
def test_loan_count_sum_and_average(db):
    filled_portfolio(db)
    loans = analytics.portfolio_report(TODAY)["loans"]
    assert loans["count"] == 2
    assert loans["total_issued"] == 1400000
    assert loans["average_amount"] == 700000


# AN-04: money flow
def test_repayment_amounts(db):
    filled_portfolio(db)
    rep = analytics.portfolio_report(TODAY)["repayments"]
    assert rep["scheduled_amount"] == 800      # 4 * 100 + 2 * 200
    assert rep["collected_amount"] == 100      # one paid payment
    assert rep["outstanding_amount"] == 700
    assert rep["collected_amount"] + rep["outstanding_amount"] == rep["scheduled_amount"]


# AN-05: overdue = Pending AND due_date < today
def test_overdue_counts_only_unpaid_past_due(db):
    filled_portfolio(db)
    rep = analytics.portfolio_report(TODAY)["repayments"]
    assert rep["overdue_count"] == 2           # loan1 10-01 and loan2 10-15
    assert rep["overdue_amount"] == 300
    assert rep["overdue_loans"] == 2
    assert rep["overdue_rate"] == pytest.approx(0.4286, abs=0.0001)  # 300 / 700


# AN-06: boundary: a payment due exactly today is NOT overdue; tomorrow it is.
def test_due_today_is_not_overdue_but_is_tomorrow(db):
    filled_portfolio(db)
    on_due_day = analytics.portfolio_report(date(2026, 11, 9))["repayments"]
    next_day = analytics.portfolio_report(date(2026, 11, 10))["repayments"]
    assert on_due_day["overdue_count"] == 2
    assert next_day["overdue_count"] == 3


# AN-07: a paid payment is never overdue, however old it is
def test_paid_old_payment_is_not_overdue(db):
    filled_portfolio(db)
    far_future = analytics.portfolio_report(date(2030, 1, 1))["repayments"]
    # Unpaid payments: 3 on loan 1 + 2 on loan 2 = 5 (300 + 400 = 700 KZT).
    # The one paid payment (100) must stay out, even though its date is in the past.
    assert far_future["overdue_count"] == 5
    assert far_future["overdue_amount"] == 700
    assert far_future["overdue_rate"] == 1.0


# AN-08: paying an overdue payment removes it from the overdue numbers
def test_paying_overdue_payment_updates_report(db):
    filled_portfolio(db)
    conn = db.get_connection()
    payment_id = conn.execute(
        "SELECT payment_id FROM repayments WHERE due_date = '2026-10-15'").fetchone()[0]
    conn.close()
    db.mark_repayment_paid(payment_id)
    rep = analytics.portfolio_report(TODAY)["repayments"]
    assert rep["overdue_count"] == 1
    assert rep["overdue_amount"] == 100
    assert rep["overdue_loans"] == 1
    assert rep["collected_amount"] == 300


# AN-09: the report only reads data
def test_report_does_not_change_the_database(db):
    filled_portfolio(db)
    first = analytics.portfolio_report(TODAY)
    second = analytics.portfolio_report(TODAY)
    assert first == second


# AN-10: the console version runs and prints the key lines
def test_print_report_output(db, capsys):
    filled_portfolio(db)
    analytics.print_report(analytics.portfolio_report(TODAY))
    out = capsys.readouterr().out
    assert "as of 2026-11-09" in out
    assert "Approval rate: 66.7%" in out
    assert "Overdue: 2 payments on 2 loans" in out
