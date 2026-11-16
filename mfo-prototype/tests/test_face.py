# tests/test_face.py
# Webcam face check (POST /api/face/verify) and the face gate on POST /api/applications.
# No real photos and no OpenCV models are needed: face_verify.verify is replaced by a fake
# that returns "match" or "no match". The real comparison is shown live with the webcam.
import io

import pytest

from conftest import face_token_for

CLIENT = {"iin": "900101300111", "full_name": "Face Client", "birth_date": "1990-01-01",
          "phone": "+77010000001", "email": "face@test.kz", "password": "face1234",
          "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}
# Tiny "images": only the first bytes (JPEG/PNG signature) are checked before verify().
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


@pytest.fixture
def fake_verify(monkeypatch):
    # Replace the neural networks: the test decides the answer and sees what was compared.
    import face_verify

    state = {"match": True, "calls": []}

    def verify(reference, live):
        state["calls"].append((reference, live))
        score = 0.81 if state["match"] else 0.12
        return {"match": state["match"], "score": score, "threshold": 0.363,
                "reason": "same person" if state["match"] else "different person (score below threshold)"}

    monkeypatch.setattr(face_verify, "verify", verify)
    return state


@pytest.fixture
def person(client, as_user):
    # A registered client with a 3x4 photo (doc_type "face") and a logged-in TestClient.
    assert client.post("/api/auth/register", json=CLIENT).status_code == 201
    user = as_user(CLIENT["email"], CLIENT["password"])
    client_id = user.get("/api/auth/me").json()["user_id"]
    r = user.post(f"/api/clients/{client_id}/documents", data={"doc_type": "face"},
                  files={"file": ("photo3x4.jpg", FAKE_JPEG, "image/jpeg")})
    assert r.status_code == 201
    return {"api": user, "id": client_id}


def apply(user, client_id, face_token=None):
    body = {"client_id": client_id, "requested_amount": 100000, "term_months": 12}
    if face_token is not None:
        body["face_token"] = face_token
    return user.post("/api/applications", json=body)


def snapshot(user, data=FAKE_JPEG):
    return user.post("/api/face/verify", files={"file": ("snapshot.jpg", io.BytesIO(data), "image/jpeg")})


def test_application_without_face_token_is_403(person):
    r = apply(person["api"], person["id"])
    assert r.status_code == 403
    assert "Face verification required" in r.json()["detail"]


def test_match_issues_token_and_token_unlocks_one_application(person, fake_verify):
    r = snapshot(person["api"])
    assert r.status_code == 200
    data = r.json()
    assert data["match"] is True and data["score"] == 0.81
    assert data["face_token"] and data["face_token_expires_in"] == 300
    # The live snapshot was compared with the 3x4 photo uploaded by THIS client.
    reference, live = fake_verify["calls"][0]
    assert "_face_" in reference and live == FAKE_JPEG

    first = apply(person["api"], person["id"], data["face_token"])
    assert first.status_code == 201 and first.json()["status"] == "Approved"
    # One-time: the same token cannot be used again.
    second = apply(person["api"], person["id"], data["face_token"])
    assert second.status_code == 403


def test_no_match_gives_no_token(person, fake_verify):
    fake_verify["match"] = False
    data = snapshot(person["api"]).json()
    assert data["match"] is False and "face_token" not in data
    assert apply(person["api"], person["id"]).status_code == 403


def test_expired_or_fake_token_is_rejected(person):
    expired = face_token_for(person["id"], minutes=-1)
    assert apply(person["api"], person["id"], expired).status_code == 403
    assert apply(person["api"], person["id"], "made-up-token").status_code == 403


def test_token_of_another_client_is_rejected(person, client):
    other = dict(CLIENT, iin="900101300222", email="other@test.kz", phone="+77010000002")
    other_id = client.post("/api/auth/register", json=other).json()["user_id"]
    stolen = face_token_for(other_id)
    assert apply(person["api"], person["id"], stolen).status_code == 403


def test_employee_creates_application_without_face_check(person, staff):
    r = apply(staff, person["id"])
    assert r.status_code == 201 and r.json()["status"] == "Approved"


def test_verify_needs_login_and_client_role(client, staff, fake_verify):
    assert snapshot(client).status_code == 401
    assert snapshot(staff).status_code == 403
    assert fake_verify["calls"] == []


def test_verify_rejects_bad_input(person, fake_verify):
    api = person["api"]
    assert snapshot(api, b"GIF89a not a photo").status_code == 400
    assert api.post("/api/face/verify").status_code == 400
    assert api.post("/api/face/verify", data={"image_base64": "%%%"}).status_code == 400
    assert fake_verify["calls"] == []


def test_verify_accepts_base64_png(person, fake_verify):
    import base64

    encoded = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
    r = person["api"].post("/api/face/verify", data={"image_base64": encoded})
    assert r.status_code == 200 and r.json()["match"] is True


def test_verify_without_3x4_photo_is_404(client, as_user, fake_verify):
    assert client.post("/api/auth/register", json=CLIENT).status_code == 201
    user = as_user(CLIENT["email"], CLIENT["password"])
    assert snapshot(user).status_code == 404


def test_face_check_is_audited_without_the_image(person, fake_verify, caplog):
    caplog.set_level("INFO", logger="mfo.audit")
    token = snapshot(person["api"]).json()["face_token"]
    apply(person["api"], person["id"])  # no token -> denied
    lines = " ".join(r.getMessage() for r in caplog.records if r.name == "mfo.audit")
    assert "event=face_verify" in lines and "match=True" in lines
    assert "event=face_gate_denied" in lines
    assert token not in lines


def test_web_registration_saves_3x4_photo_and_face_page(client):
    form = {"full_name": "Web Face", "iin": "900101300333", "phone": "+77010000003",
            "email": "webface@test.kz", "password": "pass1234", "birth_date": "1990-01-01",
            "employer_name": "Kaspi", "monthly_income": "300000"}
    r = client.post("/register", data=form, files={"face_photo": ("me.png", FAKE_PNG, "image/png")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/face", follow_redirects=False).status_code == 303  # not logged in -> /login
    client.post("/login", data={"login": "webface@test.kz", "password": "pass1234"})
    docs = client.get("/api/clients/1/documents").json()
    assert [d["doc_type"] for d in docs] == ["face"]
    assert "Face verification" in client.get("/face").text
    # A 3x4 "photo" that is not JPG/PNG is refused.
    bad = dict(form, iin="900101300444", email="bad@test.kz")
    r = client.post("/register", data=bad, files={"face_photo": ("me.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400
