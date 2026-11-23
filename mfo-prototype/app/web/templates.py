# app/web/templates.py
# One Jinja2 environment for every HTML page (pages.py, the face page, error pages).
# The folder is found from the project folder, so the app works from any current directory.
from fastapi.templating import Jinja2Templates

from app.core import config

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)
# The camera template (_face_camera.html) mirrors the preview only if the camera itself does not.
templates.env.globals["face_camera_mirrored"] = config.face.CAMERA_MIRRORED
