# face_auth - everything about the webcam face check, in one package.
#
#   config.py    settings (thresholds etc.), read from the environment / .env
#   engine.py    OpenCV: YuNet finds the face + 5 points, SFace makes the 128-number embedding
#   matching.py  cosine similarity, best of all reference photos, normal and mirrored
#   liveness.py  head-turn math: is the nose moving like on a real 3D head?
#   service.py   enrollment at registration + the two steps of the check (match, then head turns)
#   storage.py   tables face_enrollments, face_references, face_challenges, face_tokens
#   gate.py      no face_token -> no loan application (403); no biometrics -> no registration
#   router.py    /api/face/enroll/*, /api/face/verify, /api/face/liveness and the /face page
#
# Explanation in Russian: docs/FACE_AUTH_RU.md
from face_auth.gate import (attach_registration_biometrics, registration_biometrics_error,
                            require_fresh_face_token)
from face_auth.router import create_router

__all__ = ["create_router", "require_fresh_face_token",
           "registration_biometrics_error", "attach_registration_biometrics"]
