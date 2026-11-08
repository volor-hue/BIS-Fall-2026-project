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

import telegram_bot
import telegram_env
import telegram_staff

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
_STOP = threading.Event()


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
        telegram_env._apply_env_file(path)


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


def parse_port(raw):
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PORT
    text = str(raw).strip()
    if not text.isdigit():
        return None
    value = int(text)
    if value < 1 or value > 65535:
        return None
    return value


def parse_host(raw):
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HOST
    host = str(raw).strip()
    if len(host) > 253 or any(char.isspace() for char in host):
        return None
    if "/" in host or "\\" in host or ":" in host:
        return None
    return host


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
            sys.executable, "-m", "uvicorn", "main:app",
            "--host", plan.host, "--port", str(plan.port),
        ])
    if plan.start_bot:
        commands.append([
            sys.executable, os.path.join(plan.project_dir, "telegram_bot.py"),
        ])
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
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return
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
            _signal_tree(proc, signal.SIGKILL)
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
