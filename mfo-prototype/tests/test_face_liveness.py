# tests/test_face_liveness.py
# Unit tests of the face-check math with made-up numbers (no images, no OpenCV):
# head-turn geometry, mirror handling, best of several reference photos, random order.
import random

import pytest

from face_auth import liveness, matching
from face_fakes import FACES, fake_analyze, image, landmarks, unit


# ---------- yaw from 5 points ----------

def test_yaw_of_straight_face_is_zero():
    assert liveness.yaw_offset(landmarks(0.0)) == pytest.approx(0.0)


@pytest.mark.parametrize("yaw", [-0.4, -0.2, 0.1, 0.3])
def test_yaw_is_measured_in_eye_distances(yaw):
    # Same answer for a small and a big face (near/far from the camera).
    assert liveness.yaw_offset(landmarks(yaw, eye_distance=40)) == pytest.approx(yaw)
    assert liveness.yaw_offset(landmarks(yaw, eye_distance=200, x=500)) == pytest.approx(yaw)


def test_head_tilt_does_not_change_yaw():
    assert liveness.yaw_offset(landmarks(0.3, roll=0.4)) == pytest.approx(0.3)


def test_mirroring_flips_the_sign():
    # Flip the picture left <-> right: x -> width - x. The eyes swap places in YuNet's order.
    points = landmarks(0.3)
    flipped = [[640 - x, y] for x, y in points]
    flipped = [flipped[1], flipped[0], flipped[2], flipped[4], flipped[3]]
    assert liveness.yaw_offset(flipped) == pytest.approx(-0.3)


# ---------- one head-turn step ----------

def frames(*yaws, same=0.9):
    return [(y, same) for y in yaws]


def test_right_turn_passes():
    step = liveness.check_step("right", 0.0, frames(0.0, -0.15, -0.3), turn_min=0.2, same_person_min=0.3)
    assert step.passed and step.turn == pytest.approx(0.3)


def test_left_turn_passes():
    assert liveness.check_step("left", 0.0, frames(0.05, 0.25), turn_min=0.2, same_person_min=0.3).passed


def test_no_movement_fails():
    step = liveness.check_step("right", 0.0, frames(0.0, 0.01, -0.02, 0.0), turn_min=0.2, same_person_min=0.3)
    assert not step.passed and "не обнаружен" in step.reason


def test_wrong_direction_fails():
    step = liveness.check_step("right", 0.0, frames(0.0, 0.3), turn_min=0.2, same_person_min=0.3)
    assert not step.passed and "не в ту сторону" in step.reason


def test_turn_is_measured_from_the_straight_frame():
    # The person's "straight" was already a bit to the right (-0.1): -0.25 is only a 0.15 turn.
    assert not liveness.check_step("right", -0.1, frames(-0.25), turn_min=0.2, same_person_min=0.3,
                                   min_frames=1).passed


def test_another_person_in_one_frame_fails():
    step = liveness.check_step("left", 0.0, [(0.0, 0.9), (0.3, 0.1)], turn_min=0.2, same_person_min=0.3)
    assert not step.passed and "другой человек" in step.reason


def test_too_few_frames_fail():
    step = liveness.check_step("left", 0.0, frames(0.4), turn_min=0.2, same_person_min=0.3, min_frames=2)
    assert not step.passed and "мало кадров" in step.reason


def test_mirrored_camera_setting_swaps_directions():
    # FACE_CAMERA_MIRRORED=1: on a mirrored camera a right turn moves the nose to the RIGHT of the picture.
    assert liveness.check_step("right", 0.0, frames(0.0, 0.3), turn_min=0.2, same_person_min=0.3,
                               mirrored=True).passed
    assert not liveness.check_step("right", 0.0, frames(0.0, -0.3), turn_min=0.2, same_person_min=0.3,
                                   mirrored=True).passed


def test_straight_check():
    assert liveness.check_frontal(0.05, frontal_max=0.15)[0]
    assert not liveness.check_frontal(-0.3, frontal_max=0.15)[0]


def test_random_order_of_turns():
    orders = {tuple(liveness.new_steps(random.Random(seed))) for seed in range(20)}
    assert orders == {("right", "left"), ("left", "right")}


# ---------- similarity, mirror, best of references ----------

def test_cosine():
    assert matching.cosine(unit(0), unit(0)) == pytest.approx(1.0)
    assert matching.cosine(unit(0), unit(1)) == pytest.approx(0.0)
    assert matching.cosine([0.0] * 3, [1.0, 0, 0]) == 0.0


def sample(tag):
    return fake_analyze(image(tag))


def test_mirrored_live_face_uses_the_flipped_embedding():
    best = matching.best_match(sample("me_mirrored"), [("face", sample("me"))])
    assert best.score == pytest.approx(1.0) and best.mirrored is True
    best = matching.best_match(sample("me"), [("face", sample("me"))])
    assert best.mirrored is False


def test_best_of_several_references():
    refs = [("face", sample("bad_ref")), ("face_side", sample("me_side"))]
    best = matching.best_match(sample("me"), refs)
    assert best.reference == "face_side" and best.score == pytest.approx(1.0)


def test_other_person_is_low_against_every_reference():
    refs = [("face", sample("me")), ("face_side", sample("me_side"))]
    assert matching.best_match(sample("other"), refs).score < 0.363


def test_turned_face_of_same_person_stays_similar():
    # In the fakes a turned face is "a bit different" from the straight one, but not a stranger.
    yaw, turned, _ = FACES["me_right"]
    assert 0.3 < matching.cosine(turned, FACES["me"][1]) < 1.0
