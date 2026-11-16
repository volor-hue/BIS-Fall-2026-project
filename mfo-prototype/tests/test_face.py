# tests/test_face.py
# The full face check through the API, with a FAKE computer-vision engine (tests/face_fakes.py):
#   POST /api/face/verify   (step 1: match with the registration photos)
#   POST /api/face/liveness (step 2: head turns)
#   the face gate on POST /api/applications.
# No real photos and no OpenCV models are needed. The real models are checked by hand (docs/FACE_AUTH_RU.md).
import io

import pytest

from conftest import face_token_for
from face_fakes import FAKE_JPEG, FAKE_PNG, fake_analyze, image

CLIENT = {"iin": "900101300111", "full_name": "Face Client", "birth_date": "1990-01-01",
          "phone": "+77010000001", "email": "face@test.kz", "password": "face1234",
          "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}


@pytest.fixture
def fake_engine(monkeypatch):
    # Replace the neural networks with made-up faces and record what was analysed.
    from face_auth import engine

    calls = []

    def analyze(data, label="live", single=True):
        calls.append(data)
        return fake_analyze(data, label, single)

    monkeypatch.setattr(engine, "analyze", analyze)
    return calls


def upload(user, client_id, doc_type, tag):
    r = user.post(f"/api/clients/{client_id}/documents", data={"doc_type": doc_type},
                  files={"file": (f"{doc_type}.jpg", image(tag), "image/jpeg")})
    assert r.status_code == 201


@pytest.fixture
def person(client, as_user):
    # A registered client with a 3x4 photo (doc_type "face") and a logged-in TestClient.
    assert client.post("/api/auth/register", json=CLIENT).status_code == 201
    user = as_user(CLIENT["email"], CLIENT["password"])
    client_id = user.get("/api/auth/me").json()["user_id"]
    upload(user, client_id, "face", "me")
    return {"api": user, "id": client_id}


def apply(user, client_id, face_token=None):
    body = {"client_id": client_id, "requested_amount": 100000, "term_months": 12}
    if face_token is not None:
        body["face_token"] = face_token
    return user.post("/api/applications", json=body)


def snapshot(user, data=None):
    data = image("me") if data is None else data
    return user.post("/api/face/verify", files={"file": ("snapshot.jpg", io.BytesIO(data), "image/jpeg")})


def turns(user, challenge_id, right, left):
    # right / left: lists of tags, one fake frame per tag.
    files = [("frames_right", (f"r{i}.jpg", image(t), "image/jpeg")) for i, t in enumerate(right)]
    files += [("frames_left", (f"l{i}.jpg", image(t), "image/jpeg")) for i, t in enumerate(left)]
    return user.post("/api/face/liveness", data={"challenge_id": challenge_id}, files=files)


GOOD_RIGHT = ["me", "me_right", "me_right"]
GOOD_LEFT = ["me", "me_left", "me_left"]


def full_check(user):
    first = snapshot(user).json()
    return turns(user, first["challenge_id"], GOOD_RIGHT, GOOD_LEFT)


# ---------- the gate ----------

def test_application_without_face_token_is_403(person):
    r = apply(person["api"], person["id"])
    assert r.status_code == 403
    assert "Face verification required" in r.json()["detail"]


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


# ---------- step 1: match ----------

def test_match_gives_a_head_turn_challenge_not_a_token(person, fake_engine):
    data = snapshot(person["api"]).json()
    assert data["match"] is True and data["score"] == 1.0
    assert data["liveness_required"] is True
    assert sorted(data["steps"]) == ["left", "right"] and data["challenge_id"]
    assert "face_token" not in data and "anchor" not in data   # the embedding stays on the server
    # Step 1 alone does not unlock an application.
    assert apply(person["api"], person["id"]).status_code == 403


def test_other_person_does_not_match(person, fake_engine):
    data = snapshot(person["api"], image("other")).json()
    assert data["match"] is False and "challenge_id" not in data and "face_token" not in data


def test_mirrored_webcam_picture_still_matches(person, fake_engine):
    data = snapshot(person["api"], image("me_mirrored")).json()
    assert data["match"] is True and data["mirrored"] is True


def test_side_photo_is_used_when_it_matches_better(person, fake_engine):
    # The 3x4 photo is replaced by one that does not look like the webcam face; the side photo does.
    upload(person["api"], person["id"], "face", "bad_ref")
    upload(person["api"], person["id"], "face_side", "me_side")
    data = snapshot(person["api"]).json()
    assert data["match"] is True and data["best_reference"] == "face_side"
    assert data["references_used"] == ["face", "face_side"]


def test_reference_without_a_face_is_skipped(person, fake_engine):
    upload(person["api"], person["id"], "face_side", "noface")   # e.g. a real 90-degree profile
    data = snapshot(person["api"]).json()
    assert data["match"] is True and data["references_skipped"] == ["face_side"]


def test_not_looking_straight_at_step_1_fails(person, fake_engine):
    data = snapshot(person["api"], image("me_not_straight")).json()
    assert data["match"] is False and "прямо" in data["reason"]


def test_no_face_or_two_faces_on_snapshot(person, fake_engine):
    assert snapshot(person["api"], image("noface")).json()["match"] is False
    assert snapshot(person["api"], image("two")).json()["match"] is False


# ---------- step 2: head turns ----------

def test_full_check_issues_one_time_token(person, fake_engine):
    data = full_check(person["api"]).json()
    assert data["passed"] is True, data
    assert [s["passed"] for s in data["steps"]] == [True, True]
    assert data["face_token"] and data["face_token_expires_in"] == 300
    first = apply(person["api"], person["id"], data["face_token"])
    assert first.status_code == 201 and first.json()["status"] == "Approved"
    # One-time: the same token cannot be used again.
    assert apply(person["api"], person["id"], data["face_token"]).status_code == 403


def test_static_photo_fails(person, fake_engine):
    # A photo held to the camera: the same straight face in every frame.
    first = snapshot(person["api"]).json()
    data = turns(person["api"], first["challenge_id"], ["me"] * 4, ["me"] * 4).json()
    assert data["passed"] is False and "face_token" not in data
    assert "не обнаружен" in data["reason"]


def test_tiny_wobble_is_not_a_turn(person, fake_engine):
    first = snapshot(person["api"]).json()
    data = turns(person["api"], first["challenge_id"], ["me", "me_slight_right"] * 2, GOOD_LEFT).json()
    assert data["passed"] is False


def test_wrong_direction_fails(person, fake_engine):
    first = snapshot(person["api"]).json()
    # Turned LEFT when asked to turn right and the other way round.
    data = turns(person["api"], first["challenge_id"], GOOD_LEFT, GOOD_RIGHT).json()
    assert data["passed"] is False and "не в ту сторону" in data["reason"]


def test_other_person_during_turns_fails(person, fake_engine):
    first = snapshot(person["api"]).json()
    data = turns(person["api"], first["challenge_id"], ["me", "other_right"], ["me", "other_right"]).json()
    assert data["passed"] is False and "другой человек" in data["reason"]


def test_second_person_in_frame_fails(person, fake_engine):
    first = snapshot(person["api"]).json()
    data = turns(person["api"], first["challenge_id"], ["me", "two", "me_right"], GOOD_LEFT).json()
    assert data["passed"] is False and "2 лица" in data["reason"]


def test_missing_frames_fail(person, fake_engine):
    first = snapshot(person["api"]).json()
    data = turns(person["api"], first["challenge_id"], ["me_right", "noface", "noface"], GOOD_LEFT).json()
    assert data["passed"] is False and "мало кадров" in data["reason"]


def test_challenge_is_one_time(person, fake_engine):
    first = snapshot(person["api"]).json()
    assert turns(person["api"], first["challenge_id"], ["me"], ["me"]).json()["passed"] is False
    # Even after a failed try the same challenge cannot be used again: start from step 1.
    again = turns(person["api"], first["challenge_id"], GOOD_RIGHT, GOOD_LEFT)
    assert again.status_code == 403


def test_expired_unknown_or_foreign_challenge_is_403(person, client, fake_engine):
    from face_auth import storage

    anchor = {"embedding": [1.0] + [0.0] * 127, "yaw": 0.0}
    expired = storage.create_challenge(person["id"], ["right", "left"], anchor, seconds=-1)
    assert turns(person["api"], expired, GOOD_RIGHT, GOOD_LEFT).status_code == 403
    assert turns(person["api"], "made-up", GOOD_RIGHT, GOOD_LEFT).status_code == 403
    other = dict(CLIENT, iin="900101300222", email="other@test.kz", phone="+77010000002")
    other_id = client.post("/api/auth/register", json=other).json()["user_id"]
    foreign = storage.create_challenge(other_id, ["right", "left"], anchor, seconds=60)
    assert turns(person["api"], foreign, GOOD_RIGHT, GOOD_LEFT).status_code == 403


def test_liveness_can_be_switched_off(person, fake_engine, monkeypatch):
    from face_auth import config

    monkeypatch.setattr(config, "LIVENESS_ENABLED", False)
    data = snapshot(person["api"]).json()
    assert data["liveness_required"] is False and data["face_token"]


def test_too_many_frames_or_bad_frame(person, fake_engine):
    first = snapshot(person["api"]).json()
    assert turns(person["api"], first["challenge_id"], ["me"] * 13, GOOD_LEFT).status_code == 400
    bad = person["api"].post("/api/face/liveness", data={"challenge_id": first["challenge_id"]},
                             files=[("frames_right", ("x.gif", b"GIF89a", "image/gif"))])
    assert bad.status_code == 400


# ---------- access, input, logs, pages ----------

def test_face_endpoints_need_login_and_client_role(client, staff, fake_engine):
    assert snapshot(client).status_code == 401
    assert snapshot(staff).status_code == 403
    assert turns(staff, "x", GOOD_RIGHT, GOOD_LEFT).status_code == 403
    assert fake_engine == []


def test_verify_rejects_bad_input(person, fake_engine):
    api = person["api"]
    assert snapshot(api, b"GIF89a not a photo").status_code == 400
    assert api.post("/api/face/verify").status_code == 400
    assert api.post("/api/face/verify", data={"image_base64": "%%%"}).status_code == 400
    assert fake_engine == []


def test_verify_accepts_base64_png(person, fake_engine):
    import base64

    encoded = "data:image/png;base64," + base64.b64encode(FAKE_PNG).decode()
    r = person["api"].post("/api/face/verify", data={"image_base64": encoded})
    assert r.status_code == 200 and r.json()["match"] is True


def test_verify_without_3x4_photo_is_404(client, as_user, fake_engine):
    assert client.post("/api/auth/register", json=CLIENT).status_code == 201
    user = as_user(CLIENT["email"], CLIENT["password"])
    assert snapshot(user).status_code == 404


def test_face_check_is_audited_without_the_image(person, fake_engine, caplog):
    caplog.set_level("INFO", logger="mfo.audit")
    token = full_check(person["api"]).json()["face_token"]
    apply(person["api"], person["id"])  # no token -> denied
    lines = " ".join(r.getMessage() for r in caplog.records if r.name == "mfo.audit")
    assert "event=face_verify" in lines and "match=True" in lines
    assert "event=face_liveness" in lines and "passed=True" in lines
    assert "event=face_token_issued" in lines and "event=face_gate_denied" in lines
    assert token not in lines


def test_web_registration_saves_both_photos_and_face_page(client):
    form = {"full_name": "Web Face", "iin": "900101300333", "phone": "+77010000003",
            "email": "webface@test.kz", "password": "pass1234", "birth_date": "1990-01-01",
            "employer_name": "Kaspi", "monthly_income": "300000"}
    r = client.post("/register", data=form, files={"face_photo": ("me.png", FAKE_PNG, "image/png"),
                                                   "face_side_photo": ("side.jpg", FAKE_JPEG, "image/jpeg")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/face", follow_redirects=False).status_code == 303  # not logged in -> /login
    client.post("/login", data={"login": "webface@test.kz", "password": "pass1234"})
    docs = client.get("/api/clients/1/documents").json()
    assert [d["doc_type"] for d in docs] == ["face", "face_side"]
    page = client.get("/face").text
    assert "Проверка лица" in page and "Поверните голову" in page
    # Photos that are not JPG/PNG are refused.
    bad = dict(form, iin="900101300444", email="bad@test.kz")
    r = client.post("/register", data=bad, files={"face_photo": ("me.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400
    r = client.post("/register", data=bad, files={"face_side_photo": ("s.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 400
