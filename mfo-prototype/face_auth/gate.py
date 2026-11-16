# face_auth/gate.py
# ------------------------------------------------------------
# The face GATE in front of loan applications (called from main.py, POST /api/applications).
# A client must send a fresh one-time face_token; an employee entering an application
# for a client in the office does not need one (main.py decides who is who).
# ------------------------------------------------------------
import logging

from fastapi import HTTPException

from face_auth import storage
from log_config import audit


def require_fresh_face_token(request, face_token, client_id):
    """Use up a one-time face_token. 403 if it is missing, someone else's, expired or already used."""
    if not face_token:
        audit("face_gate_denied", request, logging.WARNING, client_id=client_id, reason="no_token")
        raise HTTPException(403, "Face verification required. Open /face, pass the check, then submit again.")
    if not storage.consume_token(face_token, client_id):
        audit("face_gate_denied", request, logging.WARNING, client_id=client_id, reason="bad_token")
        raise HTTPException(403, "Face verification token is invalid, expired or already used. Please verify again.")
