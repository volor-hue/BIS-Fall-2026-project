# tests/test_api_negative.py
# Negative, boundary and "known issue" tests for the REST API (FastAPI TestClient, no server).
# Test case IDs (API-xx, KI-xx) match docs/TEST_PLAN.md.
#
# Since the JWT version all /api/* endpoints (except registration and login) need a token.
# `staff` (see conftest.py) is a client logged in as the seeded employee "admin": an employee
# may work with any client and does not need a face_token. `client` is the ANONYMOUS client.
#
# Tests marked xfail describe behaviour that SHOULD exist but does not yet (known issues).
# They are reported as "xfailed" now and start to pass (XPASS) once the issue is fixed;
# then remove the xfail mark. Each reason says what the fix should be.
import os

import pytest

from conftest import face_token_for

CLIENT = {
    "iin": "050101500123", "full_name": "Test Client", "birth_date": "2005-01-01",
    "phone": "+77011234567", "address": "Almaty", "email": "client@test.kz",
    "password": "pass123", "marital_status": "Single",
}


def make_client(api, income=300000, **overrides):
    """Create a client (and one job when income is not None). Returns client_id."""
    r = api.post("/api/clients", json={**CLIENT, **overrides})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]
    if income is not None:
        r = api.post(f"/api/clients/{client_id}/employment",
                     json={"employer_name": "Kaspi", "monthly_income": income})
        assert r.status_code == 201, r.text
    return client_id


def apply(api, client_id, amount, term=12):
    return api.post("/api/applications", json={
        "client_id": client_id, "requested_amount": amount, "term_months": term, "purpose": "test"})


# ---------- API-01..07: input validation of applications (Pydantic -> 422) ----------
@pytest.mark.parametrize("body", [
    {"requested_amount": 0, "term_months": 12},        # amount must be > 0
    {"requested_amount": -100, "term_months": 12},
    {"requested_amount": 100000, "term_months": 0},    # term must be > 0
    {"requested_amount": 100000, "term_months": -1},
    {"requested_amount": 100000, "term_months": 121},  # term must be <= 120
    {"requested_amount": "abc", "term_months": 12},    # wrong type
    {"requested_amount": 100000},                      # term_months missing
])
def test_api_application_invalid_body_is_422(staff, body):
    client_id = make_client(staff)
    r = staff.post("/api/applications", json={"client_id": client_id, **body})
    assert r.status_code == 422
    assert staff.get("/api/applications").json() == []   # nothing was created


def test_api_application_term_120_is_accepted(staff):
    client_id = make_client(staff, income=300000)
    assert apply(staff, client_id, 100000, term=120).status_code == 201


def test_api_application_for_missing_client_is_404(staff):
    assert apply(staff, 999, 100000).status_code == 404


# ---------- API-08..10: business-rule boundaries through the API ----------
def test_api_amount_500000_is_auto_and_above_is_manual(staff):
    client_id = make_client(staff, income=10_000_000)
    at_limit = apply(staff, client_id, 500000).json()
    above = apply(staff, client_id, 500000.01).json()
    assert (at_limit["status"], at_limit["approval_type"]) == ("Approved", "Auto")
    assert (above["status"], above["approval_type"]) == ("Pending Admin Review", "Manual")
    assert "loan_id" in at_limit and "loan_id" not in above   # a pending application has no loan yet


def test_api_client_without_job_is_rejected(staff):
    client_id = make_client(staff, income=None)   # no employment row -> income 0
    app = apply(staff, client_id, 100000).json()
    assert (app["status"], app["approval_type"]) == ("Rejected", "Auto")
    assert "loan_id" not in app
    # rejected applications still get their 4 checks (the employee can see why)
    checks = staff.get(f"/api/applications/{app['application_id']}/checks").json()
    assert len(checks) == 4


# ---------- API-11..16: input validation of clients and employment ----------
@pytest.mark.parametrize("override", [
    {"iin": "05010150012a"},      # letter in IIN
    {"iin": "0501015001234"},     # 13 digits
    {"password": "abc"},          # shorter than 4
    {"full_name": None},          # required field is null
])
def test_api_client_invalid_body_is_422(staff, override):
    assert staff.post("/api/clients", json={**CLIENT, **override}).status_code == 422


def test_api_client_missing_email_is_422(staff):
    body = {k: v for k, v in CLIENT.items() if k != "email"}
    assert staff.post("/api/clients", json=body).status_code == 422


def test_api_negative_income_is_422(staff):
    client_id = make_client(staff, income=None)
    r = staff.post(f"/api/clients/{client_id}/employment",
                   json={"employer_name": "X", "monthly_income": -1})
    assert r.status_code == 422


