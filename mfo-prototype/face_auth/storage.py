# face_auth/storage.py
# ------------------------------------------------------------
# Database part of the face check (same mfo.db file as the rest of the app).
#
#   face_challenges - "step 1 passed, now turn your head": one row per started check.
#                     The SERVER remembers the order of the turns and the straight-face
#                     embedding, so the browser cannot fake them. One-time, expires fast.
#   face_tokens     - "the whole face check passed": a one-time token for ONE loan application.
#
# Only SHA-256 hashes of the random ids/tokens are stored, never the values themselves.
# The tables are created on first use (CREATE TABLE IF NOT EXISTS), so database.py does not
# need to know about them and an old mfo.db is upgraded automatically.
# ------------------------------------------------------------
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import database

SCHEMA = """
CREATE TABLE IF NOT EXISTS face_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,                 -- UTC 'YYYY-MM-DDTHH:MM:SS'
    used_at    TEXT,                          -- NULL = not used yet
    created_at TEXT NOT NULL,
    FOREIGN KEY (client_id) REFERENCES clients (client_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS face_challenges (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL,
    challenge_hash TEXT NOT NULL UNIQUE,
    steps          TEXT NOT NULL,             -- JSON, e.g. ["left", "right"]
    anchor         TEXT,                      -- JSON: straight-face embedding + yaw; wiped after use
    expires_at     TEXT NOT NULL,
    used_at        TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (client_id) REFERENCES clients (client_id) ON DELETE CASCADE
);
"""


def _connect():
    conn = database.get_connection()
    conn.executescript(SCHEMA)
    return conn


def _utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# =============== FACE TOKENS (proof for one loan application) ===============

def issue_token(client_id, minutes):
    """Create a random one-time token. Returns the token (shown to the client ONCE)."""
    token = secrets.token_urlsafe(32)
    save_token_hash(client_id, sha256(token), minutes)
    return token


def save_token_hash(client_id, token_hash, minutes):
    now = datetime.now(timezone.utc)
    conn = _connect()
    conn.execute("INSERT INTO face_tokens (client_id, token_hash, expires_at, created_at) VALUES (?, ?, ?, ?)",
                 (client_id, token_hash, _utc(now + timedelta(minutes=minutes)), _utc(now)))
    conn.commit()
    conn.close()


def consume_token(token, client_id):
    """True only if the token belongs to this client, was not used yet and has not expired.
    "AND used_at IS NULL" inside the UPDATE makes it single-use even for two requests at once."""
    now = _utc(datetime.now(timezone.utc))
    conn = _connect()
    cursor = conn.execute(
        """UPDATE face_tokens SET used_at = ?
           WHERE token_hash = ? AND client_id = ? AND used_at IS NULL AND expires_at > ?""",
        (now, sha256(token), client_id, now))
    conn.commit()
    ok = cursor.rowcount == 1
    conn.close()
    return ok


# =============== FACE CHALLENGES (head-turn step) ===============

def create_challenge(client_id, steps, anchor, seconds):
    """Remember the head-turn task on the server. Returns the random challenge_id."""
    challenge_id = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    conn = _connect()
    # Housekeeping: old and used challenges are deleted (they contain an embedding = biometrics).
    conn.execute("DELETE FROM face_challenges WHERE expires_at < ? OR used_at IS NOT NULL", (_utc(now),))
    conn.execute(
        """INSERT INTO face_challenges (client_id, challenge_hash, steps, anchor, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, sha256(challenge_id), json.dumps(steps), json.dumps(anchor),
         _utc(now + timedelta(seconds=seconds)), _utc(now)))
    conn.commit()
    conn.close()
    return challenge_id


def consume_challenge(challenge_id, client_id):
    """Use up the challenge (one attempt only, even if the turn then fails).
    Returns {"steps": [...], "anchor": {...}} or None if it is unknown, someone else's,
    expired or already used."""
    now = _utc(datetime.now(timezone.utc))
    conn = _connect()
    row = conn.execute(
        """SELECT id, steps, anchor FROM face_challenges
           WHERE challenge_hash = ? AND client_id = ? AND used_at IS NULL AND expires_at > ?""",
        (sha256(challenge_id), client_id, now)).fetchone()
    data = None
    if row is not None:
        # The UPDATE repeats "used_at IS NULL": if two requests race, only one gets rowcount 1.
        cursor = conn.execute("UPDATE face_challenges SET used_at = ?, anchor = NULL WHERE id = ? AND used_at IS NULL",
                              (now, row["id"]))
        if cursor.rowcount == 1:
            data = {"steps": json.loads(row["steps"]), "anchor": json.loads(row["anchor"])}
    conn.commit()
    conn.close()
    return data
