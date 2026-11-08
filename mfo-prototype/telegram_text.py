# telegram_text.py
# Russian texts for the client bot. Every value that comes from the database
# is escaped before it is placed into an HTML message.
from html import escape

STATUS_RU = {
    "Approved": "одобрена",
    "Rejected": "отклонена",
    "Pending Admin Review": "на рассмотрении",
    "Paid": "оплачен",
    "Pending": "ожидается",
}
APPROVAL_RU = {
    "Auto": "автоматически",
    "Manual": "сотрудником",
}
CHECK_RU = {
    "PASS": "пройдена",
    "FAIL": "не пройдена",
    "MANUAL_REVIEW": "нужна проверка сотрудника",
}

START_TEXT = (
    "Здравствуйте. Это бот микрофинансовой организации: здесь видны только "
    "ваши заявки и график платежей.\n\n"
    "Привязка делается одноразовым кодом из офиса:\n"
    "/link XXXX-XXXX\n\n"
    "Аргумент команды /start игнорируется. Номер клиента или номер заявки "
    "в /start ничего не открывает.\n\n"
    "Справка: /help\n"
    "Свой числовой Telegram id, без данных клиентов: /whoami"
)

HELP_TEXT = (
    "<b>Команды</b>\n"
    "/link КОД — привязать этот Telegram к своему клиенту\n"
    "/status — мои заявки и статусы\n"
    "/app НОМЕР — одна моя заявка\n"
    "/schedule — графики платежей по моим кредитам\n"
    "/loan НОМЕР — график одного моего кредита\n"
    "/unlink — отвязать Telegram\n"
    "/whoami — только ваш числовой Telegram id\n"
    "/find ИМЯ — для сотрудника из списка: заявки клиента (только чтение)\n"
    "/help — эта справка\n\n"
    "Бот не принимает оплату и не показывает ИИН, паспорт, телефон, почту и адрес.\n"
    "Код выдаёт сотрудник локальной командой на компьютере с базой, не в этом чате.\n"
    "Если код истёк или уже использован, попросите новый в офисе."
)

GROUP_TEXT = (
    "Бот отвечает только в личном чате. Откройте диалог с ботом и отправьте /help. "
    "В группе коды не принимаются и чужие данные не показываются."
)

NOT_LINKED_TEXT = (
    "Аккаунт не привязан. Получите одноразовый код в офисе и отправьте /link КОД."
)

UNKNOWN_TEXT = (
    "Не понял сообщение. Отправьте /help. "
    "Код привязки принимается только командой /link."
)


def esc(value):
    if value is None:
        return ""
    return escape(str(value), quote=True)


def status_ru(value):
    if value in STATUS_RU:
        return STATUS_RU[value]
    return "" if value is None else str(value)


def format_kzt(amount):
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return esc(amount)
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    return sign + " ".join(reversed(groups)) + "," + frac + " ₸"


def format_when(value):
    from datetime import datetime

    if not isinstance(value, str):
        return "—"
    text = value.strip()
    try:
        if len(text) >= 19 and text[4] == "-" and text[10] == " ":
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            return parsed.strftime("%d.%m.%Y %H:%M")
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            return parsed.strftime("%d.%m.%Y")
    except ValueError:
        pass
    return esc(text)


