# telegram_bot.py
# Russian client bot: link with a one-time office code, then read own
# applications and repayment schedules. Long polling. Token from the environment.
#
# Run from the mfo-prototype directory:
#   python telegram_bot.py
#
# There is no HTTP endpoint here. /start arguments are ignored on purpose.
import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import database
import telegram_env
import telegram_service
import telegram_staff
import telegram_text

log = logging.getLogger("mfo.telegram")
router = Router(name="mfo_telegram")

STAFF_DENIED = (
    "Эта команда только для сотрудника из MFO_TELEGRAM_STAFF_IDS. "
    "Чужие клиенты не показываются. Свой числовой id: /whoami"
)

PLACEHOLDER_TOKENS = {
    "",
    "PUT_YOUR_TOKEN_HERE",
    "your_token_here",
    "changeme",
}


_dispatcher = None


def build_dispatcher():
    """One dispatcher per process. A router cannot be attached twice."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = Dispatcher()
        _dispatcher.include_router(router)
    return _dispatcher


def _user_id(event):
    user = event.from_user
    if user is None or user.is_bot:
        return None
    if isinstance(user.id, bool) or not isinstance(user.id, int) or user.id <= 0:
        return None
    return user.id


def _private_message(message):
    return message.chat is not None and message.chat.type == "private"


async def _send_parts(message, text, reply_markup=None):
    parts = telegram_text.split_text(text)
    for index, part in enumerate(parts):
        markup = reply_markup if index == 0 else None
        await message.answer(part, reply_markup=markup)


def _keyboard(pairs):
    if not pairs:
        return None
    rows = [
        [InlineKeyboardButton(text=label[:60], callback_data=data)]
        for label, data in pairs
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.chat.type != "private")
async def on_non_private(message: Message):
    # Group, supergroup, and channel: never read a code and never query loans.
    log.info("ignored non-private chat_type=%s", message.chat.type)
    if message.text and message.text.startswith("/"):
        await message.answer(telegram_text.GROUP_TEXT)


@router.message(CommandStart())
async def cmd_start(message: Message):
    # command.args is intentionally unused. A client id or a code in /start
    # must not reveal records and must not consume a pairing code.
    log.info("command=start user_id=%s", _user_id(message))
    await message.answer(telegram_text.START_TEXT)


@router.message(Command("help"))
async def cmd_help(message: Message):
    log.info("command=help user_id=%s", _user_id(message))
    await message.answer(telegram_text.HELP_TEXT)


@router.message(Command("link"))
async def cmd_link(message: Message, command: CommandObject):
    user_id = _user_id(message)
    log.info("command=link user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    raw_args = command.args or ""
    if not raw_args.strip():
        await message.answer("Укажите код из офиса: /link XXXX-XXXX")
        return
    code = telegram_service.find_pairing_code(raw_args)
    result = telegram_service.redeem_pairing_code(user_id, code if code is not None else raw_args)
    profile = telegram_service.get_own_profile(user_id) if result.ok else None
    await message.answer(telegram_text.render_link_result(result, profile))


@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    user_id = _user_id(message)
    log.info("command=whoami user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    await message.answer(telegram_text.render_whoami(user_id))


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject):
    user_id = _user_id(message)
    log.info("command=find user_id=%s staff=%s", user_id, telegram_staff.is_staff(user_id))
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    if not telegram_staff.is_staff(user_id):
        await message.answer(STAFF_DENIED)
        return
    await _answer_find(message, user_id, command.args or "")


@router.message(Command("unlink"))
async def cmd_unlink(message: Message):
    user_id = _user_id(message)
    log.info("command=unlink user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    result = telegram_service.unlink_telegram_user(user_id)
    if result.link_removed:
        await message.answer(
            "Привязка удалена. Чтобы снова смотреть заявки, нужен новый код из офиса."
        )
    else:
        await message.answer("Этот Telegram не привязан к клиенту.")


@router.message(Command("status", "applications"))
async def cmd_status(message: Message):
    user_id = _user_id(message)
    log.info("command=status user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    apps = telegram_service.list_own_applications(user_id)
    if apps is None:
        await message.answer(telegram_text.NOT_LINKED_TEXT)
        return
    await _send_parts(
        message,
        telegram_text.render_applications(apps),
        _keyboard(telegram_text.application_buttons(apps)),
    )


@router.message(Command("schedule", "payments"))
async def cmd_schedule(message: Message):
    user_id = _user_id(message)
    log.info("command=schedule user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    loan_ids = telegram_service.list_own_loan_ids(user_id)
    if loan_ids is None:
        await message.answer(telegram_text.NOT_LINKED_TEXT)
        return
    if not loan_ids:
        await message.answer("Одобренных кредитов с графиком нет.")
        return
    for loan_id in loan_ids:
        schedule = telegram_service.get_own_schedule(user_id, loan_id)
        if schedule is None:
            continue
        await _send_parts(message, telegram_text.render_schedule(schedule))


@router.message(Command("app"))
async def cmd_app(message: Message, command: CommandObject):
    user_id = _user_id(message)
    log.info("command=app user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    if telegram_service.get_own_profile(user_id) is None:
        await message.answer(telegram_text.NOT_LINKED_TEXT)
        return
    application_id = telegram_text.parse_positive_int(command.args or "")
    if application_id is None:
        await message.answer("Укажите номер своей заявки: /app 12")
        return
    app = telegram_service.get_own_application(user_id, application_id)
    if app is None:
        await message.answer("Заявка не найдена или вам не принадлежит.")
        return
    await _send_parts(message, telegram_text.render_application_detail(app))


@router.message(Command("loan"))
async def cmd_loan(message: Message, command: CommandObject):
    user_id = _user_id(message)
    log.info("command=loan user_id=%s", user_id)
    if user_id is None:
        await message.answer("Не удалось определить пользователя.")
        return
    if telegram_service.get_own_profile(user_id) is None:
        await message.answer(telegram_text.NOT_LINKED_TEXT)
        return
    loan_id = telegram_text.parse_positive_int(command.args or "")
    if loan_id is None:
        await message.answer("Укажите номер своего кредита: /loan 3")
        return
    schedule = telegram_service.get_own_schedule(user_id, loan_id)
    if schedule is None:
        await message.answer("График не найден или вам не принадлежит.")
        return
    await _send_parts(message, telegram_text.render_schedule(schedule))


@router.callback_query()
async def on_callback(callback: CallbackQuery):
    message = callback.message
    chat_type = message.chat.type if message is not None and message.chat is not None else None
    if chat_type != "private":
        log.info("ignored non-private callback chat_type=%s", chat_type)
        await callback.answer("Только личный чат", show_alert=True)
        return
    user_id = _user_id(callback)
    if user_id is None:
        await callback.answer("Недоступно", show_alert=True)
        return
    staff_parsed = telegram_staff.parse_staff_callback(callback.data or "")
    if staff_parsed is not None:
        log.info("staff callback user_id=%s kind=%s allowed=%s", user_id, staff_parsed[0], telegram_staff.is_staff(user_id))
        if not telegram_staff.is_staff(user_id):
            await callback.answer("Недоступно", show_alert=True)
            return
        await _staff_callback(callback, message, user_id, staff_parsed)
        return
    parsed = telegram_text.parse_callback(callback.data or "")
    log.info("callback user_id=%s kind=%s", user_id, None if parsed is None else parsed[0])
    if parsed is None:
        await callback.answer("Недоступно", show_alert=True)
        return
    kind, entity_id = parsed
    if kind == "app":
        app = telegram_service.get_own_application(user_id, entity_id)
        if app is None:
            await callback.answer("Заявка недоступна", show_alert=True)
            return
        await callback.answer()
        await _send_parts(message, telegram_text.render_application_detail(app))
        return
    schedule = telegram_service.get_own_schedule(user_id, entity_id)
    if schedule is None:
        await callback.answer("График недоступен", show_alert=True)
        return
    await callback.answer()
    await _send_parts(message, telegram_text.render_schedule(schedule))


@router.message()
async def on_unknown(message: Message):
    # Do not echo the text: it may be a pairing code or personal data.
    user_id = _user_id(message)
    log.info("command=unknown user_id=%s", user_id)
    if not _private_message(message):
        return
    text = message.text or ""
    if (
        user_id is not None
        and telegram_staff.is_staff(user_id)
        and text.strip()
        and not text.startswith("/")
    ):
        if telegram_service.normalize_pairing_code(text):
            await message.answer("Похоже на код привязки. Отправьте его командой /link.")
            return
        log.info("command=find-plain user_id=%s", user_id)
        await _answer_find(message, user_id, text)
        return
    await message.answer(telegram_text.UNKNOWN_TEXT)


async def _answer_find(message, user_id, raw):
    name, page = telegram_staff.split_find_args(raw)
    if not name:
        await message.answer("Укажите имя: /find Нурбек")
        return
    await _send_search(message, user_id, name, page)


async def _send_search(message, user_id, name, page):
    found = telegram_service.search_clients_by_name(name, page=page)
    if found is None:
        await message.answer(
            "Нужно имя от 2 букв. ИИН, телефон и почта для поиска не используются."
        )
        return
    token = None
    if found["total"] > found["page_size"]:
        token = telegram_staff.remember_search(user_id, found["query"])
    text, buttons = telegram_text.render_client_search(found, token)
    await _send_parts(message, text, _keyboard(buttons))


async def _staff_callback(callback, message, user_id, parsed):
    if not telegram_staff.is_staff(user_id):
        await callback.answer("Недоступно", show_alert=True)
        return
    kind = parsed[0]
    if kind == "fp":
        _, token, page = parsed
        query = telegram_staff.recall_search(user_id, token)
        if query is None:
            await callback.answer("Повторите /find", show_alert=True)
            return
        await callback.answer()
        await _send_search(message, user_id, query, page)
        return
    if kind == "fst":
        client_id = parsed[1]
        client = telegram_service.client_name(client_id)
        apps = telegram_service.list_client_applications(client_id)
        if client is None or apps is None:
            await callback.answer("Недоступно", show_alert=True)
            return
        await callback.answer()
        await _send_parts(
            message,
            telegram_text.render_staff_client(client, apps),
            _keyboard(telegram_text.staff_application_buttons(client_id, apps)),
        )
        return
    if kind == "fsa":
        _, client_id, application_id = parsed
        app = telegram_service.get_client_application(client_id, application_id)
        if app is None:
            await callback.answer("Недоступно", show_alert=True)
            return
        await callback.answer()
        client = telegram_service.client_name(client_id)
        buttons = []
        if app.get("loan_id"):
            buttons.append((
                f"График {int(app['loan_id'])}",
                f"fss:{int(client_id)}:{int(app['loan_id'])}",
            ))
        prefix = ""
        if client is not None:
            prefix = f"Клиент №{int(client['client_id'])}: {telegram_text.esc(client['full_name'])}\n"
        await _send_parts(
            message,
            prefix + telegram_text.render_application_detail(app, staff=True),
            _keyboard(buttons),
        )
        return
    _, client_id, loan_id = parsed
    schedule = telegram_service.get_client_schedule(client_id, loan_id)
    if schedule is None:
        await callback.answer("Недоступно", show_alert=True)
        return
    await callback.answer()
    await _send_parts(message, telegram_text.render_schedule(schedule))


def read_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    if token in PLACEHOLDER_TOKENS or ":" not in token:
        return None
    return token


async def run_polling(token):
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    database.init_db()
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dispatcher = build_dispatcher()
    commands = [
        BotCommand(command="start", description="Начало работы"),
        BotCommand(command="help", description="Справка"),
        BotCommand(command="link", description="Привязать код из офиса"),
        BotCommand(command="status", description="Мои заявки"),
        BotCommand(command="schedule", description="График платежей"),
        BotCommand(command="unlink", description="Отвязать аккаунт"),
        BotCommand(command="whoami", description="Мой числовой Telegram id"),
        BotCommand(command="find", description="Найти клиента (сотрудник)"),
    ]
    try:
        await bot.set_my_commands(commands)
    except Exception as exc:
        log.warning("menu setup failed error_type=%s", type(exc).__name__)
    await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    telegram_env.bind_database_path()
    token = read_token()
    if token is None:
        print(
            "Задайте TELEGRAM_BOT_TOKEN в окружении или в файле .env "
            "(шаблон: .env.example). Токен берётся у @BotFather и в чат не отправляется.",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(run_polling(token))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
