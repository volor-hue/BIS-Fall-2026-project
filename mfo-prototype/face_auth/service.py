# face_auth/service.py
# ------------------------------------------------------------
# The face check as plain functions (the web layer is in router.py).
#
#   ENROLLMENT (registration, "Пройти биометрию"):
#     enroll():        frames "look straight" + frames of the head turns (random order).
#                      Live single face, same person everywhere, real turns ->
#                      3 references are kept: straight, best right-turn, best left-turn frame.
#
#   LOAN CHECK (before every application):
#     start_check():   "look straight" snapshot vs ALL enrolled references, normal and mirrored
#                      -> best score >= FACE_THRESHOLD -> the server creates a head-turn challenge.
#     finish_check():  frames of the turns RIGHT and LEFT -> both OK -> face_token (router.py).
#
# Nothing here is saved to disk: images are only in memory during the request.
# Only the 128-number vectors (not pictures) of the enrollment are stored (storage.py).
# ------------------------------------------------------------
from face_auth import config, engine, liveness, matching
from face_auth.engine import FaceError, FaceSample, ManyFacesError


def vector_sample(embedding):
    """A stored 128-number vector as a FaceSample (only .embedding is used for comparing)."""
    import numpy as np
    vector = np.asarray(embedding, dtype=float)
    return FaceSample(landmarks=[], embedding=vector, embedding_mirrored=vector)


def references_from_db(rows):
    """storage.get_references() rows -> [(kind, FaceSample), ...] for matching.best_match."""
    return [(row["kind"], vector_sample(row["embedding"])) for row in rows]


def as_reference(kind, sample):
    """FaceSample -> the dict that storage.py saves (vector + yaw, no picture)."""
    return {"kind": kind, "embedding": [round(float(x), 6) for x in sample.embedding],
            "yaw": round(liveness.yaw_offset(sample.landmarks), 4)}


class TurnsFailed(Exception):
    def __init__(self, reason, steps):
        super().__init__(reason)
        self.reason, self.steps = reason, steps


def check_turns(steps, baseline_yaw, identity_refs, frames_by_step):
    """Shared by enrollment and the loan check.
    For each requested direction: every frame -> one face? yaw? same person as identity_refs?
    Returns (step_results, best_frame_per_direction). Raises TurnsFailed."""
    results, best = [], {}
    for direction in steps:
        measured, samples = [], []   # (yaw, same_person_score) and the FaceSample of every frame with a face
        for frame in frames_by_step.get(direction) or []:
            try:
                sample = engine.analyze(frame, label="кадр поворота")
            except ManyFacesError as error:   # a second person appeared: fail at once
                raise TurnsFailed(str(error), [r.as_dict() for r in results])
            except FaceError:
                continue  # head turned so far that the face is not found - just skip this frame
            measured.append((liveness.yaw_offset(sample.landmarks),
                             matching.best_match(sample, identity_refs).score))
            samples.append(sample)
        step = liveness.check_step(direction, baseline_yaw, measured)
        results.append(step)
        if not step.passed:
            raise TurnsFailed(step.reason, [r.as_dict() for r in results])
        # The frame turned the most in the requested direction becomes this side's reference.
        sign = liveness.direction_sign(direction)
        best[direction] = max(samples, key=lambda s: (liveness.yaw_offset(s.landmarks) - baseline_yaw) * sign)
    return results, best


# =============== ENROLLMENT ===============

def enroll(steps, front_frames, frames_by_step):
    """Returns {"passed", "reason", "steps", "references"} (references only if passed)."""
    fail = lambda reason, done=(): {"passed": False, "reason": reason, "steps": list(done)}
    fronts = []
    for frame in front_frames:
        try:
            fronts.append(engine.analyze(frame, label="снимок «смотрите прямо»"))
        except ManyFacesError as error:
            return fail(str(error))
        except FaceError:
            continue
    if not fronts:
        return fail("лицо не найдено. Сядьте лицом к камере, при хорошем свете")
    # The most straight frame is the main reference.
    anchor = min(fronts, key=lambda s: abs(liveness.yaw_offset(s.landmarks)))
    yaw = liveness.yaw_offset(anchor.landmarks)
    ok, reason = liveness.check_frontal(yaw)
    if not ok:
        return fail(reason)
    identity = [("front", anchor)]
    for other in fronts:   # the other straight frames must be the same person too
        if matching.best_match(other, identity).score < config.TURN_SAME_PERSON:
            return fail("в кадрах «смотрите прямо» разные лица")
    try:
        results, best = check_turns(steps, yaw, identity, frames_by_step)
    except TurnsFailed as failed:
        return fail(failed.reason, failed.steps)
    references = [as_reference("front", anchor)] + [as_reference(d, best[d]) for d in liveness.DIRECTIONS]
    return {"passed": True, "reason": "биометрия пройдена: лицо и повороты головы сохранены",
            "steps": [r.as_dict() for r in results], "references": references}


# =============== LOAN CHECK ===============

def start_check(references, live_image, threshold=None):
    """STEP 1. references: [(kind, FaceSample)] from references_from_db().
    If it matched, result["anchor"] holds what the server must remember for step 2
    (router.py stores it and removes it from the answer)."""
    threshold = config.COSINE_THRESHOLD if threshold is None else threshold
    result = {"match": False, "score": None, "threshold": threshold}
    try:
        live = engine.analyze(live_image, label="снимок с камеры")
    except FaceError as error:
        result["reason"] = str(error)
        return result

    best = matching.best_match(live, references)
    result.update(score=best.score, best_reference=best.reference, mirrored=best.mirrored)
    result["match"] = best.score >= threshold
    if not result["match"]:
        result["reason"] = "это другой человек (сходство ниже порога)"
        return result

    yaw = liveness.yaw_offset(live.landmarks)
    ok, reason = liveness.check_frontal(yaw)
    if not ok:  # the same person, but not looking straight -> the turns could not be measured
        result.update(match=False, reason=reason)
        return result
    result["reason"] = "тот же человек"
    result["anchor"] = {"embedding": [float(x) for x in live.embedding], "yaw": round(yaw, 4)}
    return result


def finish_check(challenge, references, frames_by_step):
    """STEP 2. challenge = {"steps": [...], "anchor": {...}} from storage.consume_challenge.
    references: [(kind, FaceSample)]. frames_by_step = {"right": [bytes, ...], "left": [bytes, ...]}.
    Returns {"passed", "reason", "steps": [...]}."""
    anchor = challenge["anchor"]
    identity = [("camera", vector_sample(anchor["embedding"]))] + list(references)
    try:
        results, _ = check_turns(challenge["steps"], anchor["yaw"], identity, frames_by_step)
    except TurnsFailed as failed:
        return {"passed": False, "reason": failed.reason, "steps": failed.steps}
    return {"passed": True, "reason": "живой человек: оба поворота головы подтверждены",
            "steps": [r.as_dict() for r in results]}
