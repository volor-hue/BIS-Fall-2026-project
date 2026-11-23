# run.py
# One command for the web app and the Telegram bot.
#   python run.py
#   python run.py --bot-only
#
# Loads .env next to this file (not from the current directory), resolves
# MFO_DB to one absolute path, and starts both processes with that path.
# Ctrl+C stops both. If one process exits, the other is stopped too.
# The bot token is never printed.
import argparse
import importlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from app.core import config
from app.core.config import DEFAULT_HOST, DEFAULT_PORT, parse_host, parse_port
from bot import main as telegram_bot
from bot import staff as telegram_staff

_STOP = threading.Event()
# Windows has no SIGKILL: there "kill" means proc.kill() (see _signal_tree).
FORCE_KILL = getattr(signal, "SIGKILL", None)


@dataclass(frozen=True)
class Plan:
    project_dir: str
    db_path: str
    host: str
    port: int
    start_web: bool
    start_bot: bool
    staff_count: int


def project_directory():
    return os.path.dirname(os.path.abspath(__file__))


def reset_shutdown():
    _STOP.clear()


def request_shutdown():
    _STOP.set()


def load_project_env(directory):
    """Read only <project>/.env. The process environment wins. cwd is ignored."""
    path = os.path.join(directory, ".env")
    if os.path.isfile(path):
        config.apply_env_file(path)


def resolve_db_path(directory):
    raw = os.environ.get("MFO_DB", "").strip() or "mfo.db"
    if not os.path.isabs(raw):
        raw = os.path.join(directory, raw)
    return os.path.abspath(raw)


def apply_runtime_env(directory):
    load_project_env(directory)
    db_path = resolve_db_path(directory)
    os.environ["MFO_DB"] = db_path
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return db_path


