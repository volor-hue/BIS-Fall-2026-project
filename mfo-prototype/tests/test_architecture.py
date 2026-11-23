# tests/test_architecture.py
# The layer rules of docs/ARCHITECTURE.md: imports go only DOWN.
# Every check imports a module in a FRESH Python process and looks at what got loaded with it.
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UPPER = ("app.api", "app.web", "app.main", "app.face_auth.router", "app.payments.router")


def loaded_modules(module, tmp_path):
    code = (
        "import importlib, json, sys; importlib.import_module(sys.argv[1]);"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith(('app', 'bot')))))"
    )
    env = os.environ.copy()
    env["MFO_DB"] = str(tmp_path / "arch.db")
    env["MFO_LOG_DIR"] = str(tmp_path / "logs")
    result = subprocess.run([sys.executable, "-c", code, module], cwd=ROOT, env=env,
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module", ["app.core.config", "app.core.security", "app.core.logs",
                                    "app.core.rate_limit"])
def test_core_depends_only_on_core(module, tmp_path):
    # Only the package "app" itself and app.core.* may be loaded.
    outside = [m for m in loaded_modules(module, tmp_path) if m != "app" and not m.startswith("app.core")]
    assert outside == [], outside


@pytest.mark.parametrize("module", ["app.db.database", "app.services.loans", "app.services.clients",
                                    "app.services.documents", "app.services.auth"])
def test_lower_layers_do_not_import_api_or_pages(module, tmp_path):
    upper = [m for m in loaded_modules(module, tmp_path) if m.startswith(UPPER)]
    assert upper == [], upper


@pytest.mark.parametrize("module", ["bot.main", "bot.operator_cli"])
def test_bot_uses_only_database_and_settings_of_the_web_app(module, tmp_path):
    web = [m for m in loaded_modules(module, tmp_path)
           if m.startswith("app.") and not m.startswith(("app.core", "app.db"))]
    assert web == [], web
