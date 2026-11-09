# tests/test_rate_limit.py
# Rate limit tests: after N requests from one IP the server answers 429 Too Many Requests
# and writes a WARNING. Other IPs and other endpoints are not affected.
# The limits are fixed for tests in conftest.py: 5/minute, 10/minute, 60/minute.
import logging

from fastapi.testclient import TestClient
from limits import parse_many

import main
import rate_limit
import telegram_env

WRONG_LOGIN = {"login": "admin", "password": "wrong"}
BAD_REGISTRATION = {"full_name": "x", "iin": "12", "phone": "1", "email": "x@x.kz",
                    "password": "pass", "employer_name": "x", "monthly_income": "1"}
APPLICATION = {"client_id": 999, "requested_amount": 1000, "term_months": 12}


def allowed(limit_text):
    # "5/minute" -> 5. With several limits ("5/minute;50/hour") the smallest one.
    return min(item.amount for item in parse_many(limit_text))


def rate_limited_lines(caplog):
    return [rec for rec in caplog.records if "event=rate_limited" in rec.getMessage()]


def test_login_returns_429_page_and_warning(client, caplog):
    for _ in range(allowed(rate_limit.AUTH_LIMIT)):
        assert client.post("/login", data=WRONG_LOGIN).status_code == 401
    # Now even the CORRECT password is blocked: password guessing is stopped.
    r = client.post("/login", data={"login": "admin", "password": "admin123"})
    assert r.status_code == 429
    assert main.TOO_MANY_REQUESTS in r.text  # the login page with an error, not raw JSON
    assert r.headers["retry-after"] == "60"

    lines = rate_limited_lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    assert "path=/login" in lines[0].getMessage()
    assert any(rec.name == "mfo.web" and "status=429" in rec.getMessage()
               and rec.levelno == logging.WARNING for rec in caplog.records)


def test_register_is_limited(client):
    for _ in range(allowed(rate_limit.AUTH_LIMIT)):
        # A wrong IIN gives 400, but the attempt still counts.
        assert client.post("/register", data=BAD_REGISTRATION).status_code == 400
    r = client.post("/register", data=BAD_REGISTRATION)
    assert r.status_code == 429
    assert main.TOO_MANY_REQUESTS in r.text


def test_applications_and_documents_have_submit_limit(client):
    for _ in range(allowed(rate_limit.SUBMIT_LIMIT)):
        # Client 999 does not exist (404), but the request still counts.
        assert client.post("/api/applications", json=APPLICATION).status_code == 404
    r = client.post("/api/applications", json=APPLICATION)
    assert r.status_code == 429
    assert r.json() == {"detail": main.TOO_MANY_REQUESTS}  # the API answers JSON

    upload = {"file": ("p.png", b"img", "image/png")}
    for _ in range(allowed(rate_limit.SUBMIT_LIMIT)):
        assert client.post("/api/clients/999/documents", files=upload).status_code == 404
    assert client.post("/api/clients/999/documents", files=upload).status_code == 429


def test_other_endpoints_have_default_limit_each(client):
    for _ in range(allowed(rate_limit.DEFAULT_LIMIT)):
        assert client.get("/api/employees").status_code == 200
    assert client.get("/api/employees").status_code == 429
    # Another endpoint has its own counter and still works.
    assert client.get("/api/clients").status_code == 200


def test_changing_the_id_in_the_url_does_not_bypass_the_limit(client):
    # /api/clients/1, /api/clients/2, ... are ONE endpoint, so they share one counter.
    for client_id in range(1, allowed(rate_limit.DEFAULT_LIMIT) + 1):
        assert client.get(f"/api/clients/{client_id}").status_code == 404
    assert client.get("/api/clients/12345").status_code == 429


def test_another_ip_is_not_blocked(client):
    for _ in range(allowed(rate_limit.AUTH_LIMIT)):
        client.post("/login", data=WRONG_LOGIN)
    assert client.post("/login", data=WRONG_LOGIN).status_code == 429

    other_ip = TestClient(main.app, client=("10.0.0.2", 50000))
    assert other_ip.post("/login", data=WRONG_LOGIN).status_code == 401


def test_spam_writes_one_audit_line_per_minute(client, caplog):
    limit = allowed(rate_limit.AUTH_LIMIT)
    for _ in range(limit + 20):
        client.post("/login", data=WRONG_LOGIN)
    # 20 blocked requests: each one is in app.log, but audit.log gets only one line.
    assert len(rate_limited_lines(caplog)) == 1
    blocked = [rec for rec in caplog.records if rec.name == "mfo.web" and "status=429" in rec.getMessage()]
    assert len(blocked) == 20


def test_limits_come_from_env_file_and_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("MFO_RATE_LIMIT_SUBMIT=3/minute\nTELEGRAM_BOT_TOKEN=secret\n", encoding="utf-8")
    file_values = telegram_env.read_env_file(str(env_file))

    monkeypatch.delenv("MFO_RATE_LIMIT_SUBMIT", raising=False)
    assert rate_limit.read_limit("MFO_RATE_LIMIT_SUBMIT", file_values) == "3/minute"
    # A variable set in the environment wins over the .env file.
    monkeypatch.setenv("MFO_RATE_LIMIT_SUBMIT", "7/minute")
    assert rate_limit.read_limit("MFO_RATE_LIMIT_SUBMIT", file_values) == "7/minute"
    # Nothing set at all -> the default.
    monkeypatch.delenv("MFO_RATE_LIMIT_SUBMIT")
    assert rate_limit.read_limit("MFO_RATE_LIMIT_SUBMIT", {}) == "10/minute"


def test_broken_limit_falls_back_to_default_with_warning(monkeypatch, caplog):
    # A typo in .env must not switch the protection off.
    monkeypatch.setenv("MFO_RATE_LIMIT_AUTH", "five per minit")
    assert rate_limit.read_limit("MFO_RATE_LIMIT_AUTH", {}) == "5/minute"
    warnings = [rec for rec in caplog.records if "bad rate limit" in rec.getMessage()]
    assert warnings and warnings[0].levelno == logging.WARNING
