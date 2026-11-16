# tests/test_access.py
# Access control of the REST API with JWT:
#   no token -> 401, client -> only own data (403 for others), employee -> everything.
import pytest

ALICE = {"iin": "900101300111", "full_name": "Alice", "birth_date": "1990-01-01", "phone": "+77010000001",
         "email": "alice@test.kz", "password": "alice123",
         "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}
BOB = {"iin": "900101300222", "full_name": "Bob", "birth_date": "1990-01-01", "phone": "+77010000002",
       "email": "bob@test.kz", "password": "bob12345",
       "employer_name": "Halyk", "position": "QA", "monthly_income": 300000}


@pytest.fixture
def world(client, staff, as_user):
    # Two clients, each with an auto-approved loan (=> a repayment schedule).
    ids = {}
    for person in (ALICE, BOB):
        assert client.post("/api/auth/register", json=person).status_code == 201
    alice, bob = as_user(ALICE["email"], ALICE["password"]), as_user(BOB["email"], BOB["password"])
    for name, user in (("alice", alice), ("bob", bob)):
        me = user.get("/api/auth/me").json()["user_id"]
        app = user.post("/api/applications", json={"client_id": me, "requested_amount": 100000,
                                                    "term_months": 12})
        assert app.status_code == 201, app.text
        payment = user.get(f"/api/loans/{app.json()['loan_id']}/repayments").json()[0]
        ids[name] = {"client": me, "app": app.json()["application_id"],
                     "loan": app.json()["loan_id"], "payment": payment["payment_id"]}
    return {"anon": client, "staff": staff, "alice": alice, "bob": bob, "ids": ids}


PROTECTED = [
    ("get", "/api/clients"), ("get", "/api/clients/1"), ("delete", "/api/clients/1"),
    ("get", "/api/clients/1/employment"), ("post", "/api/clients/1/employment"),
    ("get", "/api/clients/1/documents"), ("post", "/api/clients/1/documents"),
    ("get", "/api/employees"), ("get", "/api/applications"), ("post", "/api/applications"),
    ("get", "/api/applications/1"), ("get", "/api/applications/1/checks"),
    ("post", "/api/applications/1/decision"), ("get", "/api/loans"), ("get", "/api/loans/1"),
    ("get", "/api/loans/1/repayments"), ("post", "/api/repayments/1/pay"), ("get", "/api/auth/me"),
]


@pytest.mark.parametrize("method,path", PROTECTED)
def test_no_token_gives_401(world, method, path):
    # Valid bodies for the rate-limited endpoints: they check the token after the body
    # (an empty body there gives 422 first).
    kwargs = {}
    if path == "/api/applications" and method == "post":
        kwargs = {"json": {"client_id": 1, "requested_amount": 1000, "term_months": 12}}
    elif path.endswith("/documents") and method == "post":
        kwargs = {"files": {"file": ("p.png", b"img", "image/png")}}
    r = getattr(world["anon"], method)(path, **kwargs)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_registration_stays_public(client):
    assert client.post("/api/clients", json={k: v for k, v in ALICE.items()
                                              if k not in ("employer_name", "position",
                                                           "monthly_income")}).status_code == 201


def test_client_sees_only_own_lists(world):
    alice, ids = world["alice"], world["ids"]
    apps = alice.get("/api/applications").json()
    assert {a["client_id"] for a in apps} == {ids["alice"]["client"]}
    loans = alice.get("/api/loans").json()
    assert {l["client_id"] for l in loans} == {ids["alice"]["client"]}
    # Asking for another client's list explicitly is refused.
    assert alice.get(f"/api/applications?client_id={ids['bob']['client']}").status_code == 403
    assert alice.get(f"/api/loans?client_id={ids['bob']['client']}").status_code == 403
    # The employee sees both clients.
    assert len(world["staff"].get("/api/applications").json()) == 2


def test_client_cannot_touch_another_client(world):
    alice, bob = world["alice"], world["ids"]["bob"]
    cid = bob["client"]
    for method, path, kwargs in [
        ("get", f"/api/clients/{cid}", {}),
        ("get", f"/api/clients/{cid}/employment", {}),
        ("post", f"/api/clients/{cid}/employment", {"json": {"employer_name": "X", "monthly_income": 1}}),
        ("get", f"/api/clients/{cid}/documents", {}),
        ("post", f"/api/clients/{cid}/documents", {"files": {"file": ("p.png", b"img", "image/png")}}),
        ("post", "/api/applications", {"json": {"client_id": cid, "requested_amount": 1000, "term_months": 12}}),
        ("get", f"/api/applications/{bob['app']}", {}),
        ("get", f"/api/loans/{bob['loan']}", {}),
        ("get", f"/api/loans/{bob['loan']}/repayments", {}),
        ("post", f"/api/repayments/{bob['payment']}/pay", {}),
    ]:
        r = getattr(alice, method)(path, **kwargs)
        assert r.status_code == 403, (method, path, r.status_code)
    # Bob's payment is still not paid.
    assert world["staff"].get(f"/api/loans/{bob['loan']}/repayments").json()[0]["status"] == "Pending"


def test_client_can_use_own_data(world):
    alice, me = world["alice"], world["ids"]["alice"]
    assert alice.get(f"/api/clients/{me['client']}").status_code == 200
    assert alice.post(f"/api/clients/{me['client']}/documents",
                      files={"file": ("p.png", b"img", "image/png")}).status_code == 201
    assert alice.post(f"/api/repayments/{me['payment']}/pay").json()["status"] == "Paid"


@pytest.mark.parametrize("method,path", [
    ("get", "/api/clients"), ("delete", "/api/clients/1"), ("get", "/api/employees"),
    ("get", "/api/applications/1/checks"), ("post", "/api/applications/1/decision"),
])
def test_employee_only_endpoints_refuse_clients(world, method, path):
    kwargs = {"json": {"decision": "approve"}} if path.endswith("decision") else {}
    assert getattr(world["alice"], method)(path, **kwargs).status_code == 403


def test_decision_uses_employee_from_token(world, client):
    alice, me = world["alice"], world["ids"]["alice"]
    big = alice.post("/api/applications", json={"client_id": me["client"], "requested_amount": 1000000,
                                                "term_months": 12}).json()
    assert big["status"] == "Pending Admin Review"
    # No employee_id in the body: it is taken from the JWT.
    r = world["staff"].post(f"/api/applications/{big['application_id']}/decision", json={"decision": "approve"})
    assert r.status_code == 200
    assert r.json()["employee_id"] == 1 and r.json()["status"] == "Approved"


def test_web_pages_api_works_with_cookie(client):
    # The dashboard JavaScript calls the API with the access_token cookie, no header needed.
    client.post("/api/auth/register", json=ALICE)
    client.post("/login", data={"login": ALICE["email"], "password": ALICE["password"]})
    assert client.get("/api/loans").status_code == 200
    assert client.get("/api/employees").status_code == 403
