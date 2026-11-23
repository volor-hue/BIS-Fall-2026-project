# tests/test_repayment_rules.py
# Repayment rules (repayment_rules/, docs/REPAYMENT_RULES.md):
#   BR-1 overdue status, BR-2 penalty 0.5%/day capped at 50%, BR-3 pay together with the penalty,
#   BR-4 early payoff = remaining principal only, BR-5 no early payoff while something is overdue.
# "Today" is moved forward with MFO_TODAY, the same switch that is used in the defense demo.
from datetime import date, timedelta

import pytest

from conftest import face_token_for

ERLAN = {"iin": "900101300666", "full_name": "Erlan", "birth_date": "1990-01-01", "phone": "+77010000006",
         "email": "erlan@test.kz", "password": "erlan123",
         "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}
OTHER = dict(ERLAN, iin="900101300777", full_name="Other", phone="+77010000007", email="other@test.kz",
             password="other123")
NEXT_YEAR = f"12/{(date.today().year + 1) % 100:02d}"


@pytest.fixture
def erlan(client, as_user):
    # A client with an auto-approved loan of 100 000 ₸ for 12 months at 24%.
    assert client.post("/api/auth/register", json=ERLAN).status_code == 201
    user = as_user(ERLAN["email"], ERLAN["password"])
    me = user.get("/api/auth/me").json()["user_id"]
    app = user.post("/api/applications", json={"client_id": me, "requested_amount": 100000, "term_months": 12,
                                                "face_token": face_token_for(me)}).json()
    user.loan_id = app["loan_id"]
    user.schedule = lambda: user.get(f"/api/loans/{user.loan_id}/repayments").json()
    user.first = user.schedule()[0]
    return user


def go_to(monkeypatch, day):
    monkeypatch.setenv("MFO_TODAY", day.isoformat())


def due(row):
    return date.fromisoformat(row["due_date"])


# ---------- pure rules ----------

def test_penalty_is_half_percent_per_day_with_a_cap():
    from repayment_rules import rules

    assert rules.penalty_for(10000, 0) == 0
    assert rules.penalty_for(10000, 1) == 50.0          # 0.5% of 10 000
    assert rules.penalty_for(10000, 10) == 500.0
    assert rules.penalty_for(10000, 100) == 5000.0      # 50% cap reached
    assert rules.penalty_for(10000, 400) == 5000.0      # ...and it does not grow any more


def test_overdue_starts_the_day_after_the_due_date():
    from repayment_rules import rules

    row = {"status": "Pending", "due_date": "2027-01-10", "amount_due": 9455.96}
    assert rules.status_of(row, date(2027, 1, 9)) == "Pending"
    assert rules.status_of(row, date(2027, 1, 10)) == "Pending"     # due date itself is not late
    assert rules.status_of(row, date(2027, 1, 11)) == "Overdue"
    assert rules.charge(row, date(2027, 1, 13)) == {"display_status": "Overdue", "days_overdue": 3,
                                                     "penalty": 141.84, "total_to_pay": 9597.8}
    assert rules.status_of(dict(row, status="Paid"), date(2030, 1, 1)) == "Paid"


def test_remaining_principal_follows_the_annuity():
    from approval import annuity_payment
    from repayment_rules import rules

    assert rules.remaining_principal(100000, 24, 12, 0) == 100000
    assert rules.remaining_principal(100000, 24, 12, 12) == 0
    # after 1 payment: 100 000 + 2% interest - payment
    payment = round(annuity_payment(100000, 24, 12), 2)
    assert rules.remaining_principal(100000, 24, 12, 1) == pytest.approx(102000 - payment, abs=0.01)
    assert abs(rules.remaining_principal(100000, 24, 12, 11) - payment / 1.02) < 0.05


# ---------- API ----------

def test_schedule_shows_overdue_and_penalty(erlan, monkeypatch):
    rows = erlan.schedule()
    assert {r["display_status"] for r in rows} == {"Pending"} and rows[0]["penalty"] == 0

    go_to(monkeypatch, due(rows[0]) + timedelta(days=10))
    first, second = erlan.schedule()[:2]
    assert first["display_status"] == "Overdue" and first["status"] == "Pending"   # DB value unchanged
    assert first["days_overdue"] == 10
    assert first["penalty"] == round(first["amount_due"] * 0.05, 2)
    assert first["total_to_pay"] == round(first["amount_due"] + first["penalty"], 2)
    assert second["display_status"] == "Pending"


def test_overdue_payment_is_paid_with_its_penalty(erlan, monkeypatch, caplog):
    go_to(monkeypatch, due(erlan.first) + timedelta(days=4))
    expected_penalty = round(erlan.first["amount_due"] * 0.02, 2)
    with caplog.at_level("INFO", logger="mfo.audit"):
        r = erlan.post(f"/api/repayments/{erlan.first['payment_id']}/pay")
    assert r.status_code == 200
    assert r.json()["penalty"] == expected_penalty and r.json()["days_overdue"] == 4
    assert r.json()["total_paid"] == round(erlan.first["amount_due"] + expected_penalty, 2)
    assert "penalty=" in caplog.text

    paid = erlan.schedule()[0]
    assert paid["display_status"] == "Paid" and paid["penalty"] == expected_penalty and paid["total_to_pay"] == 0

    import database
    conn = database.get_connection()
    amount = conn.execute("SELECT amount FROM payment_transactions WHERE payment_id = ?",
                          (erlan.first["payment_id"],)).fetchone()["amount"]
    conn.close()
    assert amount == r.json()["total_paid"]


def test_payment_on_time_has_no_penalty(erlan):
    r = erlan.post(f"/api/repayments/{erlan.first['payment_id']}/pay").json()
    assert r["penalty"] == 0 and r["total_paid"] == erlan.first["amount_due"]


def test_kaspi_qr_amount_includes_the_penalty(erlan, monkeypatch):
    go_to(monkeypatch, due(erlan.first) + timedelta(days=2))
    qr = erlan.post(f"/api/repayments/{erlan.first['payment_id']}/kaspi-qr").json()
    assert qr["amount"] == round(erlan.first["amount_due"] * 1.01, 2)


def test_early_payoff_is_blocked_while_something_is_overdue(erlan, monkeypatch):
    go_to(monkeypatch, due(erlan.first) + timedelta(days=1))
    quote = erlan.get(f"/api/loans/{erlan.loan_id}/early-payoff").json()
    assert quote["allowed"] is False and quote["overdue_payments"] == 1
    assert erlan.post(f"/api/loans/{erlan.loan_id}/early-payoff").status_code == 400
    # pay the overdue one -> now it is allowed
    erlan.post(f"/api/repayments/{erlan.first['payment_id']}/pay")
    assert erlan.get(f"/api/loans/{erlan.loan_id}/early-payoff").json()["allowed"] is True


def test_early_payoff_closes_the_loan_for_the_principal_only(erlan):
    erlan.post(f"/api/repayments/{erlan.first['payment_id']}/pay")          # 1 of 12 paid
    quote = erlan.get(f"/api/loans/{erlan.loan_id}/early-payoff").json()
    assert quote["allowed"] and quote["unpaid_payments"] == 11
    assert quote["payoff_amount"] < quote["scheduled_left"] and quote["saving"] > 0

    assert erlan.post("/api/payment-methods/cards", json={"holder": "Erlan", "number": "4242424242424242",
                                                          "expiry": NEXT_YEAR, "cvv": "123"}).status_code == 201
    r = erlan.post(f"/api/loans/{erlan.loan_id}/early-payoff", json={"method": "card"})
    assert r.status_code == 200
    assert r.json()["payments_closed"] == 11 and r.json()["payoff_amount"] == quote["payoff_amount"]

    rows = erlan.schedule()
    assert all(row["status"] == "Paid" for row in rows)
    assert [row["closed_early"] for row in rows] == [False] + [True] * 11
    assert rows[5]["paid_method"] == "Досрочно: Visa •••• 4242"
    loan = erlan.get(f"/api/loans/{erlan.loan_id}").json()
    assert loan["paid_count"] == loan["payments_count"] == 12
    # the loan is closed: no second payoff
    assert erlan.post(f"/api/loans/{erlan.loan_id}/early-payoff").status_code == 400


def test_early_payoff_checks_method_and_owner(erlan, client, as_user, staff):
    url = f"/api/loans/{erlan.loan_id}/early-payoff"
    assert erlan.post(url, json={"method": "kaspi_qr"}).status_code == 400   # QR is for one payment only
    assert erlan.post(url, json={"method": "card"}).status_code == 400       # no saved card

    assert client.post("/api/auth/register", json=OTHER).status_code == 201
    other = as_user(OTHER["email"], OTHER["password"])
    assert other.get(url).status_code == 403
    assert other.post(url).status_code == 403
    # an employee closes the loan at the office (manual)
    r = staff.post(url)
    assert r.status_code == 200 and r.json()["paid_method"].startswith("Manual")


def test_dashboard_shows_rules(erlan):
    page = erlan.get("/dashboard").text
    assert "Погасить досрочно" in page and "Overdue" in page
