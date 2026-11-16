# face_auth/service.py
# ------------------------------------------------------------
# The whole face check as two steps (the web layer is in router.py):
#
#   STEP 1  start_check():   webcam snapshot "look straight" vs ALL registration photos
#                            (3x4 + optional side photo), normal and mirrored -> best score.
#                            score >= FACE_THRESHOLD -> the server creates a head-turn challenge.
#   STEP 2  finish_check():  frames recorded while the person turned RIGHT and LEFT
#                            (random order). For every frame: same person? how far did the nose move?
#                            Both turns OK -> the one-time face_token is issued (router.py).
#
# Nothing here is saved to disk: images are only in memory during the request.
# ------------------------------------------------------------
from face_auth import config, engine, liveness, matching
from face_auth.engine import FaceError, FaceSample, ManyFacesError


def load_references(reference_files):
    """reference_files: list of (kind, path). Photos without a detectable face are SKIPPED
    (a real profile photo is often not found by YuNet, which is trained on frontal faces)."""
    references, skipped = [], []
    for kind, path in reference_files:
        try:
            references.append((kind, engine.analyze(path, label=kind, single=False)))
        except FaceError:
            skipped.append(kind)
    return references, skipped


def start_check(reference_files, live_image, threshold=None):
    """STEP 1. Returns a dict for the API. If it matched, result["anchor"] holds what the
    server must remember for step 2 (router.py stores it and removes it from the answer)."""
    threshold = config.COSINE_THRESHOLD if threshold is None else threshold
    result = {"match": False, "score": None, "threshold": threshold}
    references, skipped = load_references(reference_files)
    result["references_used"] = [kind for kind, _ in references]
    result["references_skipped"] = skipped
    if not references:
        result["reason"] = "на фото из регистрации не найдено лицо. Загрузите чёткое фото 3x4"
        return result
    try:
        live = engine.analyze(live_image, label="снимок с камеры", single=True)
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


def same_person_score(sample, anchor_sample, references):
    """Best similarity of one turned frame with the straight webcam frame or any registration photo."""
    return matching.best_match(sample, [("camera", anchor_sample)] + references).score


def finish_check(challenge, reference_files, frames_by_step):
    """STEP 2. challenge = {"steps": [...], "anchor": {...}} from storage.consume_challenge.
    frames_by_step = {"right": [bytes, ...], "left": [bytes, ...]}.
    Returns {"passed", "reason", "steps": [...]}."""
    import numpy as np
    anchor = challenge["anchor"]
    vector = np.asarray(anchor["embedding"], dtype=float)
    anchor_sample = FaceSample(landmarks=[], embedding=vector, embedding_mirrored=vector)
    references, _ = load_references(reference_files)

    results = []
    for direction in challenge["steps"]:
        measured = []   # (yaw, same_person_score) of every frame with a face
        for frame in frames_by_step.get(direction) or []:
            try:
                sample = engine.analyze(frame, label="кадр поворота", single=True)
            except ManyFacesError as error:   # a second person appeared: fail at once
                return {"passed": False, "reason": str(error), "steps": [r.as_dict() for r in results]}
            except FaceError:
                continue  # head turned so far that the face is not found - just skip this frame
            measured.append((liveness.yaw_offset(sample.landmarks),
                             same_person_score(sample, anchor_sample, references)))
        step = liveness.check_step(direction, anchor["yaw"], measured)
        results.append(step)
        if not step.passed:
            break
    passed = len(results) == len(challenge["steps"]) and all(r.passed for r in results)
    reason = "живой человек: оба поворота головы подтверждены" if passed else results[-1].reason
    return {"passed": passed, "reason": reason, "steps": [r.as_dict() for r in results]}