def test_api_zero_income_is_allowed(staff):
    client_id = make_client(staff, income=None)
    r = staff.post(f"/api/clients/{client_id}/employment",
                   json={"employer_name": "Unemployed", "monthly_income": 0})
    assert r.status_code == 201


def test_api_duplicate_email_with_other_iin_is_400(staff):
    make_client(staff)
    r = staff.post("/api/clients", json={**CLIENT, "iin": "990101300456"})
    assert r.status_code == 400


def test_api_email_is_stored_lowercase_and_trimmed(staff):
    client_id = make_client(staff, email="  Client@Test.KZ ")
    assert staff.get(f"/api/clients/{client_id}").json()["email"] == "client@test.kz"


# ---------- API-17..23: unknown ids -> 404 (logged in as employee) ----------
@pytest.mark.parametrize("method, url", [
    ("get", "/api/clients/999"),
    ("get", "/api/applications/999"),
    ("get", "/api/applications/999/checks"),
    ("get", "/api/loans/999"),
    ("get", "/api/loans/999/repayments"),
    ("post", "/api/repayments/999/pay"),
    ("delete", "/api/clients/999"),
])
def test_api_unknown_id_is_404(staff, method, url):
    assert getattr(staff, method)(url).status_code == 404


def test_api_non_numeric_id_is_422(staff):
    assert staff.get("/api/clients/abc").status_code == 422


def test_api_delete_client_twice(staff):
    client_id = make_client(staff)
    assert staff.delete(f"/api/clients/{client_id}").status_code == 200
    assert staff.delete(f"/api/clients/{client_id}").status_code == 404


# ---------- API-24..28: employee decision ----------
def pending_application(staff):
    client_id = make_client(staff, income=300000)
    app = apply(staff, client_id, 1_000_000).json()
    assert app["status"] == "Pending Admin Review"
    return app["application_id"]


def test_api_decision_value_must_be_approve_or_reject(staff):
    application_id = pending_application(staff)
    r = staff.post(f"/api/applications/{application_id}/decision",
                   json={"employee_id": 1, "decision": "maybe"})
    assert r.status_code == 422


def test_api_reject_decision_creates_no_loan(staff):
    application_id = pending_application(staff)
    r = staff.post(f"/api/applications/{application_id}/decision",
                   json={"employee_id": 1, "decision": "reject"})
    assert r.status_code == 200
    assert r.json()["status"] == "Rejected" and r.json()["employee_id"] == 1
    assert "loan_id" not in r.json()
    assert staff.get("/api/loans").json() == []


def test_api_cannot_decide_the_same_application_twice(staff):
    application_id = pending_application(staff)
    url = f"/api/applications/{application_id}/decision"
    assert staff.post(url, json={"decision": "approve"}).status_code == 200
    # second decision (even the opposite one) is refused and must not create a second loan
    assert staff.post(url, json={"decision": "reject"}).status_code == 400
    assert len(staff.get("/api/loans").json()) == 1


def test_api_decision_on_missing_application_is_404(staff):
    assert staff.post("/api/applications/999/decision", json={"decision": "approve"}).status_code == 404


def test_api_employee_cannot_decide_in_the_name_of_a_colleague(staff):
    # The deciding employee is taken from the token; a different employee_id in the body -> 403.
    application_id = pending_application(staff)
    r = staff.post(f"/api/applications/{application_id}/decision",
                   json={"employee_id": 999, "decision": "approve"})
    assert r.status_code == 403
    # the application is still waiting
    assert staff.get(f"/api/applications/{application_id}").json()["status"] == "Pending Admin Review"


# ---------- API-29..30: whole loan life cycle ----------
def test_api_full_repayment_of_a_loan(staff):
    client_id = make_client(staff, income=300000)
    loan_id = apply(staff, client_id, 300000, term=3).json()["loan_id"]
    payments = staff.get(f"/api/loans/{loan_id}/repayments").json()
    assert len(payments) == 3 and all(p["status"] == "Pending" for p in payments)
    for p in payments:
        assert staff.post(f"/api/repayments/{p['payment_id']}/pay").status_code == 200
    loan = staff.get(f"/api/loans/{loan_id}").json()
    assert loan["paid_count"] == loan["payments_count"] == 3


def test_api_schedule_matches_annuity_rule(staff):
    from approval import INTEREST_RATE, build_schedule
    from datetime import date
    client_id = make_client(staff, income=300000)
    loan_id = apply(staff, client_id, 400000, term=12).json()["loan_id"]
    payments = staff.get(f"/api/loans/{loan_id}/repayments").json()
    expected = build_schedule(400000, INTEREST_RATE, 12, date.today())
    assert [(p["due_date"], p["amount_due"]) for p in payments] == expected


