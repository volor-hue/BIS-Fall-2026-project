# face_auth/matching.py
# ------------------------------------------------------------
# "Is it the same person?" = cosine similarity of two embeddings.
#   cos = (a . b) / (|a| * |b|)     1.0 = same direction, ~0 = unrelated
#
# Best of everything:
#   - the client may have several reference photos (straight 3x4 + optional side photo);
#   - the webcam picture may be mirrored.
# We compare the live face (normal AND flipped) with every reference and keep the highest score.
# A different person stays low for every pair, so taking the best one is safe.
# ------------------------------------------------------------
from dataclasses import dataclass


def cosine(a, b):
    import numpy as np
    a = np.asarray(a, dtype=float).ravel()
    b = np.asarray(b, dtype=float).ravel()
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(a @ b) / norm if norm else 0.0


@dataclass
class Match:
    score: float       # best cosine similarity found
    reference: str     # which reference gave it, e.g. "face" or "face_side"
    mirrored: bool     # True = the flipped live face matched better


def best_match(live, references):
    """live: FaceSample. references: list of (name, FaceSample). Returns Match or None."""
    best = None
    for name, ref in references:
        for mirrored, vector in ((False, live.embedding), (True, live.embedding_mirrored)):
            score = cosine(vector, ref.embedding)
            if best is None or score > best.score:
                best = Match(round(score, 4), name, mirrored)
    return best
