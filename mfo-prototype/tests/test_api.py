# tests/test_api.py
# API tests: they call the real endpoints through FastAPI's TestClient
# (like a browser/Postman, but without starting a server).
import sqlite3

import database

CLIENT = {
    "iin": "050101500123", "full_name": "Test Client", "birth_date": "2005-01-01",
    "phone": "+77011234567", "address": "Almaty", "email": "client@test.kz",
    "password": "pass123", "marital_status": "Single",
}


def make_client_with_income(client, income=300000):
    r = client.post("/api/clients", json=CLIENT)
    assert r.status_code == 201
    client_id = r.json()["client_id"]
    r = client.post(f"/api/clients/{client_id}/employment",
                    json={"employer_name": "Kaspi", "position": "Analyst", "monthly_income": income})
    assert r.status_code == 201
    return client_id


def test_create_and_get_client_hides_password(client):
    client_id = make_client_with_income(client)
    data = client.get(f"/api/clients/{client_id}").json()
    assert data["iin"] == CLIENT["iin"]
    assert data["total_monthly_income"] == 300000
    assert "password_hash" not in data


def test_iin_must_be_12_digits(client):
    r = client.post("/api/clients", json={**CLIENT, "iin": "123"})
    assert r.status_code == 422  # Pydantic validation error


def test_iin_is_unique(client):
    make_client_with_income(client)
    r = client.post("/api/clients", json={**CLIENT, "email": "other@test.kz"})
    assert r.status_code == 400


def test_income_is_sum_of_all_jobs(client):
    client_id = make_client_with_income(client, 100000)
    client.post(f"/api/clients/{client_id}/employment",
                json={"employer_name": "Freelance", "monthly_income": 50000})
    assert len(client.get(f"/api/clients/{client_id}/employment").json()) == 2
    assert client.get(f"/api/clients/{client_id}").json()["total_monthly_income"] == 150000


def test_foreign_key_blocks_employment_for_missing_client(client):
    r = client.post("/api/clients/999/employment", json={"employer_name": "X", "monthly_income": 1})
    assert r.status_code == 400
    assert "FOREIGN KEY" in r.json()["detail"]


def test_foreign_key_enforced_in_sqlite_directly(client):
    # Even without our API, the database itself refuses a bad client_id.
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO loan_applications (client_id, requested_amount, term_months, "
                     "status, approval_type, created_at) VALUES (999, 1, 1, 'x', 'Auto', 'now')")
        assert False, "should have failed"
    except sqlite3.IntegrityError as error:
        assert "FOREIGN KEY" in str(error)
    finally:
        conn.close()


def test_document_upload_saves_file_and_hash(client):
    client_id = make_client_with_income(client)
    r = client.post(f"/api/clients/{client_id}/documents", data={"doc_type": "passport"},
                    files={"file": ("passport.jpg", b"fake image", "image/jpeg")})
    assert r.status_code == 201
    doc = r.json()
    # sha256 of b"fake image"
    import hashlib
    assert doc["file_hash"] == hashlib.sha256(b"fake image").hexdigest()
    with open(doc["file_path"], "rb") as f:
        assert f.read() == b"fake image"
    assert len(client.get(f"/api/clients/{client_id}/documents").json()) == 1


