# face_auth/router.py
# ------------------------------------------------------------
# Web layer of the face check (FastAPI). main.py adds it with app.include_router(...).
#
#   Biometric enrollment ("Пройти биометрию" on the registration page):
#   POST /api/face/enroll/start    -> {enroll_id, steps: ["left", "right"]}   (random order)
#   POST /api/face/enroll/finish   frames "look straight" + head turns
#                                  -> passed: one-time enrollment_token for the registration form
#                                     (a logged-in client without biometrics: saved to the account at once)
#
#   Loan check (before every application):
#   GET  /face                     webcam page (templates/face.html)
#   POST /api/face/verify          STEP 1: "look straight" snapshot vs the enrolled references
#                                  -> match: a head-turn challenge {challenge_id, steps}
#   POST /api/face/liveness        STEP 2: frames of the head turns -> passed: one-time face_token
#
# The SERVER decides everything (match, turn order, pass/fail, tokens). The browser only shows
# instructions and sends pictures, so calling the API directly (curl/Swagger) cannot skip a step.
# ------------------------------------------------------------
import base64
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

import database
from face_auth import config, liveness, service, storage
from log_config import audit, log
from rate_limit import SUBMIT_LIMIT, limiter

JPEG_MAGIC = b"\xff\xd8\xff"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_FRONT_FRAMES = 5
NOT_ENROLLED = "Биометрия не пройдена. Откройте /face и нажмите «Пройти биометрию»."


def check_image(data, max_bytes, what="Snapshot"):
    if len(data) > max_bytes:
        raise HTTPException(413, f"{what} is larger than {max_bytes // (1024 * 1024)} MB")
    if not (data.startswith(JPEG_MAGIC) or data.startswith(PNG_MAGIC)):
        raise HTTPException(400, f"{what} must be JPG or PNG")
    return data


def read_frames(uploads, limit):
    uploads = [u for u in (uploads or []) if u is not None]
    if len(uploads) > limit:
        raise HTTPException(400, f"Too many frames: at most {limit}")
    return [check_image(u.file.read(config.MAX_FRAME_BYTES + 1), config.MAX_FRAME_BYTES, "Frame") for u in uploads]


def client_references(client_id):
    """The enrolled references of the client as [(kind, FaceSample)]."""
    return service.references_from_db(storage.get_references(client_id))