def port_is_free(host, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


def missing_modules(names):
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def child_commands(plan):
    commands = []
    if plan.start_web:
        commands.append([
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", plan.host, "--port", str(plan.port),
        ])
    if plan.start_bot:
        commands.append([sys.executable, "-m", "bot"])
    return commands


def child_env(plan):
    env = os.environ.copy()
    env["MFO_DB"] = plan.db_path
    env["MFO_HOST"] = plan.host
    env["MFO_PORT"] = str(plan.port)
    return env


def format_banner(plan):
    lines = [f"База: {plan.db_path}"]
    if plan.start_web:
        lines.append(f"Сайт: http://{plan.host}:{plan.port}")
    else:
        lines.append("Сайт не запускается (режим --bot-only).")
    if plan.start_bot:
        lines.append("Бот: long polling.")
        if plan.staff_count == 0:
            lines.append(
                "Список сотрудников пуст: /find выключен, /whoami работает. "
                "Добавьте id в MFO_TELEGRAM_STAFF_IDS и перезапустите."
            )
    else:
        lines.append("Бот не запускается (режим --web-only).")
    if plan.start_web and plan.start_bot:
        lines.append("Сайт и бот работают, пока это окно открыто. Остановка: Ctrl+C.")
    else:
        lines.append("Процесс работает, пока это окно открыто. Остановка: Ctrl+C.")
    return "\n".join(lines)


def port_busy_message(port):
    return (
        f"Порт {port} уже занят. Ни сайт, ни бот не запущены.\n"
        "Остановите программу на этом порту или выберите другой, например:\n"
        "  MFO_PORT=8002 python run.py\n"
        "По умолчанию используется 8001: порт 8000 часто уже занят."
    )


def _parser():
    parser = argparse.ArgumentParser(
        description="Запуск сайта и Telegram-бота одной командой."
    )
    parser.add_argument("--bot-only", action="store_true", help="Только бот, без сайта")
    parser.add_argument("--web-only", action="store_true", help="Только сайт, без бота")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", default=None)
    return parser


def build_plan(argv, project_dir):
    args = _parser().parse_args(argv)
    if args.bot_only and args.web_only:
        return None, "Укажите обычный запуск, --bot-only или --web-only, но не оба флага сразу."
    db_path = apply_runtime_env(project_dir)
    if not telegram_staff.allowlist_is_valid():
        return None, (
            "MFO_TELEGRAM_STAFF_IDS задан неверно. "
            "Нужны числовые Telegram id через запятую, например 100000001,100000002. "
            "Пустая строка допустима. Имена и @username нельзя. Значение не напечатано."
        )
    host = parse_host(args.host if args.host is not None else os.environ.get("MFO_HOST"))
    if host is None:
        return None, "MFO_HOST задан неверно. Пример: 127.0.0.1"
    port = parse_port(args.port if args.port is not None else os.environ.get("MFO_PORT"))
    if port is None:
        return None, "MFO_PORT должен быть числом от 1 до 65535. По умолчанию 8001."
    start_web = not args.bot_only
    start_bot = not args.web_only
    if start_bot and telegram_bot.read_token() is None:
        return None, (
            "Задайте TELEGRAM_BOT_TOKEN в .env рядом с run.py "
            "(шаблон в .env.example). Токен в это сообщение не выводится."
        )
    required = []
    if start_web:
        required.extend(["fastapi", "uvicorn"])
    if start_bot:
        required.append("aiogram")
    missing = missing_modules(required)
    if missing:
        return None, (
            "Не установлены зависимости: " + ", ".join(missing) + ". "
            "Выполните: pip install -r requirements.txt"
        )
    if start_web and not port_is_free(host, port):
        return None, port_busy_message(port)
    staff_ids = telegram_staff.current_staff_ids()
    return Plan(
        project_dir=os.path.abspath(project_dir),
        db_path=db_path,
        host=host,
        port=port,
        start_web=start_web,
        start_bot=start_bot,
        staff_count=len(staff_ids),
    ), None


def _popen(command, env, cwd):
    kwargs = {"cwd": cwd, "env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _signal_tree(proc, sig):
    if proc.poll() is not None:
        return
    if os.name == "nt":
        if sig == FORCE_KILL:
            proc.kill()
        else:
            proc.terminate()
        return
    if sig is None:
        sig = signal.SIGTERM
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def stop_processes(procs, grace=2.0):
    for proc in procs:
        _signal_tree(proc, signal.SIGTERM)
    deadline = time.time() + grace
    for proc in procs:
        remaining = deadline - time.time()
        try:
            proc.wait(timeout=max(0.1, remaining))
        except subprocess.TimeoutExpired:
            _signal_tree(proc, FORCE_KILL)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass


def supervise(commands, env, cwd, stop_event=None):
    """Run commands until stop_event or until any child exits. Always cleans up."""
    if stop_event is None:
        stop_event = _STOP
    procs = []
    exit_code = 0
    try:
        for command in commands:
            procs.append(_popen(command, env, cwd))
        while True:
            if stop_event.is_set():
                exit_code = 0
                return exit_code
            for proc in procs:
                code = proc.poll()
                if code is not None:
                    exit_code = code if code else 1
                    return exit_code
            stop_event.wait(0.2)
    finally:
        stop_processes(procs)


def prefetch_face_models():
    """Download the two face-check models (~38 MB) in the background on the first start,
    so the first webcam check does not wait for the download. Errors only print a hint."""
    def work():
        try:
            from app.face_auth import engine
            engine.ensure_models()
        except Exception as error:  # no internet / no OpenCV: the site still works
            print(f"Модели для проверки лица не скачаны ({type(error).__name__}). "
                  "Они скачаются при первой проверке лица.", file=sys.stderr)
    threading.Thread(target=work, daemon=True).start()


def main(argv=None):
    reset_shutdown()
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)

    def on_stop(signum, frame):
        request_shutdown()

    try:
        plan, error = build_plan(argv, project_directory())
        if error is not None:
            print(error, file=sys.stderr)
            if error.startswith("Порт "):
                return 3
            return 2
        print(format_banner(plan))
        # Settings that were wrong and replaced by defaults (JWT secret, limits, face thresholds...).
        for problem in config.check_settings():
            print("Предупреждение в настройках: " + problem, file=sys.stderr)
        if plan.start_web:
            prefetch_face_models()
        signal.signal(signal.SIGINT, on_stop)
        signal.signal(signal.SIGTERM, on_stop)
        try:
            return supervise(
                child_commands(plan),
                child_env(plan),
                plan.project_dir,
            )
        except KeyboardInterrupt:
            request_shutdown()
            return 0
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(main())
