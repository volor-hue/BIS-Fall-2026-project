# tests/test_face_enroll.py
# "Пройти биометрию" at registration (POST /api/face/enroll/start + /finish) and the rule
# "no biometrics -> no registration". Fake computer-vision engine, no real photos.
import pytest

from face_fakes import FAKE_PNG
from test_face import CLIENT, enroll, snapshot

WEB_FORM = {"full_name": "Web Face", "iin": "900101300333", "phone": "+77010000003",
            "email": "webface@test.kz", "password": "pass1234", "birth_date": "1990-01-01",
            "employer_name": "Kaspi", "monthly_income": "300000"}


@pytest.fixture
def required(monkeypatch):
    # The real server default: biometrics are required (the tests run with FACE_ENROLL_REQUIRED=0).
    from face_auth import config

    monkeypatch.setattr(config, "ENROLL_REQUIRED", True)


def client_id_by_email(staff, email):
    return next(c["client_id"] for c in staff.get("/api/clients").json() if c["email"] == email)


def test_enrollment_gives_one_time_token(client, fake_engine):
    data = enroll(client).json()
    assert data["passed"] is True, data
    assert data["enrollment_token"] and data["enrollment_expires_in"] == 900
    assert [s["passed"] for s in data["steps"]] == [True, True]
    assert "references" not in data           # the face vectors stay on the server


def test_web_registration_with_biometrics_saves_three_references(client, staff, fake_engine, required):
    from face_auth import storage
    from face_fakes import FACES

    token = enroll(client).json()["enrollment_token"]
    r = client.post("/register", data=dict(WEB_FORM, enrollment_token=token), follow_redirects=False)
    assert r.status_code == 303
    refs = storage.get_references(client_id_by_email(staff, WEB_FORM["email"]))
    assert [ref["kind"] for ref in refs] == ["front", "right", "left"]
    assert len(refs[0]["embedding"]) == 128
    # The most turned frame of each side is kept.
    assert refs[1]["embedding"] == pytest.approx(list(FACES["me_right"][1]), abs=1e-5)
    assert refs[1]["yaw"] < -0.2 < 0.2 < refs[2]["yaw"]
    # The enrolled face then passes step 1 of the loan check.
    client.post("/login", data={"login": WEB_FORM["email"], "password": WEB_FORM["password"]})
    assert snapshot(client).json()["match"] is True


def test_registration_without_biometrics_is_refused(client, staff, fake_engine, required):
    r = client.post("/register", data=WEB_FORM)
    assert r.status_code == 400 and "Пройдите биометрию" in r.text
    r = client.post("/api/auth/register", json=CLIENT)
    assert r.status_code == 400 and "биометрию" in r.json()["detail"]
    assert client.post("/register", data=dict(WEB_FORM, enrollment_token="made-up")).status_code == 400
    assert staff.get("/api/clients").json() == []


def test_enrollment_token_is_one_time(client, fake_engine, required):
    token = enroll(client).json()["enrollment_token"]
    assert client.post("/api/auth/register", json=dict(CLIENT, enrollment_token=token)).status_code == 201
    other = dict(CLIENT, iin="900101300222", email="other@test.kz", enrollment_token=token)
    r = client.post("/api/auth/register", json=other)
    assert r.status_code == 400 and "устарела" in r.json()["detail"]


def test_expired_enrollment_token_is_refused(client, fake_engine, required, monkeypatch):
    from face_auth import config

    monkeypatch.setattr(config, "ENROLL_TOKEN_MINUTES", -1)
    token = enroll(client).json()["enrollment_token"]
    assert client.post("/register", data=dict(WEB_FORM, enrollment_token=token)).status_code == 400


def test_form_error_keeps_the_biometrics(client, fake_engine, required):
    # A wrong IIN: the page comes back with the token, the user does not turn the head again.
    token = enroll(client).json()["enrollment_token"]
    r = client.post("/register", data=dict(WEB_FORM, iin="123", enrollment_token=token))
    assert r.status_code == 400 and token in r.text
    r = client.post("/register", data=dict(WEB_FORM, enrollment_token=token), follow_redirects=False)
    assert r.status_code == 303


@pytest.mark.parametrize("front, right, left, reason", [
    (["me"], ["me"] * 4, ["me"] * 4, "не обнаружен"),                      # a photo: nothing moves
    (["me"], ["me", "me_left"], ["me", "me_right"], "не в ту сторону"),     # turned the wrong way
    (["me"], ["me", "other_right"], ["me", "me_left"], "другой человек"),  # someone else turns
    (["two"], ["me", "me_right"], ["me", "me_left"], "2 лица"),              # two people
    (["noface"], ["me", "me_right"], ["me", "me_left"], "лицо не найдено"),
    (["me_not_straight"], ["me", "me_right"], ["me", "me_left"], "прямо"),
    (["me", "other"], ["me", "me_right"], ["me", "me_left"], "разные лица"),
])
def test_bad_enrollment_gives_no_token(client, fake_engine, front, right, left, reason):
    data = enroll(client, front, right, left).json()
    assert data["passed"] is False and "enrollment_token" not in data
    assert reason in data["reason"]


def test_enrollment_challenge_is_one_time(client, fake_engine):
    from test_face import frames

    start = client.post("/api/face/enroll/start").json()
    files = frames("frames_front", ["me"]) + frames("frames_right", ["me"]) + frames("frames_left", ["me"])
    assert client.post("/api/face/enroll/finish", data={"enroll_id": start["enroll_id"]}, files=files).status_code == 200
    again = client.post("/api/face/enroll/finish", data={"enroll_id": start["enroll_id"]}, files=files)
    assert again.status_code == 403
    assert client.post("/api/face/enroll/finish", data={"enroll_id": "made-up"}, files=files).status_code == 403


def test_old_account_enrolls_on_the_face_page(client, as_user, fake_engine):
    # An account created without biometrics (before this feature): enrolls once, saved to the account.
    from face_auth import storage

    assert client.post("/api/auth/register", json=CLIENT).status_code == 201
    user = as_user(CLIENT["email"], CLIENT["password"])
    uid = user.get("/api/auth/me").json()["user_id"]
    assert 'id="enroll-btn"' in user.get("/face").text
    data = enroll(user).json()
    assert data["passed"] is True and data["saved"] is True and "enrollment_token" not in data
    assert len(storage.get_references(uid)) == 3
    # Already enrolled: the biometrics cannot be replaced from the account.
    assert user.post("/api/face/enroll/start").status_code == 409
    assert 'id="enroll-btn"' not in user.get("/face").text


def test_register_page_has_biometrics_and_no_photo_fields(client):
    page = client.get("/register").text
    assert "Пройти биометрию" in page and 'name="enrollment_token"' in page
    assert "face_photo" not in page and "Photo 3x4" not in page
    assert 'name="passport_photo"' in page   # the passport upload stays


def test_enrollment_rejects_bad_frames(client, fake_engine):
    start = client.post("/api/face/enroll/start").json()
    bad = client.post("/api/face/enroll/finish", data={"enroll_id": start["enroll_id"]},
                      files=[("frames_front", ("x.gif", b"GIF89a", "image/gif"))])
    assert bad.status_code == 400
    ok_png = client.post("/api/face/enroll/finish", data={"enroll_id": start["enroll_id"]},
                         files=[("frames_front", ("x.png", FAKE_PNG, "image/png"))])
    assert ok_png.status_code == 200 and ok_png.json()["passed"] is False   # no turn frames
