# face_auth/config.py
# ------------------------------------------------------------
# All settings of the face check in ONE place.
# Every value can be changed in the environment or in mfo-prototype/.env
# (the environment variable wins). Wrong values fall back to the default.
# ------------------------------------------------------------
import os

import telegram_env  # the team's small .env reader (also used by the Telegram bot)

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_file = telegram_env.read_env_file(os.path.join(PROJECT_DIR, ".env"))


def setting(name, default):
    """Text value: environment variable -> .env file -> default."""
    return str(os.environ.get(name) or _env_file.get(name) or default).strip()


def number(name, default, low, high, kind=float):
    """Number value inside [low, high]; anything else -> default."""
    try:
        value = kind(setting(name, default))
    except ValueError:
        return default
    return value if low <= value <= high else default


def flag(name, default):
    return setting(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


# ---------- models ----------
MODEL_DIR = setting("FACE_MODEL_DIR", os.path.join(PROJECT_DIR, "models"))
DETECT_SCORE = 0.8   # how sure YuNet must be that a box is a face
# Big photos are shrunk to this size first: faster, and YuNet is MORE sure on a medium-size face
# (a close-up 3x4 photo at full resolution can get a detector score below DETECT_SCORE).
MAX_SIDE = 640

# ---------- step 1: "is it the same person as on the registration photo?" ----------
# Cosine similarity threshold recommended by the SFace authors (>= means same person).
COSINE_THRESHOLD = number("FACE_THRESHOLD", 0.363, 0.0, 1.0)

# ---------- step 2: liveness (head turn) ----------
# FACE_LIVENESS=0 turns the head-turn step off (the token is issued right after step 1).
LIVENESS_ENABLED = flag("FACE_LIVENESS", True)
# How long the client has to finish the head turns after step 1 (seconds).
CHALLENGE_SECONDS = number("FACE_CHALLENGE_SECONDS", 120, 20, 600, int)
# "Yaw" = how far the nose is shifted sideways, measured in eye distances (see liveness.py).
#   straight face ~ 0.0, head turned ~25 degrees ~ 0.2, ~45 degrees ~ 0.45
# The head must turn at least this much (compared with the "look straight" frame).
TURN_MIN = number("FACE_TURN_MIN", 0.20, 0.05, 1.0)
# The "look straight" frame must be at most this far from 0.
FRONTAL_MAX = number("FACE_FRONTAL_MAX", 0.15, 0.02, 0.5)
# Every turned frame must still be the same person. A turned face looks less like the straight
# photo, so this threshold is lower than FACE_THRESHOLD (unknown people score about 0.0-0.2).
TURN_SAME_PERSON = number("FACE_TURN_SAME_PERSON", 0.30, 0.0, 1.0)
# At least this many frames with a face are needed in each step.
MIN_FRAMES = number("FACE_TURN_MIN_FRAMES", 2, 1, 20, int)
MAX_FRAMES = 12      # frames per step the server accepts (the browser sends 8)
# Set to 1 only if YOUR camera driver already mirrors the picture (left and right swapped).
# The browser page sends raw (not mirrored) frames, so normally this stays 0.
CAMERA_MIRRORED = flag("FACE_CAMERA_MIRRORED", False)

# ---------- the one-time token for the loan application ----------
TOKEN_MINUTES = number("FACE_TOKEN_MINUTES", 5, 1, 60, int)

# ---------- uploads ----------
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024   # one webcam snapshot
MAX_FRAME_BYTES = 2 * 1024 * 1024      # one liveness frame
