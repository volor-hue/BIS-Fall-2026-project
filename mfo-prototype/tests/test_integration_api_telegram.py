# tests/test_integration_api_telegram.py
# Integration: data created / changed through the REST API must be visible, correct and
# isolated per client in the Telegram service layer (telegram_service.py). Both layers use
# ONE database. The Bot API is not needed: we test the service functions the bot calls.
# The API is used as the employee "admin" (fixture `staff`, JWT), because since the JWT
# version every /api/* endpoint except registration needs a token.
# Test case IDs (INT-xx) match docs/TEST_PLAN.md.
import telegram_service

CLIENT = {
    "iin": "050101500123", "full_name": "Alice Test", "birth_date": "2000-01-01",
    "phone": "+77011234567", "address": "Almaty", "email": "alice@test.kz",
    "password": "pass123", "marital_status": "Single",
}
TG_ALICE, TG_BOB = 1001, 1002


def register(api, income=300000, **overrides):
    r = api.post("/api/clients", json={**CLIENT, **overrides})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]
    r = api.post(f"/api/clients/{client_id}/employment",
                 json={"employer_name": "Kaspi", "monthly_income": income})
    assert r.status_code == 201, r.text
    return client_id


def link(client_id, telegram_user_id):
    issued = telegram_service.issue_pairing_code(client_id)
    result = telegram_service.redeem_pairing_code(telegram_user_id, issued.code)
    assert result.ok and result.client_id == client_id


def apply(api, client_id, amount, term=12):
    r = api.post("/api/applications", json={"client_id": client_id, "requested_amount": amount,
                                            "term_months": term, "purpose": "test"})
    assert r.status_code == 201, r.text
    return r.json()


# INT-01: an application created via the API appears in the bot with the same data
def test_application_from_api_is_visible_in_bot(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    app = apply(staff, cid, 400000)
    rows = telegram_service.list_own_applications(TG_ALICE)
    assert [r["application_id"] for r in rows] == [app["application_id"]]
    assert rows[0]["requested_amount"] == 400000 and rows[0]["status"] == "Approved"
    assert rows[0]["loan_id"] == app["loan_id"]
    detail = telegram_service.get_own_application(TG_ALICE, app["application_id"])
    assert [c["check_type"] for c in detail["checks"]] == ["IIN_FORMAT", "AGE_18", "INCOME_DTI", "AMOUNT_LIMIT"]


# INT-02: the schedule in the bot is the same as in the API
def test_schedule_in_bot_equals_schedule_in_api(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    loan_id = apply(staff, cid, 400000)["loan_id"]
    api_payments = staff.get(f"/api/loans/{loan_id}/repayments").json()
    bot = telegram_service.get_own_schedule(TG_ALICE, loan_id)
    assert [(p["due_date"], p["amount_due"], p["status"]) for p in bot["payments"]] == \
           [(p["due_date"], p["amount_due"], p["status"]) for p in api_payments]


# INT-03: a payment made through the API shows as Paid in the bot
def test_payment_via_api_is_seen_in_bot(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    loan_id = apply(staff, cid, 400000)["loan_id"]
    payment_id = staff.get(f"/api/loans/{loan_id}/repayments").json()[0]["payment_id"]
    assert staff.post(f"/api/repayments/{payment_id}/pay").status_code == 200
    payments = telegram_service.get_own_schedule(TG_ALICE, loan_id)["payments"]
    assert [p["status"] for p in payments].count("Paid") == 1
    assert payments[0]["status"] == "Paid"


# INT-04: the employee's decision (API) changes what the client sees in the bot
def test_employee_decision_is_seen_in_bot(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    app = apply(staff, cid, 1_000_000)
    before = telegram_service.get_own_application(TG_ALICE, app["application_id"])
    assert before["status"] == "Pending Admin Review" and before["loan_id"] is None
    r = staff.post(f"/api/applications/{app['application_id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200
    after = telegram_service.get_own_application(TG_ALICE, app["application_id"])
    assert after["status"] == "Approved" and after["loan_id"] is not None
    assert telegram_service.list_own_loan_ids(TG_ALICE) == [after["loan_id"]]


# INT-05: a client sees ONLY own data (no leak between two linked clients)
def test_bot_user_sees_only_own_data(staff):
    alice = register(staff)
    bob = register(staff, iin="990101300456", email="bob@test.kz", full_name="Bob Test")
    link(alice, TG_ALICE)
    link(bob, TG_BOB)
    a_app = apply(staff, alice, 400000)
    b_app = apply(staff, bob, 300000)
    assert [r["application_id"] for r in telegram_service.list_own_applications(TG_ALICE)] == [a_app["application_id"]]
    assert [r["application_id"] for r in telegram_service.list_own_applications(TG_BOB)] == [b_app["application_id"]]
    # reading the other person's application / schedule by id returns nothing
    assert telegram_service.get_own_application(TG_ALICE, b_app["application_id"]) is None
    assert telegram_service.get_own_schedule(TG_ALICE, b_app["loan_id"]) is None


# INT-06: a Telegram user who is not linked gets nothing
def test_unlinked_telegram_user_gets_nothing(staff):
    cid = register(staff)
    apply(staff, cid, 400000)
    assert telegram_service.list_own_applications(777) is None
    assert telegram_service.list_own_loan_ids(777) is None
    assert telegram_service.get_own_profile(777) is None


# INT-07: the profile exposes only the name, never IIN / phone / e-mail / password
def test_bot_profile_has_no_personal_data(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    profile = telegram_service.get_own_profile(TG_ALICE)
    assert profile["full_name"] == "Alice Test"
    blob = str(profile)
    for secret in (CLIENT["iin"], CLIENT["phone"], CLIENT["email"], "password"):
        assert secret not in blob


# INT-08: deleting the client through the API removes the Telegram link (ON DELETE CASCADE)
def test_deleting_client_removes_telegram_link(staff):
    cid = register(staff)
    link(cid, TG_ALICE)
    assert telegram_service.get_own_profile(TG_ALICE) is not None
    assert staff.delete(f"/api/clients/{cid}").status_code == 200
    assert telegram_service.get_own_profile(TG_ALICE) is None


# INT-09: a pairing code works once and is single-use
def test_pairing_code_cannot_be_reused(staff):
    cid = register(staff)
    issued = telegram_service.issue_pairing_code(cid)
    assert telegram_service.redeem_pairing_code(TG_ALICE, issued.code).ok
    again = telegram_service.redeem_pairing_code(TG_BOB, issued.code)
    assert not again.ok and again.reason == "replayed"
    assert telegram_service.get_own_profile(TG_BOB) is None
