# telegram_operator.py
# Local operator commands. There is no web endpoint for these actions.
# Whoever can run this file and read the SQLite file can issue a pairing code.
# Protect the computer account and the database file. Do not expose this CLI.
#
#   python telegram_operator.py issue --client-id 1
#   python telegram_operator.py status --client-id 1
#   python telegram_operator.py unlink --client-id 1
import argparse
import logging
import os
import sys

import database
import telegram_env
import telegram_service

log = logging.getLogger("mfo.telegram")


def _plain(value):
    return " ".join(str(value).split())


def _print_db_path():
    print(f"База: {os.path.abspath(database.DB_FILE)}")


def _command_issue(client_id):
    try:
        issued = telegram_service.issue_pairing_code(client_id)
    except telegram_service.TelegramServiceError as exc:
        if exc.code == "client_not_found":
            print("Клиент не найден.", file=sys.stderr)
        elif exc.code == "already_linked":
            print(
                "Клиент уже привязан к Telegram. Сначала отвяжите:\n"
                f"python telegram_operator.py unlink --client-id {client_id}",
                file=sys.stderr,
            )
        else:
            print("Не удалось выдать код.", file=sys.stderr)
        return 2
    _print_db_path()
    print(f"Клиент: {issued.client_id}")
    print(f"Имя: {_plain(issued.full_name)}")
    print(f"Код привязки (показывается один раз): {issued.code}")
    print(f"Действителен до: {issued.expires_at}")
    print("Передайте код клиенту лично или по доверенному каналу.")
    print(f"В личном чате с ботом клиент отправляет: /link {issued.code}")
    print("ИИН, телефон, почта и паспорт в это сообщение не выводятся.")
    return 0


def _command_status(client_id):
    status = telegram_service.client_link_status(client_id)
    if status is None:
        print("Клиент не найден.", file=sys.stderr)
        return 2
    _print_db_path()
    print(f"Клиент: {status['client_id']}")
    print(f"Имя: {_plain(status['full_name'])}")
    if status["telegram_user_id"] is None:
        print("Telegram: не привязан")
    else:
        print(f"Telegram user id: {status['telegram_user_id']}")
        print(f"Привязан: {status['linked_at']}")
    if status["active_code_expires_at"] is None:
        print("Активный код: нет (сами коды в базе не хранятся)")
    else:
        print(f"Активный код: есть, до {status['active_code_expires_at']} (текст кода не хранится)")
    return 0


def _command_unlink(client_id):
    result = telegram_service.unlink_client(client_id)
    if not result.client_found:
        print("Клиент не найден.", file=sys.stderr)
        return 2
    _print_db_path()
    if result.link_removed:
        print(f"Привязка клиента {client_id} удалена.")
    else:
        print(f"У клиента {client_id} не было привязки.")
    if result.codes_revoked:
        print(f"Отозвано неиспользованных кодов: {result.codes_revoked}.")
    else:
        print("Активных кодов не было.")
    return 0


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(
        description=(
            "Локальные команды оператора для привязки Telegram. "
            "Не публикуйте эту команду в интернет и не добавляйте для неё HTTP-эндпоинт."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("issue", "Выдать одноразовый код известному клиенту"),
        ("status", "Показать, привязан ли клиент (без текста кода и без ИИН)"),
        ("unlink", "Отвязать Telegram и отозвать активные коды клиента"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--client-id", type=int, required=True)
    args = parser.parse_args(argv)
    telegram_env.bind_database_path()
    database.init_db()
    if args.command == "issue":
        return _command_issue(args.client_id)
    if args.command == "status":
        return _command_status(args.client_id)
    if args.command == "unlink":
        return _command_unlink(args.client_id)
    print("Неизвестная команда.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