def create_router(authenticate, current_client_id, oauth2_scheme, templates):
    """authenticate / current_client_id / oauth2_scheme / templates come from main.py,
    so the face package uses exactly the same JWT login as the rest of the app."""
    router = APIRouter()
    # The camera template (_face_camera.html) mirrors the preview only if the camera itself does not.
    templates.env.globals["face_camera_mirrored"] = config.CAMERA_MIRRORED

    def client_only(request, token):
        user = authenticate(request, token)
        if user["role"] != "client":
            raise HTTPException(403, "Only clients verify their face")
        return user["uid"]

    def logged_in_client(request, token):
        # Enrollment is open WITHOUT login (registration page). If a client IS logged in, return the id.
        try:
            user = authenticate(request, token)
        except HTTPException:
            return None
        return user["uid"] if user["role"] == "client" else None

    def run_cv(function, *args):
        try:
            return function(*args)
        except (ImportError, OSError):
            # OpenCV is not installed, or the models could not be downloaded (no internet).
            log.exception("face check unavailable")
            raise HTTPException(503, "Face check is not available on the server right now. Try again later.")

    def issue_token(request, client_id):
        # A random one-time token: the client sends it with the loan application.
        face_token = storage.issue_token(client_id, config.TOKEN_MINUTES)
        audit("face_token_issued", request, client_id=client_id)
        return {"face_token": face_token, "face_token_expires_in": config.TOKEN_MINUTES * 60}

    # ---------------- biometric enrollment ----------------

    @router.post("/api/face/enroll/start", tags=["face"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_enroll_start(request: Request, token: Optional[str] = Depends(oauth2_scheme)):
        """Start "Пройти биометрию": the server picks the random order of the head turns."""
        client_id = logged_in_client(request, token)
        if client_id is not None and storage.get_references(client_id):
            raise HTTPException(409, "Biometrics are already enrolled for this account")
        steps = liveness.new_steps()
        return {"enroll_id": storage.create_enrollment(steps, config.CHALLENGE_SECONDS),
                "steps": steps, "expires_in": config.CHALLENGE_SECONDS}

    @router.post("/api/face/enroll/finish", tags=["face"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_enroll_finish(request: Request, enroll_id: str = Form(..., max_length=100),
                          frames_front: List[UploadFile] = File(None),
                          frames_right: List[UploadFile] = File(None),
                          frames_left: List[UploadFile] = File(None),
                          token: Optional[str] = Depends(oauth2_scheme)):
        """Frames 'look straight' (`frames_front`, 1-5) and of the head turns (`frames_right`,
        `frames_left`). One attempt per enroll_id. Passed -> one-time `enrollment_token`
        for the registration form (valid FACE_ENROLL_TOKEN_MINUTES)."""
        front = read_frames(frames_front, MAX_FRONT_FRAMES)
        turns = {"right": read_frames(frames_right, config.MAX_FRAMES),
                 "left": read_frames(frames_left, config.MAX_FRAMES)}
        challenge = storage.take_enrollment_challenge(enroll_id)
        if challenge is None:
            raise HTTPException(403, "The biometric enrollment is unknown, expired or already used. Start again.")
        try:
            result = run_cv(service.enroll, challenge["steps"], front, turns)
        except HTTPException:
            storage.drop_enrollment(challenge["id"])
            raise
        references = result.pop("references", None)
        client_id = logged_in_client(request, token)
        audit("face_enroll", request, logging.INFO if result["passed"] else logging.WARNING,
              passed=result["passed"], client_id=client_id,
              turns=",".join(f"{s['direction']}:{s['turn']}" for s in result["steps"]))
        if not result["passed"]:
            storage.drop_enrollment(challenge["id"])
            return result
        if client_id is not None and not storage.get_references(client_id):
            # A logged-in client who registered before biometrics existed: save to the account now.
            storage.drop_enrollment(challenge["id"])
            storage.save_references(client_id, references)
            result["saved"] = True
            return result
        result["enrollment_token"] = storage.pass_enrollment(challenge["id"], references, config.ENROLL_TOKEN_MINUTES)
        result["enrollment_expires_in"] = config.ENROLL_TOKEN_MINUTES * 60
        return result

    # ---------------- loan check ----------------

    @router.post("/api/face/verify", tags=["face"])
    @limiter.limit(SUBMIT_LIMIT)
    def api_face_verify(request: Request, file: Optional[UploadFile] = File(None),
                        image_base64: Optional[str] = Form(None),
                        token: Optional[str] = Depends(oauth2_scheme)):
        """STEP 1: compare a 'look straight' webcam snapshot (multipart `file` or form field
        `image_base64`) with the client's enrolled references (straight + turned right + left)."""
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
        references = client_references(client_id)
        if not references:
            raise HTTPException(404, NOT_ENROLLED)

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
        else:  # FACE_LIVENESS=0: the token right after the match
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
        frames = {"right": read_frames(frames_right, config.MAX_FRAMES),
                  "left": read_frames(frames_left, config.MAX_FRAMES)}
        challenge = storage.consume_challenge(challenge_id, client_id)
        if challenge is None:
            audit("face_liveness", request, logging.WARNING, client_id=client_id, passed=False, reason="bad_challenge")
            raise HTTPException(403, "The head-turn check is unknown, expired or already used. Start again.")

        result = run_cv(service.finish_check, challenge, client_references(client_id), frames)
        audit("face_liveness", request, logging.INFO if result["passed"] else logging.WARNING,
              client_id=client_id, passed=result["passed"],
              turns=",".join(f"{s['direction']}:{s['turn']}" for s in result["steps"]))
        if result["passed"]:
            result.update(issue_token(request, client_id))
        return result

    @router.get("/face", include_in_schema=False)
    def face_page(request: Request):
        # Webcam page. ?next=apply: after the check the saved application is sent automatically.
        # A client without biometrics (registered before this feature) enrolls here first.
        client_id = current_client_id(request)
        client = database.get_client(client_id) if client_id else None
        if client is None:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(request, "face.html",
                                          {"client": client, "user_name": client["full_name"],
                                           "enrolled": bool(storage.get_references(client_id))})

    return router
