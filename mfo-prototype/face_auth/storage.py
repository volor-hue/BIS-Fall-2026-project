# face_auth/storage.py
# ------------------------------------------------------------
# Database part of the face check (same mfo.db file as the rest of the app).
#
#   face_challenges - "step 1 passed, now turn your head": one row per started check.
#                     The SERVER remembers the order of the turns and the straight-face
#                     embedding, so the browser cannot fake them. One-time, expires fast.
#   face_tokens     - "the whole face check passed": a one-time token for ONE loan application.
#   face_enrollments - biometric enrollment at registration: the head-turn task, then (after it
#                     passed) the measured face vectors waiting for the registration form.
#   face_references - the client's enrolled face vectors (straight + turned right + turned left).
#                     These are what the webcam face is compared with later. NO images are stored.
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
CREATE TABLE IF NOT EXISTS face_enrollments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_hash TEXT NOT NULL UNIQUE,      -- SHA-256 of enroll_id (the head-turn task)
    steps          TEXT NOT NULL,             -- JSON, e.g. ["right", "left"]
    status         TEXT NOT NULL,             -- started -> checking -> passed -> used
    token_hash     TEXT UNIQUE,               -- SHA-256 of enrollment_token (after it passed)
    refs           TEXT,                      -- JSON face vectors; wiped when used
    expires_at     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS face_references (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL,
    kind       TEXT NOT NULL,                 -- 'front', 'right', 'left'
    embedding  TEXT NOT NULL,                 -- JSON list of 128 numbers (SFace vector)
    yaw        REAL NOT NULL,                 -- head turn of that frame (see liveness.py)
    created_at TEXT NOT NULL,
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


# =============== BIOMETRIC ENROLLMENT (registration) ===============
# 1) start:  create_enrollment()            -> enroll_id + random order of turns
# 2) finish: take_enrollment_challenge()    -> one attempt; then pass_enrollment() or drop_enrollment()
# 3) the registration form sends enrollment_token -> attach_enrollment() copies the vectors
#    to face_references of the NEW client. Every step is one-time and expires.

def create_enrollment(steps, seconds):
    enroll_id = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    conn = _connect()
    # Housekeeping: expired and used enrollments are deleted (they may contain face vectors).
    conn.execute("DELETE FROM face_enrollments WHERE expires_at < ? OR status = 'used'", (_utc(now),))
    conn.execute("""INSERT INTO face_enrollments (challenge_hash, steps, status, expires_at, created_at)
                    VALUES (?, ?, 'started', ?, ?)""",
                 (sha256(enroll_id), json.dumps(steps), _utc(now + timedelta(seconds=seconds)), _utc(now)))
    conn.commit()
    conn.close()
    return enroll_id


def take_enrollment_challenge(enroll_id):
    """One attempt per enroll_id. Returns {"id", "steps"} or None (unknown, expired, already used)."""
    now = _utc(datetime.now(timezone.utc))
    conn = _connect()
    row = conn.execute("""SELECT id, steps FROM face_enrollments
                          WHERE challenge_hash = ? AND status = 'started' AND expires_at > ?""",
                       (sha256(enroll_id), now)).fetchone()
    data = None
    if row is not None:
        cursor = conn.execute("UPDATE face_enrollments SET status = 'checking' WHERE id = ? AND status = 'started'",
                              (row["id"],))
        if cursor.rowcount == 1:
            data = {"id": row["id"], "steps": json.loads(row["steps"])}
    conn.commit()
    conn.close()
    return data


def pass_enrollment(row_id, refs, minutes):
    """The head turns passed: keep the vectors for `minutes` and return the one-time enrollment_token."""
    token = secrets.token_urlsafe(32)
    expires = _utc(datetime.now(timezone.utc) + timedelta(minutes=minutes))
    conn = _connect()
    conn.execute("UPDATE face_enrollments SET status = 'passed', token_hash = ?, refs = ?, expires_at = ? WHERE id = ?",
                 (sha256(token), json.dumps(refs), expires, row_id))
    conn.commit()
    conn.close()
    return token


def drop_enrollment(row_id):
    conn = _connect()
    conn.execute("DELETE FROM face_enrollments WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


def enrollment_ready(token):
    """True if the token is a passed, unused, not expired enrollment (does NOT use it up)."""
    if not token:
        return False
    now = _utc(datetime.now(timezone.utc))
    conn = _connect()
    row = conn.execute("""SELECT 1 FROM face_enrollments
                          WHERE token_hash = ? AND status = 'passed' AND expires_at > ?""",
                       (sha256(token), now)).fetchone()
    conn.close()
    return row is not None


def attach_enrollment(token, client_id):
    """Use up the enrollment_token: its face vectors become the references of this client.
    Returns True on success. The vectors are removed from face_enrollments."""
    if not token:
        return False
    now = _utc(datetime.now(timezone.utc))
    conn = _connect()
    row = conn.execute("""SELECT id, refs FROM face_enrollments
                          WHERE token_hash = ? AND status = 'passed' AND expires_at > ?""",
                       (sha256(token), now)).fetchone()
    ok = False
    if row is not None:
        cursor = conn.execute("UPDATE face_enrollments SET status = 'used', refs = NULL WHERE id = ? AND status = 'passed'",
                              (row["id"],))
        if cursor.rowcount == 1:
            _insert_references(conn, client_id, json.loads(row["refs"]), now)
            ok = True
    conn.commit()
    conn.close()
    return ok


# =============== FACE REFERENCES (what the webcam face is compared with) ===============

def _insert_references(conn, client_id, refs, now):
    conn.execute("DELETE FROM face_references WHERE client_id = ?", (client_id,))
    for ref in refs:
        conn.execute("""INSERT INTO face_references (client_id, kind, embedding, yaw, created_at)
                        VALUES (?, ?, ?, ?, ?)""",
                     (client_id, ref["kind"], json.dumps(ref["embedding"]), ref["yaw"], now))


def save_references(client_id, refs):
    """refs: [{"kind": "front", "embedding": [128 numbers], "yaw": 0.01}, ...] (replaces old ones)."""
    conn = _connect()
    _insert_references(conn, client_id, refs, _utc(datetime.now(timezone.utc)))
    conn.commit()
    conn.close()


def get_references(client_id):
    """The client's enrolled references: [{"kind", "embedding", "yaw"}, ...] (empty if not enrolled)."""
    conn = _connect()
    rows = conn.execute("SELECT kind, embedding, yaw FROM face_references WHERE client_id = ? ORDER BY id",
                        (client_id,)).fetchall()
    conn.close()
    return [{"kind": r["kind"], "embedding": json.loads(r["embedding"]), "yaw": r["yaw"]} for r in rows]
