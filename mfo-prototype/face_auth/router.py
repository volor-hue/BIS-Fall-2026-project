# face_auth/router.py
# ------------------------------------------------------------
# Web layer of the face check (FastAPI). main.py adds it with app.include_router(...).
#
#   GET  /face                 webcam page (templates/face.html)
#   POST /api/face/verify      STEP 1: "look straight" snapshot vs registration photos
#                              -> match: a head-turn challenge {challenge_id, steps: ["left", "right"]}
#   POST /api/face/liveness    STEP 2: frames of the head turns -> passed: one-time face_token
#
# The SERVER decides everything (match, turn order, pass/fail, token). The browser only shows
# instructions and sends pictures, so calling the API directly (curl/Swagger) cannot skip a step.
# ------------------------------------------------------------
import base64
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

import database
from face_auth import config, liveness, service, storage
from log_config import audit, log
from rate_limit import SUBMIT_LIMIT, limiter

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")

# Registration photos used as references, in this order. "passport" is only a fallback
# when the client has no 3x4 photo at all.
REFERENCE_DOC_TYPES = ("face", "face_side")
FALLBACK_DOC_TYPE = "passport"


def reference_photos(client_id):
    """[(doc_type, path), ...]: the newest JPG/PNG of every reference type the client uploaded."""
    documents = database.get_documents(client_id)

    def newest(doc_type):
        for document in reversed(documents):
            path = document["file_path"]
            if document["doc_type"] == doc_type and path.lower().endswith(IMAGE_EXTENSIONS) \
                    and os.path.exists(path):
                return path
        return None

    found = [(kind, newest(kind)) for kind in REFERENCE_DOC_TYPES]
    found = [(kind, path) for kind, path in found if path]
    if not any(kind == "face" for kind, _ in found):
        passport = newest(FALLBACK_DOC_TYPE)
        if passport:
            found.insert(0, (FALLBACK_DOC_TYPE, passport))
    return found


def check_image(data, max_bytes, what="Snapshot"):
    if len(data) > max_bytes:
        raise HTTPException(413, f"{what} is larger than {max_bytes // (1024 * 1024)} MB")
    if not (data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC)):
        raise HTTPException(400, f"{what} must be JPG or PNG")
    return data


def create_router(authenticate, current_client_id, oauth2_scheme, templates):
    """authenticate / current_client_id / oauth2_scheme / templates come from main.py,
    so the face package uses exactly the same JWT login as the rest of the app."""
    router = APIRouter()

    def client_only(request, token):
        user = authenticate(request, token)
        if user["role"] != "client":
            raise HTTPException(403, "Only clients verify their face")
        return user["uid"]

    def run_cv(function, *args):
        try:
            return function(*args)
        except (ImportError, OSError):
            # OpenCV is not installed, or the models could not be downloaded (no internet).
            log.exception("face check unavailable")
            raise HTTPException(503, "Face check is not available on the server right now. Try again later.")

    @router.post("/api/face/verify", tags=["face"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_face_verify(request: Request, file: Optional[UploadFile] = File(None),
                        image_base64: Optional[str] = Form(None),
                        token: Optional[str] = Depends(oauth2_scheme)):
        """STEP 1: compare a 'look straight' webcam snapshot (multipart `file` or form field
        `image_base64`) with the client's registration photos (3x4 + optional side photo)."""
        client_id = client_only(request, token)
        if file is not None:
            live = file.file.read(config.MAX_SNAPSHOT_BYTES + 1)
        elif image_base64:
            try:  # also accepts "data:image/jpeg;base64,...."
                live = base64.b64decode(image_base64.split(",")[-1], validate=True)
            except ValueError:
                raise HTTPException(400, "image_base64 is not valid base64")
        else:
            raise HTTPException(400, "Send a webcam snapshot as 'file' or 'image_base64'")
        check_image(live, config.MAX_SNAPSHOT_BYTES)
        references = reference_photos(client_id)
        if not references:
            raise HTTPException(404, "No 3x4 photo found. Upload a JPG/PNG photo (doc_type 'face') first.")

        result = run_cv(service.start_check, references, live)
        anchor = result.pop("anchor", None)   # stays on the server, never sent to the browser
        # The log gets only numbers, never the image (biometric data).
        audit("face_verify", request, logging.INFO if result["match"] else logging.WARNING,
              client_id=client_id, match=result["match"], score=result["score"],
              reference=result.get("best_reference"), mirrored=result.get("mirrored"))
        if not result["match"]:
            return result
        if config.LIVENESS_ENABLED:
            steps = liveness.new_steps()
            result.update(liveness_required=True, steps=steps,
                          challenge_id=storage.create_challenge(client_id, steps, anchor, config.CHALLENGE_SECONDS),
                          challenge_expires_in=config.CHALLENGE_SECONDS)
        else:  # FACE_LIVENESS=0: the old behaviour, token right after the match
            result["liveness_required"] = False
            result.update(issue_token(request, client_id))
        return result

    @router.post("/api/face/liveness", tags=["face"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_face_liveness(request: Request, challenge_id: str = Form(..., max_length=100),
                          frames_right: List[UploadFile] = File(None),
                          frames_left: List[UploadFile] = File(None),
                          token: Optional[str] = Depends(oauth2_scheme)):
        """STEP 2: frames recorded while the client turned the head RIGHT (`frames_right`)
        and LEFT (`frames_left`). One attempt per challenge; on failure start again from step 1."""
        client_id = client_only(request, token)
        frames = {}
        for direction, uploads in (("right", frames_right), ("left", frames_left)):
            uploads = [u for u in (uploads or []) if u is not None]
            if len(uploads) > config.MAX_FRAMES:
                raise HTTPException(400, f"Too many frames: at most {config.MAX_FRAMES} per turn")
            frames[direction] = [check_image(u.file.read(config.MAX_FRAME_BYTES + 1),
                                             config.MAX_FRAME_BYTES, "Frame") for u in uploads]

        challenge = storage.consume_challenge(challenge_id, client_id)
        if challenge is None:
            audit("face_liveness", request, logging.WARNING, client_id=client_id, passed=False, reason="bad_challenge")
            raise HTTPException(403, "The head-turn check is unknown, expired or already used. Start again.")

        result = run_cv(service.finish_check, challenge, reference_photos(client_id), frames)
        audit("face_liveness", request, logging.INFO if result["passed"] else logging.WARNING,
              client_id=client_id, passed=result["passed"],
              turns=",".join(f"{s['direction']}:{s['turn']}" for s in result["steps"]))
        if result["passed"]:
            result.update(issue_token(request, client_id))
        return result

    def issue_token(request, client_id):
        # A random one-time token: the client sends it with the loan application.
        face_token = storage.issue_token(client_id, config.TOKEN_MINUTES)
        audit("face_token_issued", request, client_id=client_id)
        return {"face_token": face_token, "face_token_expires_in": config.TOKEN_MINUTES * 60}

    @router.get("/face", include_in_schema=False)
    def face_page(request: Request):
        # Webcam page. ?next=apply: after the check the saved application is sent automatically.
        client_id = current_client_id(request)
        client = database.get_client(client_id) if client_id else None
        if client is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "face.html",
                                          {"client": client, "user_name": client["full_name"]})

    return router
