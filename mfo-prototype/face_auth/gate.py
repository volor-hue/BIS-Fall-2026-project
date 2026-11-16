# face_auth/gate.py
# ------------------------------------------------------------
# The face GATE in front of loan applications (called from main.py, POST /api/applications).
# A client must send a fresh one-time face_token; an employee entering an application
# for a client in the office does not need one (main.py decides who is who).
# ------------------------------------------------------------
import logging

from fastapi import HTTPException

from face_auth import config, storage
from log_config import audit


def require_fresh_face_token(request, face_token, client_id):
    """Use up a one-time face_token. 403 if it is missing, someone else's, expired or already used."""
    if not face_token:
        audit("face_gate_denied", request, logging.WARNING, client_id=client_id, reason="no_token")
        raise HTTPException(403, "Face verification required. Open /face, pass the check, then submit again.")
    if not storage.consume_token(face_token, client_id):
        audit("face_gate_denied", request, logging.WARNING, client_id=client_id, reason="bad_token")
        raise HTTPException(403, "Face verification token is invalid, expired or already used. Please verify again.")


# ---------- registration: biometrics are required (main.py, /register and /api/auth/register) ----------
BIOMETRICS_MISSING = "Пройдите биометрию: нажмите «Пройти биометрию» и следуйте подсказкам."
BIOMETRICS_EXPIRED = "Биометрия устарела или уже использована. Пройдите её ещё раз."


def registration_biometrics_error(enrollment_token):
    """None if the registration may go on, otherwise the error text.
    Checked BEFORE the client is created (the token is not used up here)."""
    if enrollment_token:
        return None if storage.enrollment_ready(enrollment_token) else BIOMETRICS_EXPIRED
    return BIOMETRICS_MISSING if config.ENROLL_REQUIRED else None


def attach_registration_biometrics(enrollment_token, client_id):
    """AFTER the client is created: the enrolled face vectors become this client's references.
    Returns False only if a required enrollment could not be used (then main.py undoes the registration)."""
    if not enrollment_token:
        return not config.ENROLL_REQUIRED
    return storage.attach_enrollment(enrollment_token, client_id)
