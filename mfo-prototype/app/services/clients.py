# app/services/clients.py
# ------------------------------------------------------------
# Business operations with clients: registration, jobs, deletion.
# Used by the API (app/api/) and by the web forms (app/web/pages.py), so the rules are the same
# everywhere. Who is ALLOWED to call them is checked one layer up (app/api/deps.py).
# ------------------------------------------------------------
import sqlite3
from datetime import datetime

from app import face_auth
from app.db import database
from app.services.errors import NotFoundError, RuleError

DUPLICATE = "Client with this IIN or email already exists"


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def check_registration_biometrics(enrollment_token):
    """Before the client is created: is the biometric enrollment done (see app/face_auth/gate.py)?"""
    error = face_auth.registration_biometrics_error(enrollment_token)
    if error:
        raise RuleError(error, reason="no_biometrics")


def register_client(iin, full_name, birth_date, phone, address, email, password, marital_status,
                    enrollment_token=None, check_biometrics=True):
    """Create a client. Returns client_id.
    RuleError reasons: "no_biometrics" (enrollment missing/expired), "duplicate" (IIN or e-mail taken)."""
    if check_biometrics:
        check_registration_biometrics(enrollment_token)
    try:
        client_id = database.create_client(iin, full_name, birth_date, phone, address,
                                           email.strip().lower(), password, marital_status, now_text())
    except sqlite3.IntegrityError:
        # UNIQUE constraint on iin or email failed.
        raise RuleError(DUPLICATE, reason="duplicate")
    # The face vectors of the enrollment now belong to this client (one-time token).
    if check_biometrics and not face_auth.attach_registration_biometrics(enrollment_token, client_id):
        database.delete_client(client_id)   # the biometrics expired in the meantime: undo
        raise RuleError(face_auth.gate.BIOMETRICS_EXPIRED, reason="no_biometrics")
    return client_id


def add_employment(client_id, employer_name, position, monthly_income):
    try:
        return database.create_employment(client_id, employer_name, position, monthly_income)
    except sqlite3.IntegrityError:
        # The FOREIGN KEY rule stopped us: there is no client with this id.
        raise RuleError("FOREIGN KEY constraint failed: client does not exist")


def delete_client(client_id):
    # Demonstrates ON DELETE CASCADE: all related rows disappear too.
    if database.delete_client(client_id) == 0:
        raise NotFoundError("Client not found")