# ---------- API-31..33: document upload ----------
def test_api_upload_filename_cannot_escape_uploads_folder(staff):
    import main
    client_id = make_client(staff)
    r = staff.post(f"/api/clients/{client_id}/documents", data={"doc_type": "passport"},
                   files={"file": ("../../evil.txt", b"x", "text/plain")})
    assert r.status_code == 201
    path = r.json()["file_path"]
    assert os.path.dirname(path) == main.UPLOAD_DIR   # stays inside the uploads folder
    assert ".." not in os.path.basename(path)


def test_api_upload_without_file_is_422(staff):
    client_id = make_client(staff)
    assert staff.post(f"/api/clients/{client_id}/documents", data={"doc_type": "passport"}).status_code == 422


def test_api_upload_for_missing_client_is_404(staff):
    r = staff.post("/api/clients/999/documents", files={"file": ("p.png", b"x", "image/png")})
    assert r.status_code == 404


# ---------- API-34..36: client path with the face gate (the new rule) ----------
PERSON = {"iin": "900101300111", "full_name": "Alice", "birth_date": "1990-01-01",
          "phone": "+77010000001", "email": "alice@test.kz", "password": "alice123",
          "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}


def test_api_client_application_needs_a_one_time_face_token(client, as_user):
    assert client.post("/api/auth/register", json=PERSON).status_code == 201
    alice = as_user(PERSON["email"], PERSON["password"])
    me = alice.get("/api/auth/me").json()["user_id"]
    body = {"client_id": me, "requested_amount": 100000, "term_months": 12}

    assert alice.post("/api/applications", json=body).status_code == 403   # no token

    token = face_token_for(me)
    ok = alice.post("/api/applications", json={**body, "face_token": token})
    assert ok.status_code == 201 and ok.json()["status"] == "Approved"

    again = alice.post("/api/applications", json={**body, "face_token": token})
    assert again.status_code == 403                                        # the token is one-time


def test_api_client_cannot_reuse_face_token_of_a_failed_request(client, as_user):
    # A request refused by validation (422) must not use up the token.
    assert client.post("/api/auth/register", json=PERSON).status_code == 201
    alice = as_user(PERSON["email"], PERSON["password"])
    me = alice.get("/api/auth/me").json()["user_id"]
    token = face_token_for(me)
    bad = alice.post("/api/applications", json={"client_id": me, "requested_amount": -5,
                                                "term_months": 12, "face_token": token})
    assert bad.status_code == 422
    good = alice.post("/api/applications", json={"client_id": me, "requested_amount": 100000,
                                                 "term_months": 12, "face_token": token})
    assert good.status_code == 201


# ====================================================================
# REGRESSION TESTS for defects that were found earlier and are now fixed
# (KI-02 forged cookie, KI-03 open API). They must keep passing.
# ====================================================================

def test_ki02_fixed_forged_plain_cookies_do_not_open_pages(client):
    client.cookies.set("employee_id", "1")
    client.cookies.set("client_id", "1")
    for page in ("/admin", "/dashboard"):
        r = client.get(page, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_ki03_fixed_api_needs_login(client, staff):
    make_client(staff)
    assert client.get("/api/clients").status_code == 401
    assert client.delete("/api/clients/1").status_code == 401
    assert staff.get("/api/clients").status_code == 200


# ====================================================================
# KNOWN ISSUES (xfail): still open. Report them to the team.
# ====================================================================

@pytest.mark.xfail(reason="KI-01: AGE_18 is only an informational check; a minor can get an "
                          "auto-approved loan. Fix: reject in submit_application when AGE_18 fails.")
def test_ki01_minor_must_not_get_a_loan(staff):
    client_id = make_client(staff, birth_date="2015-01-01", income=300000)
    app = apply(staff, client_id, 100000).json()
    assert app["status"] == "Rejected"


@pytest.mark.xfail(reason="KI-04: birth_date is a free string; 'garbage' is accepted and only "
                          "fails the AGE_18 check later. Fix: validate as an ISO date in ClientIn.")
def test_ki04_invalid_birth_date_is_rejected_on_registration(client):
    r = client.post("/api/clients", json={**CLIENT, "birth_date": "garbage"})
    assert r.status_code == 422


@pytest.mark.xfail(reason="KI-05: nothing marks a payment as overdue; 'Pending' stays 'Pending' "
                          "after due_date. Fix: derive 'Overdue' in the API (see analytics.py).")
def test_ki05_past_due_payment_is_reported_as_overdue(staff):
    import database
    client_id = make_client(staff, income=300000)
    loan_id = apply(staff, client_id, 300000, term=3).json()["loan_id"]
    conn = database.get_connection()
    conn.execute("UPDATE repayments SET due_date = '2020-01-01' WHERE loan_id = ?", (loan_id,))
    conn.commit()
    conn.close()
    statuses = {p["status"] for p in staff.get(f"/api/loans/{loan_id}/repayments").json()}
    assert "Overdue" in statuses
