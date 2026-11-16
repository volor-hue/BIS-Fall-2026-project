# face_auth - everything about the webcam face check, in one package.
#
#   config.py    settings (thresholds etc.), read from the environment / .env
#   engine.py    OpenCV: YuNet finds the face + 5 points, SFace makes the 128-number embedding
#   matching.py  cosine similarity, best of all reference photos, normal and mirrored
#   liveness.py  head-turn math: is the nose moving like on a real 3D head?
#   service.py   the two steps of the check (match, then head turns)
#   storage.py   tables face_challenges + face_tokens (one-time, only hashes stored)
#   gate.py      require_fresh_face_token(): no token -> no loan application (403)
#   router.py    FastAPI endpoints /api/face/verify, /api/face/liveness and the /face page
#
# Explanation in Russian: docs/FACE_AUTH_RU.md
from face_auth.gate import require_fresh_face_token
from face_auth.router import create_router

__all__ = ["create_router", "require_fresh_face_token"]