def format_rate(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if number.is_integer():
        shown = str(int(number))
    else:
        shown = f"{number:.2f}".rstrip("0").rstrip(".")
    return shown + "% годовых"


def _short(value, limit):
    text = "" if value is None else " ".join(str(value).split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def render_link_result(result, profile):
    if result.ok:
        name = esc(_short(profile["full_name"], 120)) if profile else "клиент"
        return (
            f"Готово. Этот Telegram привязан к клиенту <b>{name}</b>.\n"
            "Заявки: /status. График: /schedule.\n"
            "ИИН, паспорт и контакты в чат не отправляются."
        )
    messages = {
        "malformed": "Код не распознан. Нужен код из офиса: /link XXXX-XXXX",
        "invalid": "Код недействителен.",
        "expired": "Срок действия кода истёк. Попросите новый код в офисе.",
        "replayed": "Код уже использован или отозван.",
        "locked": "Слишком много неудачных попыток. Подождите 15 минут и запросите код заново, если он ещё не использован.",
        "user_already_linked": "Этот Telegram уже привязан. Сначала отправьте /unlink, если нужно сменить клиента.",
        "client_already_linked": "Клиент из этого кода уже привязан к другому Telegram.",
        "race": "Не удалось привязать код. Запросите новый код в офисе.",
    }
    return messages.get(result.reason, "Привязка отклонена.")


def render_whoami(user_id):
    return (
        f"Ваш Telegram id: {int(user_id)}\n"
        "Здесь только этот номер. Имена клиентов, заявки и контакты не показываются.\n"
        "Для команды /find добавьте номер в MFO_TELEGRAM_STAFF_IDS и перезапустите бота."
    )


def render_applications(apps, title="Ваши заявки"):
    if not apps:
        return "Заявок пока нет. Новую заявку можно подать в личном кабинете на сайте."
    lines = [f"<b>{esc(title)}</b>"]
    for app in apps[:10]:
        lines.append(
            f"№{int(app['application_id'])}: {esc(status_ru(app['status']))}, "
            f"{format_kzt(app['requested_amount'])}, {int(app['term_months'])} мес."
        )
    if len(apps) > 10:
        lines.append(
            f"Показаны 10 из {len(apps)}. Остальные открываются командой /app НОМЕР."
        )
    lines.append("Кнопка ниже открывает заявку или график.")
    return "\n".join(lines)


def application_buttons(apps):
    """(label, callback_data) only for rows already loaded for this user."""
    buttons = []
    for app in apps[:10]:
        application_id = int(app["application_id"])
        buttons.append((f"Заявка {application_id}", f"app:{application_id}"))
        if app.get("loan_id"):
            loan_id = int(app["loan_id"])
            buttons.append((f"График {loan_id}", f"sch:{loan_id}"))
    return buttons


def render_application_detail(app, staff=False):
    lines = [
        f"<b>Заявка №{int(app['application_id'])}</b>",
        f"Статус: {esc(status_ru(app['status']))}",
        f"Как решена: {esc(APPROVAL_RU.get(app['approval_type'], str(app['approval_type'] or '—')))}",
        f"Сумма: {format_kzt(app['requested_amount'])}",
        f"Срок: {int(app['term_months'])} мес.",
        f"Цель: {esc(_short(app.get('purpose'), 300) or '—')}",
        f"Подана: {format_when(app.get('created_at'))}",
    ]
    checks = app.get("checks") or []
    if checks:
        lines.append("Проверки:")
        for item in checks:
            result = CHECK_RU.get(item["result_status"], str(item["result_status"]))
            lines.append(f"• {esc(item['check_type'])}: {esc(result)}")
    if app.get("loan_id"):
        if staff:
            lines.append(f"Кредит №{int(app['loan_id'])}. График открывается кнопкой под сообщением.")
        else:
            lines.append(f"Кредит №{int(app['loan_id'])}. График: /loan {int(app['loan_id'])}")
    else:
        lines.append("Графика платежей нет: кредит не выдан.")
    return "\n".join(lines)


def render_client_search(page, token=None):
    total = page["total"]
    if total == 0:
        return "Никого не нашли. Уточните имя. ИИН и телефон для поиска не используются.", []
    start = (page["page"] - 1) * page["page_size"] + 1
    end = start + len(page["items"]) - 1
    lines = [
        f"<b>Клиенты</b>: {total}. Показаны {start}–{end}.",
        "Одинаковые имена различаются номером клиента.",
    ]
    buttons = []
    for item in page["items"]:
        client_id = int(item["client_id"])
        label = _short(item["full_name"], 40) or "без имени"
        lines.append(f"№{client_id}: {esc(label)}")
        buttons.append((f"{label} · №{client_id}", f"fst:{client_id}"))
    if token and end < total and page["page"] < 20:
        buttons.append(("Дальше", f"fp:{token}:{page['page'] + 1}"))
    lines.append("ИИН, паспорт, телефон и почта не показываются.")
    return "\n".join(lines), buttons


def render_staff_client(client, apps):
    header = (
        f"<b>Клиент №{int(client['client_id'])}</b>: "
        f"{esc(_short(client['full_name'], 120) or 'без имени')}"
    )
    return header + "\n" + render_applications(apps, title="Заявки клиента")


def staff_application_buttons(client_id, apps):
    buttons = []
    client_id = int(client_id)
    for app in apps[:10]:
        application_id = int(app["application_id"])
        buttons.append((f"Заявка {application_id}", f"fsa:{client_id}:{application_id}"))
        if app.get("loan_id"):
            loan_id = int(app["loan_id"])
            buttons.append((f"График {loan_id}", f"fss:{client_id}:{loan_id}"))
    return buttons


def render_schedule(schedule):
    payments = schedule["payments"]
    paid = sum(1 for item in payments if item["status"] == "Paid")
    lines = [
        f"<b>График кредита №{int(schedule['loan_id'])}</b>",
        f"Заявка №{int(schedule['application_id'])}",
        f"Сумма: {format_kzt(schedule['approved_amount'])}",
        f"Ставка: {esc(format_rate(schedule['interest_rate']))}",
        f"Выдан: {format_when(schedule['issue_date'])}",
        f"Оплачено: {paid} из {len(payments)}",
        "",
    ]
    for index, payment in enumerate(payments, start=1):
        lines.append(
            f"{index}. {format_when(payment['due_date'])} — "
            f"{format_kzt(payment['amount_due'])} — {esc(status_ru(payment['status']))}"
        )
    lines.append("")
    lines.append("Бот не принимает оплату и не показывает данные карты.")
    return "\n".join(lines)


def split_text(text, limit=3500):
    if len(text) <= limit:
        return [text]
    chunks = []
    current = []
    size = 0
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                size = 0
            for start in range(0, len(line), limit):
                chunks.append(line[start:start + limit])
            continue
        extra = len(line) + (1 if current else 0)
        if current and size + extra > limit:
            chunks.append("\n".join(current))
            current = [line]
            size = len(line)
        else:
            current.append(line)
            size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks


def parse_callback(data):
    if not isinstance(data, str) or len(data) > 64 or data.count(":") != 1:
        return None
    kind, raw = data.split(":", 1)
    if kind not in {"app", "sch"} or not raw.isdigit() or len(raw) > 18:
        return None
    value = int(raw)
    if value <= 0:
        return None
    return kind, value


def parse_positive_int(raw):
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.isdigit() or len(text) > 18:
        return None
    value = int(text)
    if value <= 0:
        return None
    return value
