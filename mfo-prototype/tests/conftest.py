# tests/conftest.py
# Shared setup for all tests. pytest loads this file automatically.
import os
import sys
import tempfile

import pytest

# Let Python find main.py / database.py / approval.py in the parent folder.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use a temporary database file, so tests never touch the real mfo.db.
os.environ["MFO_DB"] = os.path.join(tempfile.mkdtemp(), "import_time.db")
# The same for logs: tests write into a temporary folder, not into the real logs/.
os.environ["MFO_LOG_DIR"] = os.path.join(tempfile.mkdtemp(), "logs")
# Fixed rate limits for the tests, so a developer's own .env cannot change the results.
os.environ["MFO_RATE_LIMIT_AUTH"] = "5/minute"
os.environ["MFO_RATE_LIMIT_SUBMIT"] = "10/minute"
os.environ["MFO_RATE_LIMIT_DEFAULT"] = "60/minute"
# A fixed JWT secret for the tests (a real server sets its own in .env).
os.environ["MFO_JWT_SECRET"] = "test-secret-only-for-pytest-0123456789abcdef"
os.environ["MFO_JWT_EXPIRE_MINUTES"] = "60"
# Registration without a camera in the tests (the real server requires "Пройти биометрию").
# tests/test_face_enroll.py switches the requirement on and checks it.
os.environ["FACE_ENROLL_REQUIRED"] = "0"


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Every test gets a NEW empty database and its own uploads folder.
    import database
    import main
    import rate_limit
    from fastapi.testclient import TestClient

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    database.init_db()
    # Rate limit counters live in memory: start every test from zero.
    rate_limit.reset()
    # An ANONYMOUS client (no token). Protected /api/* endpoints answer it with 401.
    return TestClient(main.app)


def bearer(login, password):
    # Check the password like /api/auth/login does and return {"Authorization": "Bearer <token>"}.
    # (Not through HTTP, so the fixtures do not use up the login rate limit of the tests.)
    import auth_jwt
    import main

    user = main.check_credentials(login, password)
    assert user is not None, "wrong test login or password"
    return {"Authorization": "Bearer " + auth_jwt.create_token(*user)}


def face_token_for(client_id, minutes=5):
    # A one-time face_token as if the client had just passed the face check (match + head turns)
    # (no camera and no OpenCV needed). Clients must send it with POST /api/applications.
    from face_auth import storage

    token = "test-face-token-" + str(client_id) + "-" + os.urandom(6).hex()
    storage.save_token_hash(client_id, storage.sha256(token), minutes)
    return token


@pytest.fixture
def staff(client):
    # A separate TestClient logged in as the seeded employee "admin" (Bearer token in every request).
    import main
    from fastapi.testclient import TestClient

    test_client = TestClient(main.app)
    test_client.headers.update(bearer("admin", "admin123"))
    return test_client


@pytest.fixture
def as_user(client):
    # as_user("mail@test.kz", "pass123") -> a new TestClient logged in as this user.
    import main
    from fastapi.testclient import TestClient

    def make(login, password):
        test_client = TestClient(main.app)
        test_client.headers.update(bearer(login, password))
        return test_client

    return make


@pytest.fixture
def fake_engine(monkeypatch):
    # Face tests: replace the neural networks with made-up faces (tests/face_fakes.py)
    # and record what was analysed.
    from face_auth import engine
    from face_fakes import fake_analyze

    calls = []

    def analyze(data, label="live"):
        calls.append(data)
        return fake_analyze(data, label)

    monkeypatch.setattr(engine, "analyze", analyze)
    return calls
