# face_auth/engine.py
# ------------------------------------------------------------
# The computer-vision part: picture -> face -> 5 points + 128-number "fingerprint".
#
# Uses ONLY opencv-python (no dlib / CMake) and two small neural networks
# from the official OpenCV model zoo (ONNX files, downloaded once into models/):
#   1) YuNet - face DETECTOR: finds the face box + 5 landmarks
#              (right eye, left eye, nose tip, right and left mouth corner)
#   2) SFace - face RECOGNIZER: turns an aligned 112x112 face into a 128-number vector (embedding)
# Same person -> vectors point in a similar direction -> high cosine similarity (matching.py).
#
# Mirror handling: we also compute the embedding of the horizontally flipped face
# (embedding_mirrored). A mirrored webcam picture of you then still matches your photo.
# ------------------------------------------------------------
import os
import threading
import urllib.request
from dataclasses import dataclass

from face_auth import config

# cv2 (OpenCV) and numpy are imported inside the functions, so the website still starts
# on a computer where OpenCV is not installed yet (only the face check answers 503).

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
MODEL_URLS = {
    DETECTOR_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + DETECTOR_FILE,
    RECOGNIZER_FILE: "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/" + RECOGNIZER_FILE,
}

_lock = threading.Lock()    # OpenCV DNN objects are not thread-safe
_detector = None
_recognizer = None


class FaceError(Exception):
    """Image problem we can explain to the user (no face, 2 faces, broken file)."""


class ManyFacesError(FaceError):
    """More than one person in the frame (for the head turn this is a hard fail)."""


@dataclass
class FaceSample:
    """Everything we need to know about ONE face on ONE picture."""
    landmarks: list            # 5 points [[x, y], ...]: right eye, left eye, nose, right mouth, left mouth
    embedding: object          # numpy vector, 128 numbers
    embedding_mirrored: object  # the same face flipped left <-> right
    detect_score: float = 1.0


def model_path(name):
    path = os.path.join(config.MODEL_DIR, name)
    if not os.path.exists(path):  # first run: download once (~38 MB total)
        os.makedirs(config.MODEL_DIR, exist_ok=True)
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
        _detector = cv2.FaceDetectorYN.create(model_path(DETECTOR_FILE), "", (320, 320),
                                              config.DETECT_SCORE, 0.3, 5000)
        _recognizer = cv2.FaceRecognizerSF.create(model_path(RECOGNIZER_FILE), "")
    return _detector, _recognizer


def load_image(data):
    """data = file path (str) or raw bytes (jpg/png). Returns a BGR numpy image."""
    import cv2
    import numpy as np
    if isinstance(data, (str, os.PathLike)):
        if not os.path.exists(data):
            raise FaceError("файл с фото не найден")
        with open(data, "rb") as f:
            data = f.read()
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None
    if image is None:
        raise FaceError("картинку не удалось прочитать (нужен JPG или PNG)")
    h, w = image.shape[:2]
    if min(h, w) < 64:
        raise FaceError("картинка слишком маленькая")
    scale = config.MAX_SIDE / max(h, w)
    if scale < 1:
        image = cv2.resize(image, (int(w * scale), int(h * scale)))
    return image


def analyze(data, label="live", single=True):
    """Find the face, return a FaceSample (5 points + embedding + mirrored embedding).

    single=True  (webcam frames): exactly one person must be in the frame.
    single=False (registration photos): take the biggest face.
    Raises FaceError with a human-readable reason."""
    import cv2
    import numpy as np
    image = load_image(data)
    with _lock:
        detector, recognizer = _models()
        h, w = image.shape[:2]
        detector.setInputSize((w, h))
        _, faces = detector.detect(image)
        if faces is None or len(faces) == 0:
            raise FaceError(f"лицо не найдено ({label})")
        # Ignore tiny background faces (< 30% of the biggest face's width).
        biggest = max(f[2] for f in faces)
        faces = [f for f in faces if f[2] >= 0.3 * biggest]
        if single and len(faces) > 1:
            raise ManyFacesError(f"в кадре {len(faces)} лица ({label}) - в кадре должен быть один человек")
        face = max(faces, key=lambda f: f[2])
        aligned = recognizer.alignCrop(image, face)          # rotate/crop by the eyes -> 112x112
        embedding = recognizer.feature(aligned).flatten()
        mirrored = recognizer.feature(cv2.flip(aligned, 1)).flatten()  # 1 = flip left <-> right
    landmarks = np.asarray(face[4:14], dtype=float).reshape(5, 2).tolist()
    return FaceSample(landmarks=landmarks, embedding=embedding,
                      embedding_mirrored=mirrored, detect_score=float(face[14]))
