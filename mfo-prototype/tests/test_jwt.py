# tests/test_jwt.py
# JWT authentication: tokens are signed, checked, expire, and replace the old plain-id cookies.
import base64
import json
import time

import pytest

from app.core import security

NEW_CLIENT = {
    "iin": "990101300777", "full_name": "Jwt Client", "birth_date": "1999-01-01",
    "phone": "+77017654321", "address": "Astana", "email": "Jwt@Test.kz",
    "password": "pass123", "marital_status": "Single",
    "employer_name": "Halyk", "position": "Dev", "monthly_income": 500000,
}


def _b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


# ---------- the token itself ----------

def test_token_roundtrip():
    payload = security.decode_token(security.create_token("client", 7))
    assert payload["role"] == "client" and payload["uid"] == 7 and payload["sub"] == "client:7"
    assert payload["exp"] - payload["iat"] == security.EXPIRE_SECONDS


def test_edited_payload_is_rejected():
    header, _, signature = security.create_token("client", 7).split(".")
    now = int(time.time())
    forged = _b64({"sub": "employee:1", "role": "employee", "uid": 1, "iat": now, "exp": now + 999})
    with pytest.raises(security.TokenError):
        security.decode_token(f"{header}.{forged}.{signature}")


def test_alg_none_is_rejected():
    now = int(time.time())
    token = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(
        {"role": "employee", "uid": 1, "iat": now, "exp": now + 999}) + "."
    with pytest.raises(security.TokenError):
        security.decode_token(token)


def test_expired_token_is_rejected():
    old = security.create_token("client", 7, now=time.time() - security.EXPIRE_SECONDS - 5)
    with pytest.raises(security.TokenError):
        security.decode_token(old)


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "x" * 5000, "...."])
def test_garbage_is_rejected(junk):
    with pytest.raises(security.TokenError):
        security.decode_token(junk)


def test_wrong_role_gives_none():
    token = security.create_token("client", 3)
    assert security.user_from_token(token, "client") == 3
    assert security.user_from_token(token, "employee") is None


# ---------- API: register / login / me ----------

def test_api_register_returns_token_and_me_works(client):
    r = client.post("/api/auth/register", json=NEW_CLIENT)
    assert r.status_code == 201
    data = r.json()
    assert data["token_type"] == "bearer" and data["role"] == "client"
    assert data["client"]["email"] == "jwt@test.kz"
    assert "password_hash" not in data["client"]

    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + data["access_token"]})
    assert me.status_code == 200
    assert me.json()["user_id"] == data["user_id"]
    assert me.json()["profile"]["full_name"] == "Jwt Client"
    # The job from the registration was saved too (the client reads their own profile with the token).
    own = client.get(f"/api/clients/{data['user_id']}",
                     headers={"Authorization": "Bearer " + data["access_token"]})
    assert own.json()["total_monthly_income"] == 500000


def test_api_register_duplicate(client):
    assert client.post("/api/auth/register", json=NEW_CLIENT).status_code == 201
    assert client.post("/api/auth/register", json=NEW_CLIENT).status_code == 400


def test_api_login_client_and_employee(client):
    client.post("/api/auth/register", json=NEW_CLIENT)
    r = client.post("/api/auth/login", data={"username": "jwt@test.kz", "password": "pass123"})
    assert r.status_code == 200 and r.json()["role"] == "client"

    r = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code == 200 and r.json()["role"] == "employee"
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + r.json()["access_token"]})
    assert me.json()["profile"]["login"] == "admin"


def test_api_login_wrong_password(client):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_me_without_or_with_bad_token(client):
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401


def test_me_after_account_deleted(client, staff):
    data = client.post("/api/auth/register", json=NEW_CLIENT).json()
    assert staff.delete(f"/api/clients/{data['user_id']}").status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": "Bearer " + data["access_token"]})
    assert me.status_code == 401


# ---------- web pages use the JWT cookie ----------

def test_web_login_sets_jwt_cookie(client):
    r = client.post("/login", data={"login": "admin", "password": "admin123"}, follow_redirects=False)
    assert r.status_code == 303
    set_cookie = r.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie and "HttpOnly" in set_cookie
    token = client.cookies.get("access_token")
    assert security.decode_token(token)["role"] == "employee"
    assert client.get("/admin").status_code == 200


def test_old_plain_cookie_no_longer_works(client):
    # Before JWT anyone could write "employee_id=1" into a cookie and open /admin.
    client.cookies.set("employee_id", "1")
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_forged_cookie_is_rejected(client):
    client.cookies.set("access_token", security.create_token("client", 1) + "x")
    assert client.get("/dashboard", follow_redirects=False).status_code == 303


def test_client_token_does_not_open_admin(client):
    client.post("/api/auth/register", json=NEW_CLIENT)
    client.post("/login", data={"login": "jwt@test.kz", "password": "pass123"})
    assert client.get("/dashboard").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_logout_removes_token(client):
    client.post("/login", data={"login": "admin", "password": "admin123"})
    client.get("/logout", follow_redirects=False)
    assert client.cookies.get("access_token") is None
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_token_never_logged(client, caplog):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    token = r.json()["access_token"]
    client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text
