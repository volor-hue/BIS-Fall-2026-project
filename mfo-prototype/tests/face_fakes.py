# tests/face_fakes.py
# Made-up faces for the face-check tests: NO real photos and NO OpenCV models.
# A fake "image" is a JPEG signature + a tag, e.g. FAKE_JPEG + b"|me_right".
# fake_analyze() turns the tag into a FaceSample with chosen 5 points and embedding.
import numpy as np

from app.face_auth.engine import FaceError, FaceSample, ManyFacesError

FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


def image(tag):
    return FAKE_JPEG + b"|" + tag.encode()


def landmarks(yaw, eye_distance=60.0, x=100.0, y=100.0, roll=0.0):
    """5 YuNet points of a face whose nose is shifted by `yaw` eye distances.
    roll (radians) tilts the whole face, to show that head tilt does not change the yaw."""
    points = np.array([[0, 0], [1, 0], [0.5 + yaw, 0.5], [0.15, 1.0], [0.85, 1.0]]) * eye_distance
    c, s = np.cos(roll), np.sin(roll)
    points = points @ np.array([[c, s], [-s, c]])
    return (points + [x, y]).tolist()


def unit(index, mix=None):
    v = np.zeros(128)
    v[index] = 1.0
    if mix is not None:          # (other_index, weight): a slightly different picture of the same person
        v[mix[0]] = mix[1]
    return v / np.linalg.norm(v)


ME = unit(0)
# tag -> (yaw, embedding, mirrored embedding)
FACES = {
    "me": (0.0, ME, unit(5)),
    "me_right": (-0.35, unit(0, (1, 0.6)), unit(5)),     # the person turned to THEIR right
    "me_left": (0.35, unit(0, (2, 0.6)), unit(5)),
    "me_slight_right": (-0.08, ME, unit(5)),            # a tiny wobble, not a real turn
    "me_mirrored": (0.0, unit(6), ME),                  # a mirrored webcam picture of me
    "me_not_straight": (0.4, ME, unit(5)),
    "other": (0.0, unit(3), unit(4)),
    "other_right": (-0.35, unit(3), unit(4)),
}


def fake_analyze(data, label="live"):
    tag = data.split(b"|")[-1].decode(errors="ignore") if b"|" in data else "me"
    if tag == "noface":
        raise FaceError(f"лицо не найдено ({label})")
    if tag == "two":
        raise ManyFacesError(f"в кадре 2 лица ({label})")
    yaw, embedding, mirrored = FACES[tag]
    return FaceSample(landmarks=landmarks(yaw), embedding=embedding, embedding_mirrored=mirrored)
