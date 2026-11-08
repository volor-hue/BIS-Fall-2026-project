# telegram_env.py
# Load an optional local .env without extra dependencies.
# Existing process environment always wins. Values are never logged.
import os

import database


def load_local_env():
    """Read .env from the current directory, then from this file's directory."""
    paths = []
    for folder in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(folder, ".env")
        if path not in paths:
            paths.append(path)
    for path in paths:
        if os.path.isfile(path):
            _apply_env_file(path)


def bind_database_path():
    """Point database.DB_FILE at MFO_DB after .env has been read.

    Call this at process start (bot, operator CLI), before opening the database.
    The web app does not call it: export MFO_DB before uvicorn so both sides
    open the same absolute file.
    """
    load_local_env()
    configured = os.environ.get("MFO_DB", "").strip()
    if configured:
        database.DB_FILE = configured
    return os.path.abspath(database.DB_FILE)


def _apply_env_file(path):
    with open(path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key:
                os.environ.setdefault(key, value)
