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


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Every test gets a NEW empty database and its own uploads folder.
    import database
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "UPLOAD_DIR", str(tmp_path / "uploads"))
    database.init_db()
    return TestClient(main.app)
