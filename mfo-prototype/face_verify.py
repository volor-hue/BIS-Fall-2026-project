# face_verify.py
# ------------------------------------------------------------
# Face verification: "is the person on the webcam the same person
# as on the 3x4 photo uploaded at registration?"
#
# Uses ONLY opencv-python (no dlib / CMake needed) and two small
# neural networks from the official OpenCV model zoo (ONNX files):
#   1) YuNet  - face DETECTOR: finds the face box + 5 landmarks (eyes, nose, mouth)
#   2) SFace  - face RECOGNIZER: turns an aligned face into a 128-number vector (embedding)
# Same person -> vectors point in a similar direction -> high cosine similarity.
#
#   from face_verify import verify
#   verify("uploads/client1_face_x.jpg", webcam_jpeg_bytes)
#   -> {"match": True, "score": 0.61, "threshold": 0.363, "reason": "..."}
# ------------------------------------------------------------
import os
import threading
import urllib.request

import telegram_env

# cv2 (OpenCV) and numpy are imported inside the functions below, so the website
# still starts on a computer where OpenCV is not installed yet (only the face check fails).

HERE = os.path.dirname(os.path.abspath(__file__))
# Settings: environment variable wins over the .env file next to this module.
_env_file = telegram_env.read_env_file(os.path.join(HERE, ".env"))


def _setting(name, default):
    return (os.environ.get(name) or _env_file.get(name) or default).strip()


MODEL_DIR = _setting("FACE_MODEL_DIR", os.path.join(HERE, "models"))
DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
MODEL_URLS = {
    DETECTOR_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + DETECTOR_FILE,
    RECOGNIZER_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/" + RECOGNIZER_FILE,
}

# Threshold recommended by the SFace authors for cosine similarity (>= means same person).
# Can be made stricter via env, e.g. FACE_THRESHOLD=0.45 (fewer false accepts, more false rejects).
COSINE_THRESHOLD = float(_setting("FACE_THRESHOLD", "0.363"))

# After a match the client gets a one-time face_token valid for this many minutes (1..60).
try:
    TOKEN_MINUTES = int(_setting("FACE_TOKEN_MINUTES", "5"))
    if not 1 <= TOKEN_MINUTES <= 60:
        raise ValueError
except ValueError:
    TOKEN_MINUTES = 5
DETECT_SCORE = 0.8          # how sure YuNet must be that a box is a face
MAX_SIDE = 1280             # big photos are shrunk first (faster, same accuracy)

_lock = threading.Lock()    # OpenCV DNN objects are not thread-safe
_detector = None
_recognizer = None


class FaceError(Exception):
    """Image problem we can explain to the user (no face, 2 faces, broken file)."""


def model_path(name):
    path = os.path.join(MODEL_DIR, name)
    if not os.path.exists(path):  # first run: download once (~38 MB total)
        os.makedirs(MODEL_DIR, exist_ok=True)
        urllib.request.urlretrieve(MODEL_URLS[name], path + ".part")
        os.replace(path + ".part", path)
    return path


def ensure_models():
    """Download both models if they are missing (run.py calls this at start)."""
    return [model_path(name) for name in MODEL_URLS]


def _models():
    import cv2
    global _detector, _recognizer
    if _detector is None:
        _detector = cv2.FaceDetectorYN.create(model_path(DETECTOR_FILE), "", (320, 320), DETECT_SCORE, 0.3, 5000)
        _recognizer = cv2.FaceRecognizerSF.create(model_path(RECOGNIZER_FILE), "")
    return _detector, _recognizer


def load_image(data):
    """data = file path (str) or raw bytes (jpg/png). Returns a BGR numpy image."""
    import cv2
    import numpy as np
    if isinstance(data, (str, os.PathLike)):
        if not os.path.exists(data):
            raise FaceError("image file not found")
        with open(data, "rb") as f:
            data = f.read()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise FaceError("bad image: cannot decode (send JPG or PNG)")
    h, w = image.shape[:2]
    if min(h, w) < 64:
        raise FaceError("bad image: too small")
    scale = MAX_SIDE / max(h, w)
    if scale < 1:
        image = cv2.resize(image, (int(w * scale), int(h * scale)))
    return image


def embed(image, label):
    """Find exactly one face, align it, return its 128-dim embedding."""
    detector, recognizer = _models()
    h, w = image.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(image)
    if faces is None or len(faces) == 0:
        raise FaceError(f"no face found on the {label} image")
    # Ignore tiny background faces (< 30% of the biggest face's width).
    biggest = max(f[2] for f in faces)
    faces = [f for f in faces if f[2] >= 0.3 * biggest]
    if len(faces) > 1:
        raise FaceError(f"{len(faces)} faces found on the {label} image - only one person must be in the frame")
    aligned = recognizer.alignCrop(image, faces[0])   # rotate/crop by eyes -> 112x112
    return recognizer.feature(aligned)


def verify(reference_image, live_image, threshold=None):
    """reference_image / live_image: path or bytes.
    Returns {"match", "score", "threshold", "reason"}. Never raises for bad input."""
    import cv2
    threshold = COSINE_THRESHOLD if threshold is None else threshold
    result = {"match": False, "score": None, "threshold": threshold}
    try:
        ref = load_image(reference_image)
        live = load_image(live_image)
        with _lock:
            ref_vec = embed(ref, "reference")
            live_vec = embed(live, "live")
            score = float(_recognizer.match(ref_vec, live_vec, cv2.FaceRecognizerSF_FR_COSINE))
    except FaceError as error:
        result["reason"] = str(error)
        return result
    result["score"] = round(score, 4)
    result["match"] = score >= threshold
    result["reason"] = "same person" if result["match"] else "different person (score below threshold)"
    return result
