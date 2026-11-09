# tests/test_logging.py
# Logging tests: important events get into the audit log,
# and secrets (password, password hash, IIN, e-mail) never get into any log.
import logging
import os

import pytest

import database
import log_config
from log_config import clean, mask_login

PASSWORD = "Sup3r-Secret-pass"
IIN = "990101300456"
EMAIL = "log@test.kz"


def audit_messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == "mfo.audit"]


def test_register_login_logout_are_audited_without_secrets(client, caplog):
    r = client.post("/register", data={
        "full_name": "Log Client", "iin": IIN, "phone": "+77010000000", "email": EMAIL,
        "password": PASSWORD, "employer_name": "Halyk", "monthly_income": "300000"},
        follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/login", data={"login": EMAIL, "password": PASSWORD}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    client.get("/logout", follow_redirects=False)

    lines = audit_messages(caplog)
    assert any("event=register_ok" in line and "via=web" in line for line in lines)
    assert any("event=login_ok" in line and "role=client" in line for line in lines)
    assert any("event=logout" in line and "user=client:" in line for line in lines)
    # Secrets and personal data never get into ANY log line.
    for secret in (PASSWORD, database.hash_password(PASSWORD), IIN, EMAIL):
        assert secret not in caplog.text


def test_failed_login_is_warning_with_masked_login(client, caplog):
    r = client.post("/login", data={"login": "admin", "password": "wrong-password-123"})
    assert r.status_code == 401

    failed = [rec for rec in caplog.records if "event=login_failed" in rec.getMessage()]
    assert len(failed) == 1
    assert failed[0].levelno == logging.WARNING
    assert "login=ad***" in failed[0].getMessage()
    assert "wrong-password-123" not in caplog.text
    # The request line of a 4xx answer is a WARNING too.
    assert any(rec.name == "mfo.web" and "status=401" in rec.getMessage()
               and rec.levelno == logging.WARNING for rec in caplog.records)


def test_business_events_are_audited(client, caplog):
    r = client.post("/api/clients", json={
        "iin": "050101500123", "full_name": "Api Client", "birth_date": "2005-01-01",
        "phone": "+77011234567", "email": "api@test.kz", "password": "pass123"})
    client_id = r.json()["client_id"]
    client.post(f"/api/clients/{client_id}/employment",
                json={"employer_name": "Kaspi", "monthly_income": 300000})
    # Small amount -> auto-approved -> a loan with a schedule is issued.
    r = client.post("/api/applications", json={"client_id": client_id, "requested_amount": 100000,
                                               "term_months": 12})
    loan_id = r.json()["loan_id"]
    payment_id = client.get(f"/api/loans/{loan_id}/repayments").json()[0]["payment_id"]
    client.post(f"/api/repayments/{payment_id}/pay")
    client.delete(f"/api/clients/{client_id}")

    events = " ".join(audit_messages(caplog))
    for event in ("register_ok", "application_submitted", "loan_issued", "payment_paid", "client_deleted"):
        assert f"event={event}" in events


def test_user_input_cannot_forge_log_lines(client, caplog):
    # Someone types a line break and a fake "login_ok" line into the login field.
    client.post("/login", data={"login": "evil@x.kz\n2026-11-09 INFO mfo.audit event=login_ok",
                                "password": "x"})
    line = [msg for msg in audit_messages(caplog) if "event=login_failed" in msg][-1]
    assert "\n" not in line

    assert clean("a\nb") == "a?b"
    assert clean("two words") == '"two words"'
    assert clean("") == '""'
    assert mask_login("web@test.kz") == "we***@test.kz"
    assert mask_login("admin") == "ad***"


def test_crash_is_logged_with_traceback(client, caplog, monkeypatch):
    def broken():
        raise RuntimeError("database is down")

    monkeypatch.setattr(database, "get_clients", broken)
    with pytest.raises(RuntimeError):
        client.get("/api/clients")

    crashed = [rec for rec in caplog.records if "request crashed" in rec.getMessage()]
    assert len(crashed) == 1
    assert crashed[0].levelno == logging.ERROR
    assert crashed[0].exc_info is not None  # the traceback is saved


def test_audit_has_its_own_file(client):
    client.post("/login", data={"login": "admin", "password": "wrong"})
    folder = log_config.LOG_DIR  # a temporary folder in tests (see conftest.py)
    app_text = open(os.path.join(folder, "app.log"), encoding="utf-8").read()
    audit_text = open(os.path.join(folder, "audit.log"), encoding="utf-8").read()

    assert "event=login_failed" in audit_text
    assert "method=POST path=/login status=401" in app_text  # request lines go to app.log...
    assert "method=" not in audit_text                        # ...but not into the audit file
    assert "event=login_failed" in app_text                   # app.log is the full timeline