def test_full_process_three_cases_and_employee_decision(client):
    client_id = make_client_with_income(client, 300000)

    def apply(amount, term):
        r = client.post("/api/applications", json={"client_id": client_id, "requested_amount": amount,
                                                   "term_months": term, "purpose": "test"})
        assert r.status_code == 201
        return r.json()

    a1 = apply(400000, 12)
    assert (a1["status"], a1["approval_type"]) == ("Approved", "Auto")
    assert "loan_id" in a1
    a2 = apply(400000, 1)
    assert (a2["status"], a2["approval_type"]) == ("Rejected", "Auto")
    a3 = apply(1000000, 12)
    assert (a3["status"], a3["approval_type"]) == ("Pending Admin Review", "Manual")
    assert a3["employee_id"] is None

    # Each application has 4 automatic checks.
    checks = client.get(f"/api/applications/{a3['application_id']}/checks").json()
    assert {c["check_type"]: c["result_status"] for c in checks}["AMOUNT_LIMIT"] == "MANUAL_REVIEW"

    # Auto applications cannot be decided by an employee.
    r = client.post(f"/api/applications/{a1['application_id']}/decision",
                    json={"employee_id": 1, "decision": "approve"})
    assert r.status_code == 400

    # Unknown employee -> 404.
    r = client.post(f"/api/applications/{a3['application_id']}/decision",
                    json={"employee_id": 999, "decision": "approve"})
    assert r.status_code == 404

    # The seeded employee approves the big one.
    r = client.post(f"/api/applications/{a3['application_id']}/decision",
                    json={"employee_id": 1, "decision": "approve"})
    assert r.status_code == 200
    assert r.json()["status"] == "Approved"
    assert r.json()["employee_id"] == 1

    # Two loans now: auto (400k) and manual (1M). Rejected one has no loan.
    loans = client.get(f"/api/loans?client_id={client_id}").json()
    assert sorted(l["approved_amount"] for l in loans) == [400000, 1000000]

    # Schedule: 12 payments, pay the first one.
    loan_id = a1["loan_id"]
    payments = client.get(f"/api/loans/{loan_id}/repayments").json()
    assert len(payments) == 12
    first = payments[0]["payment_id"]
    assert client.post(f"/api/repayments/{first}/pay").json()["status"] == "Paid"
    assert client.post(f"/api/repayments/{first}/pay").status_code == 400  # already paid
    assert client.get(f"/api/loans/{loan_id}").json()["paid_count"] == 1


def test_loan_is_one_to_one_with_application(client):
    client_id = make_client_with_income(client)
    app = client.post("/api/applications", json={"client_id": client_id, "requested_amount": 100000,
                                                 "term_months": 12}).json()
    conn = database.get_connection()
    try:
        # A second loan for the same application breaks the UNIQUE rule.
        conn.execute("INSERT INTO loans (application_id, approved_amount, interest_rate, issue_date) "
                     "VALUES (?, 1, 1, 'now')", (app["application_id"],))
        assert False, "should have failed"
    except sqlite3.IntegrityError as error:
        assert "UNIQUE" in str(error)
    finally:
        conn.close()


def test_delete_client_cascades(client):
    client_id = make_client_with_income(client)
    app = client.post("/api/applications", json={"client_id": client_id, "requested_amount": 100000,
                                                 "term_months": 12}).json()
    assert client.delete(f"/api/clients/{client_id}").status_code == 200
    # Everything that belonged to the client is gone (ON DELETE CASCADE).
    assert client.get(f"/api/clients/{client_id}/employment").json() == []
    assert client.get(f"/api/applications/{app['application_id']}").status_code == 404
    assert client.get(f"/api/loans/{app['loan_id']}").status_code == 404
    conn = database.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM repayments").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM client_checks").fetchone()[0] == 0
    conn.close()


def test_employees_seeded_without_password(client):
    employees = client.get("/api/employees").json()
    assert employees[0]["login"] == "admin"
    assert "password_hash" not in employees[0]


def test_html_register_login_and_pages(client):
    r = client.post("/register", data={
        "full_name": "Web Client", "iin": "990101300456", "phone": "1", "email": "web@test.kz",
        "password": "pass123", "birth_date": "1999-01-01", "employer_name": "Halyk",
        "monthly_income": "300000"}, files={"passport_photo": ("p.png", b"img", "image/png")},
        follow_redirects=False)
    assert r.status_code == 303
    clients = client.get("/api/clients").json()
    cid = clients[0]["client_id"]
    assert len(client.get(f"/api/clients/{cid}/documents").json()) == 1

    # Bad IIN in the form shows an error.
    r = client.post("/register", data={"full_name": "x", "iin": "12", "phone": "1", "email": "x@x.kz",
                                       "password": "pass", "employer_name": "x", "monthly_income": "1"})
    assert r.status_code == 400

    r = client.post("/login", data={"login": "web@test.kz", "password": "pass123"}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    assert client.get("/dashboard").status_code == 200

    r = client.post("/login", data={"login": "admin", "password": "admin123"}, follow_redirects=False)
    assert r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200

    r = client.post("/login", data={"login": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_all_endpoints_are_in_swagger(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/api/clients", "/api/clients/{client_id}", "/api/clients/{client_id}/employment",
              "/api/clients/{client_id}/documents", "/api/applications", "/api/applications/{application_id}",
              "/api/applications/{application_id}/checks", "/api/applications/{application_id}/decision",
              "/api/loans", "/api/loans/{loan_id}", "/api/loans/{loan_id}/repayments",
              "/api/repayments/{payment_id}/pay", "/api/employees"]:
        assert p in paths, p
    assert client.get("/docs").status_code == 200
