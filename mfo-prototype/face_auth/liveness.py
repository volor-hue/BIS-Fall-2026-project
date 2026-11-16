# face_auth/liveness.py
# ------------------------------------------------------------
# Liveness = "is a LIVE person in front of the camera, not a photo on a phone?"
#
# Idea: a real head is 3D, a photo is flat.
# When a real person turns the head, the NOSE TIP (it sticks out) moves sideways much more
# than the eyes and the mouth corners. On a flat photo all 5 points move together,
# so the position of the nose relative to the eyes does not change, even if the phone is tilted.
#
# YAW (left/right turn) from the 5 YuNet points:
#   center = average of the 2 eyes and the 2 mouth corners      (middle of the face)
#   eye_distance = distance between the eyes                    (makes it size-independent)
#   yaw = (nose_x - center_x) / eye_distance                    (along the eye line, so head tilt does not matter)
#     straight face  ~ 0.0
#     turned ~25 deg ~ 0.2      turned ~45 deg ~ 0.45
#
# Direction (camera picture NOT mirrored, the way the browser sends it):
#   the person turns to THEIR right -> the nose moves to the LEFT of the picture -> yaw goes DOWN (negative)
#   the person turns to THEIR left  -> the nose moves to the RIGHT of the picture -> yaw goes UP (positive)
#
# This file is pure math (no OpenCV), so it is tested with made-up points.
# ------------------------------------------------------------
from dataclasses import dataclass, field

from face_auth import config

DIRECTIONS = ("right", "left")
RU = {"right": "вправо", "left": "влево"}


def yaw_offset(landmarks):
    """landmarks: 5 points [[x, y], ...] in YuNet order. Returns the yaw number (see above)."""
    (rex, rey), (lex, ley), (nx, ny), (rmx, rmy), (lmx, lmy) = [(float(p[0]), float(p[1])) for p in landmarks]
    # Unit vector along the eye line (from the first eye to the second one).
    dx, dy = lex - rex, ley - rey
    eye_distance = (dx * dx + dy * dy) ** 0.5
    if eye_distance < 1e-6:
        return 0.0
    ux, uy = dx / eye_distance, dy / eye_distance
    cx = (rex + lex + rmx + lmx) / 4
    cy = (rey + ley + rmy + lmy) / 4
    # How far the nose is from the middle of the face, measured along the eye line.
    return ((nx - cx) * ux + (ny - cy) * uy) / eye_distance


def direction_sign(direction, mirrored=None):
    """+1 if the requested turn makes yaw go up, -1 if it makes yaw go down."""
    mirrored = config.CAMERA_MIRRORED if mirrored is None else mirrored
    sign = -1 if direction == "right" else 1
    return -sign if mirrored else sign


@dataclass
class StepResult:
    direction: str
    passed: bool
    reason: str
    turn: float = 0.0            # how far the head turned in the requested direction (yaw units)
    frames: int = 0              # frames with a face
    min_same_person: float = None  # the lowest identity score among the frames
    details: dict = field(default_factory=dict)

    def as_dict(self):
        return {"direction": self.direction, "passed": self.passed, "reason": self.reason,
                "turn": round(self.turn, 3), "frames": self.frames,
                "min_same_person": None if self.min_same_person is None else round(self.min_same_person, 3)}


def check_step(direction, baseline_yaw, frames, turn_min=None, same_person_min=None,
               min_frames=None, mirrored=None):
    """Did the head really turn in `direction`, and was it the same person all the time?

    direction:    "right" or "left" (the person's own right/left)
    baseline_yaw: yaw of the "look straight" frame from step 1
    frames:       list of (yaw, same_person_score) for every frame of this step that had a face
    Returns StepResult."""
    turn_min = config.TURN_MIN if turn_min is None else turn_min
    same_person_min = config.TURN_SAME_PERSON if same_person_min is None else same_person_min
    min_frames = config.MIN_FRAMES if min_frames is None else min_frames
    word = RU.get(direction, direction)
    if direction not in DIRECTIONS:
        return StepResult(direction, False, "неизвестное направление")
    if len(frames) < min_frames:
        return StepResult(direction, False, f"мало кадров с лицом ({len(frames)} из нужных {min_frames}). "
                          "Держите лицо в кадре", frames=len(frames))

    sign = direction_sign(direction, mirrored)
    # Movement of every frame compared with the straight frame, positive = requested direction.
    moves = [(yaw - baseline_yaw) * sign for yaw, _ in frames]
    turn = max(moves)                   # the biggest turn in the requested direction
    wrong = -min(moves)                 # the biggest turn in the OTHER direction
    same = min(score for _, score in frames)
    result = StepResult(direction, False, "", turn=turn, frames=len(frames), min_same_person=same)

    # 1) Same person in EVERY frame (nobody can swap faces in the middle).
    if same < same_person_min:
        result.reason = f"в одном из кадров другой человек или лицо не похоже (сходство {same:.2f} < {same_person_min})"
    # 2) Turned the wrong way?
    elif turn < turn_min and wrong >= turn_min:
        result.reason = f"голова повернулась не в ту сторону. Нужно повернуть {word}"
    # 3) Not enough movement: a photo or a video of a still face ends up here.
    elif turn < turn_min:
        result.reason = (f"поворот {word} не обнаружен (сдвиг {turn:.2f}, нужно не меньше {turn_min}). "
                         "Поверните голову сильнее")
    else:
        result.passed = True
        result.reason = f"поворот {word} есть"
    return result


def check_frontal(baseline_yaw, frontal_max=None):
    """The step-1 frame must look roughly straight at the camera. Returns (ok, reason)."""
    frontal_max = config.FRONTAL_MAX if frontal_max is None else frontal_max
    if abs(baseline_yaw) > frontal_max:
        return False, "смотрите прямо в камеру и начните проверку заново"
    return True, ""


def new_steps(rng=None):
    """Random order of the two turns (a pre-recorded video does not know the order)."""
    import secrets
    steps = list(DIRECTIONS)
    if (rng or secrets.SystemRandom()).random() < 0.5:
        steps.reverse()
    return steps
