# log_config.py
# ------------------------------------------------------------
# Logging for the web app (standard Python "logging", no extra libraries).
#
# Two files in the logs/ folder:
#   logs/app.log    - technical log: every HTTP request, warnings, errors with traceback.
#   logs/audit.log  - security journal: WHO did WHAT and WHEN
#                     (registration, login, logout, applications, decisions, payments...).
# Audit lines are also copied into app.log and the console, so app.log is the full timeline.
#
# Files "rotate": when app.log reaches 1 MB it becomes app.log.1, the old app.log.1
# becomes app.log.2, and so on. Only the last few files are kept, so the disk never fills up.
#
# RULE: never log passwords, password hashes, IIN, phone numbers, cookies or tokens.
# ------------------------------------------------------------
import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = os.environ.get("MFO_LOG_DIR", "logs")
LOG_LEVEL = os.environ.get("MFO_LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
MAX_BYTES = 1_000_000

# "mfo.web" is for requests and errors, "mfo.audit" is for the security journal.
# Both are children of "mfo", so everything set up on "mfo" receives both.
log = logging.getLogger("mfo.web")
audit_log = logging.getLogger("mfo.audit")


def _file_handler(file_name, backups):
    handler = RotatingFileHandler(os.path.join(LOG_DIR, file_name), maxBytes=MAX_BYTES,
                                  backupCount=backups, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def setup_logging():
    parent = logging.getLogger("mfo")
    if getattr(parent, "mfo_configured", False):
        return  # already done (the module was imported twice)

    level = logging.getLevelName(LOG_LEVEL)
    if not isinstance(level, int):
        level = logging.INFO  # unknown name in MFO_LOG_LEVEL -> use INFO
    parent.setLevel(level)

    os.makedirs(LOG_DIR, exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    parent.addHandler(console)
    parent.addHandler(_file_handler("app.log", backups=5))
    # The audit file gets ONLY audit lines and keeps a longer history.
    audit_log.addHandler(_file_handler("audit.log", backups=20))
    parent.mfo_configured = True


def clean(value):
    # Turn any value into one safe "word" for a log line.
    # Line breaks and other control characters are replaced with "?", so a user cannot type
    # "x\n... event=login_ok ..." into a form and add a fake line to the log (log injection).
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value))[:100]
    if text == "" or " " in text or "=" in text or '"' in text:
        text = '"' + text.replace('"', "'") + '"'
    return text


def mask_login(login):
    # "web@test.kz" -> "we***@test.kz", "admin" -> "ad***".
    # Enough to notice someone guessing passwords, without saving the full e-mail.
    name, at, domain = str(login).partition("@")
    return name[:2] + "***" + at + domain


def audit(event, request=None, level=logging.INFO, **fields):
    # One audit line looks like:
    #   2026-11-09 12:00:00,123 WARNING mfo.audit event=login_failed ip=127.0.0.1 user=anon login=ad***
    # "user" is who sent the request (set by the request middleware in main.py).
    parts = ["event=" + clean(event)]
    if request is not None:
        parts.append("ip=" + clean(request.client.host if request.client else "-"))
        parts.append("user=" + clean(getattr(request.state, "user", "-")))
    for key, value in fields.items():
        parts.append(key + "=" + clean(value))
    audit_log.log(level, " ".join(parts))
