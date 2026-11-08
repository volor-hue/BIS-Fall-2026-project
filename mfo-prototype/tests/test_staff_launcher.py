# Staff search, /whoami, and the one-command launcher. No live Telegram calls.
import asyncio
import itertools
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import database
import run
import telegram_bot
import telegram_service
import telegram_staff
import telegram_text
from approval import INTEREST_RATE, build_schedule, decide_application, run_checks
from test_telegram import MockSession

_SEQ = itertools.count(1)
TOKEN = "100000:test-token-not-a-real-secret"
STAFF_ID = 900001
OTHER_STAFF = 900002
CLIENT_USER = 515151


def use_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "staff.db"))
    database.init_db()
    telegram_staff.clear_search_memory()


def create_person(**overrides):
    fields = {
        "iin": "050101500123",
        "full_name": "Test Client",
        "birth_date": "2005-01-01",
        "phone": "+77011234567",
        "address": "Almaty",
        "email": "client@test.kz",
        "password": "pass123",
        "marital_status": "Single",
        "created_at": "2026-11-08 09:00:00",
    }
    fields.update(overrides)
    return database.create_client(
        fields["iin"], fields["full_name"], fields["birth_date"], fields["phone"],
        fields["address"], fields["email"], fields["password"], fields["marital_status"],
        fields["created_at"],
    )


def add_application(client_id, amount, term, purpose, income=300000, iin="050101500123",
                    birth="2005-01-01"):
    database.create_employment(client_id, "Kaspi", "Analyst", income)
    status, approval_type = decide_application(income, amount, term)
    app_id = database.create_application(
        client_id, amount, term, purpose, status, approval_type, "2026-11-08 10:00:00",
    )
    database.create_checks(
        app_id,
        run_checks(iin, birth, income, amount, term, today=datetime(2026, 11, 8).date()),
        "2026-11-08 10:00:00",
    )
    loan_id = None
    if status == "Approved":
        schedule = build_schedule(amount, INTEREST_RATE, term, datetime(2026, 11, 8).date())
        loan_id = database.create_loan_with_schedule(
            app_id, amount, INTEREST_RATE, "2026-11-08", schedule,
        )
    return {"application_id": app_id, "loan_id": loan_id, "status": status}


def outgoing_text(session):
    return "\n".join(call.text for call in session.calls if isinstance(call, SendMessage))


def alerts(session):
    return [call.text for call in session.calls if isinstance(call, AnswerCallbackQuery)]


def button_data(session):
    found = []
    for call in session.calls:
        markup = getattr(call, "reply_markup", None)
        if markup is None or not hasattr(markup, "inline_keyboard"):
            continue
        for row in markup.inline_keyboard:
            for button in row:
                found.append(button.callback_data)
    return found


async def feed(text=None, *, user_id=STAFF_ID, chat_type="private", callback_data=None,
               first_name="Нурбек", username="nurbek_user"):
    session = MockSession()
    bot = Bot(
        token=TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    user = User(id=user_id, is_bot=False, first_name=first_name, username=username)
    chat_id = user_id if chat_type == "private" else -100123
    chat = Chat(id=chat_id, type=chat_type, title=None if chat_type == "private" else "Group")
    if callback_data is None:
        message = Message(
            message_id=next(_SEQ), date=datetime.now(timezone.utc),
            chat=chat, from_user=user, text=text,
        )
        update = Update(update_id=next(_SEQ), message=message)
    else:
        origin = Message(
            message_id=next(_SEQ), date=datetime.now(timezone.utc),
            chat=chat, from_user=user, text="кнопка",
        )
        query = CallbackQuery(
            id=str(next(_SEQ)), from_user=user, chat_instance="chat-instance",
            data=callback_data, message=origin,
        )
        update = Update(update_id=next(_SEQ), callback_query=query)
    try:
        await telegram_bot.build_dispatcher().feed_update(bot, update)
    finally:
        await bot.session.close()
    return session


def run_feed(*args, **kwargs):
    return asyncio.run(feed(*args, **kwargs))


def allow(monkeypatch, *ids):
    if ids:
        monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", ",".join(str(item) for item in ids))
    else:
        monkeypatch.delenv("MFO_TELEGRAM_STAFF_IDS", raising=False)


def test_allowlist_fails_closed_and_ignores_names():
    assert telegram_staff.parse_staff_ids("") == frozenset()
    assert telegram_staff.parse_staff_ids(None) == frozenset()
    assert telegram_staff.parse_staff_ids("  ") == frozenset()
    assert telegram_staff.parse_staff_ids("100000001, 100000002") == frozenset({100000001, 100000002})
    assert telegram_staff.parse_staff_ids("100000001;100000002") == frozenset({100000001, 100000002})
    for bad in ("Нурбек", "@clerk", "100000001,Нурбек", "0123", "0", "-5", "1.5", "id:100000001"):
        assert telegram_staff.parse_staff_ids(bad) is None
    assert telegram_staff.is_staff(True) is False
    assert telegram_staff.is_staff("100000001") is False


def test_whoami_returns_only_the_requesting_id(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    allow(monkeypatch)
    create_person(
        full_name="Секрет Клиент", iin="850101300999", email="secret-mail@test.kz",
        phone="+77009998877", address="SECRET ADDRESS 9",
    )
    session = run_feed("/whoami", user_id=424242424, first_name="Секрет Клиент", username="secret_user")
    text = outgoing_text(session)
    assert text.startswith("Ваш Telegram id: 424242424")
    for secret in ("850101300999", "+77009998877", "secret-mail@test.kz", "SECRET ADDRESS 9",
                   "Секрет Клиент", "secret_user", "100000001"):
        assert secret not in text
    group = run_feed("/whoami", user_id=424242424, chat_type="group")
    assert "424242424" not in outgoing_text(group)
    assert telegram_text.GROUP_TEXT in outgoing_text(group)


def test_unauthorized_find_plain_name_and_callback_are_denied(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    allow(monkeypatch)
    victim = create_person(
        full_name="Нурбек", iin="850101300999", email="secret-mail@test.kz",
        phone="+77009998877", address="SECRET ADDRESS 9",
    )
    victim_app = add_application(
        victim, 777001, 12, "VICTIM_PURPOSE_77", income=5_000_000, iin="850101300999",
    )
    for text in ("/find Нурбек", "Нурбек", "/find нурбек"):
        session = run_feed(text, user_id=CLIENT_USER, first_name="Нурбек", username="nurbek_user")
        body = outgoing_text(session)
        assert "VICTIM_PURPOSE_77" not in body
        assert "850101300999" not in body
        assert "777 001,00 ₸" not in body
        assert "+77009998877" not in body
        assert "fst:" not in " ".join(button_data(session))
    denied = run_feed(
        callback_data=f"fst:{victim}", user_id=CLIENT_USER, first_name="Нурбек", username="nurbek_user",
    )
    assert alerts(denied) == ["Недоступно"]
    assert outgoing_text(denied) == ""
    tampered = run_feed(
        callback_data=f"fsa:{victim}:{victim_app['application_id']}", user_id=CLIENT_USER,
    )
    assert alerts(tampered) == ["Недоступно"]
    assert "VICTIM_PURPOSE_77" not in outgoing_text(tampered)
    group = run_feed("/find Нурбек", user_id=STAFF_ID, chat_type="supergroup")
    assert telegram_text.GROUP_TEXT in outgoing_text(group)
    assert "VICTIM_PURPOSE_77" not in outgoing_text(group)
    group_button = run_feed(callback_data=f"fst:{victim}", user_id=STAFF_ID, chat_type="group")
    assert alerts(group_button) == ["Только личный чат"]
    assert outgoing_text(group_button) == ""


def test_invalid_and_empty_allowlist_deny_even_a_listed_number(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    create_person(full_name="Нурбек", iin="111111111111", email="n@test.kz")
    monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", f"{STAFF_ID},Нурбек")
    assert telegram_staff.is_staff(STAFF_ID) is False
    body = outgoing_text(run_feed("/find Нурбек", user_id=STAFF_ID))
    assert "Клиенты" not in body
    assert "111111111111" not in body
    allow(monkeypatch)
    assert telegram_staff.is_staff(STAFF_ID) is False
    assert "MFO_TELEGRAM_STAFF_IDS" in outgoing_text(run_feed("/find Нурбек", user_id=STAFF_ID))


def test_cyrillic_partial_duplicates_wildcards_and_pagination(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    allow(monkeypatch, STAFF_ID, OTHER_STAFF)
    create_person(full_name="Айгуль", iin="222222222222", email="a@test.kz", phone="+77001000001")
    yo = create_person(full_name="Ёлка", iin="333333333333", email="yo@test.kz", phone="+77001000002")
    first = create_person(full_name="Нурбек", iin="444444444444", email="d1@test.kz")
    second = create_person(full_name="Нурбек", iin="555555555555", email="d2@test.kz")
    page_ids = []
    for index, letter in enumerate("АБВГДЕ", start=1):
        page_ids.append(create_person(
            full_name=f"Нурбек {letter}",
            iin=f"66666666666{index}",
            email=f"p{index}@test.kz",
            phone=f"+7700200000{index}",
        ))
    found = run_feed("/find нУрБеК", user_id=STAFF_ID, first_name="Other")
    text = outgoing_text(found)
    assert f"№{first}" in text and f"№{second}" in text
    assert "Айгуль" not in text
    assert "444444444444" not in text and "555555555555" not in text
    assert f"fst:{first}" in button_data(found) and f"fst:{second}" in button_data(found)
    plain = outgoing_text(run_feed("нурбек", user_id=STAFF_ID))
    assert f"№{first}" in plain and "Айгуль" not in plain
    yo_text = outgoing_text(run_feed("/find ёлка", user_id=STAFF_ID))
    assert f"№{yo}" in yo_text
    assert "333333333333" not in yo_text

    wildcard = outgoing_text(run_feed("/find %%", user_id=STAFF_ID))
    assert "Айгуль" not in wildcard and "Никого не нашли" in wildcard
    injected = outgoing_text(run_feed("/find Нур%' OR 1=1 --", user_id=STAFF_ID))
    assert "Айгуль" not in injected and "Ёлка" not in injected
    assert "Никого не нашли" in injected
    iin_try = outgoing_text(run_feed("/find 444444444444", user_id=STAFF_ID))
    assert "444444444444" not in iin_try
    assert "не используются" in iin_try

    page = run_feed("/find Нурбек", user_id=STAFF_ID)
    buttons = button_data(page)
    next_buttons = [item for item in buttons if item.startswith("fp:")]
    assert len([item for item in buttons if item.startswith("fst:")]) == 5
    assert len(next_buttons) == 1
    assert f"fst:{page_ids[-1]}" not in buttons
    token = next_buttons[0]
    stolen = run_feed(callback_data=token, user_id=OTHER_STAFF)
    assert alerts(stolen) == ["Повторите /find"]
    assert f"№{page_ids[-1]}" not in outgoing_text(stolen)
    second_page = run_feed(callback_data=token, user_id=STAFF_ID)
    assert f"№{page_ids[-1]}" in outgoing_text(second_page)
    assert f"fst:{page_ids[0]}" not in button_data(second_page)


def test_staff_can_read_selected_client_and_client_ownership_stays(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    allow(monkeypatch, STAFF_ID)
    owner = create_person(
        full_name="Свой Клиент", iin="101010101010", email="own@test.kz", phone="+77003000001",
    )
    victim = create_person(
        full_name="Нурбек Лашбаев", iin="850101300999", email="secret-mail@test.kz",
        phone="+77009998877", address="SECRET ADDRESS 9",
    )
    own = add_application(owner, 400000, 12, "OWN_PURPOSE", iin="101010101010")
    victim_app = add_application(
        victim, 400000, 12, "VICTIM_PURPOSE_77", income=300000, iin="850101300999",
    )
    issued = telegram_service.issue_pairing_code(owner)
    assert telegram_service.redeem_pairing_code(CLIENT_USER, issued.code).ok
    own_view = outgoing_text(run_feed("/status", user_id=CLIENT_USER))
    assert "OWN_PURPOSE" not in own_view or "одобрена" in own_view
    assert "VICTIM_PURPOSE_77" not in own_view
    assert "850101300999" not in own_view
    hidden = outgoing_text(run_feed(f"/app {victim_app['application_id']}", user_id=CLIENT_USER))
    assert "не принадлежит" in hidden
    assert "VICTIM_PURPOSE_77" not in hidden
    assert telegram_text.NOT_LINKED_TEXT in outgoing_text(run_feed("/status", user_id=STAFF_ID))

    found = run_feed("/find Лашбаев", user_id=STAFF_ID)
    assert f"fst:{victim}" in button_data(found)
    assert "850101300999" not in outgoing_text(found)
    opened = run_feed(callback_data=f"fst:{victim}", user_id=STAFF_ID)
    opened_text = outgoing_text(opened)
    assert "Нурбек Лашбаев" in opened_text
    assert "одобрена" in opened_text
    assert "850101300999" not in opened_text
    assert "+77009998877" not in opened_text
    assert "secret-mail@test.kz" not in opened_text
    assert f"fsa:{victim}:{victim_app['application_id']}" in button_data(opened)
    detail = run_feed(
        callback_data=f"fsa:{victim}:{victim_app['application_id']}", user_id=STAFF_ID,
    )
    assert "VICTIM_PURPOSE_77" in outgoing_text(detail)
    assert "850101300999" not in outgoing_text(detail)
    before = database.get_repayments(victim_app["loan_id"])
    schedule = run_feed(
        callback_data=f"fss:{victim}:{victim_app['loan_id']}", user_id=STAFF_ID,
    )
    assert "37 823,84 ₸" in outgoing_text(schedule)
    assert "не принимает оплату" in outgoing_text(schedule)
    assert database.get_repayments(victim_app["loan_id"]) == before
    mismatched = run_feed(
        callback_data=f"fss:{owner}:{victim_app['loan_id']}", user_id=STAFF_ID,
    )
    assert alerts(mismatched) == ["Недоступно"]
    assert "37 823,84 ₸" not in outgoing_text(mismatched)
    assert "VICTIM_PURPOSE_77" not in outgoing_text(mismatched)
    sql_callback = run_feed(callback_data="fst:1 OR 1=1", user_id=STAFF_ID)
    assert alerts(sql_callback) == ["Недоступно"]
    loan_reply = outgoing_text(run_feed(f"/loan {victim_app['loan_id']}", user_id=STAFF_ID))
    assert "не привязан" in loan_reply
    assert "VICTIM_PURPOSE_77" not in loan_reply
    assert "37 823,84 ₸" not in loan_reply


def test_launcher_path_port_token_and_cleanup(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (other / ".env").write_text("MFO_DB=/tmp/from-cwd-should-be-ignored.db\n", encoding="utf-8")
    (project / ".env").write_text(
        "MFO_DB=data/mfo.db\nTELEGRAM_BOT_TOKEN=PUT_YOUR_TOKEN_HERE\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MFO_DB", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MFO_TELEGRAM_STAFF_IDS", raising=False)
    monkeypatch.delenv("MFO_PORT", raising=False)
    monkeypatch.chdir(other)
    plan, error = run.build_plan(["--bot-only"], str(project))
    assert plan is None
    assert "TELEGRAM_BOT_TOKEN" in error
    assert os.environ["MFO_DB"] == os.path.abspath(project / "data" / "mfo.db")
    assert "/tmp/from-cwd-should-be-ignored.db" not in os.environ["MFO_DB"]

    monkeypatch.setenv("MFO_DB", str(tmp_path / "from-process.db"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", "")
    plan, error = run.build_plan(["--bot-only"], str(project))
    assert error is None
    assert plan.db_path == os.path.abspath(tmp_path / "from-process.db")
    assert plan.staff_count == 0
    assert plan.start_bot and not plan.start_web
    banner = run.format_banner(plan)
    assert TOKEN not in banner
    commands = run.child_commands(plan)
    assert commands[0][0] == sys.executable
    assert all(TOKEN not in part for command in commands for part in command)
    assert run.child_env(plan)["MFO_DB"] == plan.db_path

    monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", f"{STAFF_ID},@clerk")
    plan, error = run.build_plan(["--bot-only"], str(project))
    assert plan is None
    assert "@clerk" not in error
    assert TOKEN not in error

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", "")
    original_popen = run.subprocess.Popen
    monkeypatch.setattr(
        run.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")),
    )
    try:
        code = run.main(["--port", str(port)])
    finally:
        busy.close()
    monkeypatch.setattr(run.subprocess, "Popen", original_popen)
    captured = capsys.readouterr()
    assert code == 3
    assert "уже занят" in captured.err
    assert "8001" in captured.err
    assert TOKEN not in captured.err
    assert TOKEN not in captured.out

    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "LEAKED-TOKEN-VALUE")
    assert run.main(["--bot-only"]) == 2
    leaked = capsys.readouterr()
    assert "LEAKED-TOKEN-VALUE" not in leaked.err
    assert "LEAKED-TOKEN-VALUE" not in leaked.out

    real_import = run.importlib.import_module

    def broken(name, *args, **kwargs):
        if name == "fastapi":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(run.importlib, "import_module", broken)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("MFO_TELEGRAM_STAFF_IDS", "")
    plan, error = run.build_plan(["--web-only", "--port", "8011"], str(project))
    assert plan is None
    assert "fastapi" in error
    plan, error = run.build_plan(["--bot-only", "--web-only"], str(project))
    assert plan is None and "оба" in error
    monkeypatch.setenv("MFO_PORT", "nope")
    plan, error = run.build_plan(["--web-only"], str(project))
    assert plan is None and "MFO_PORT" in error

    run.reset_shutdown()
    pid_a = tmp_path / "a.pid"
    pid_b = tmp_path / "b.pid"
    sleeper = (
        "import os,sys,time,pathlib; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    )
    code = run.supervise(
        [
            [sys.executable, "-c", sleeper, str(pid_a)],
            [sys.executable, "-c", "import time,sys; time.sleep(0.6); sys.exit(3)"],
        ],
        os.environ.copy(),
        str(tmp_path),
    )
    assert code == 3
    assert pid_a.exists()
    dead = int(pid_a.read_text())
    try:
        os.kill(dead, 0)
        raise AssertionError("sleeper still running")
    except ProcessLookupError:
        pass

    run.reset_shutdown()
    def stop_later():
        time.sleep(0.8)
        run.request_shutdown()
    threading.Thread(target=stop_later, daemon=True).start()
    code = run.supervise(
        [
            [sys.executable, "-c", sleeper, str(pid_b)],
            [sys.executable, "-c", sleeper, str(tmp_path / "c.pid")],
        ],
        os.environ.copy(),
        str(tmp_path),
    )
    assert code == 0
    for path in (pid_b, tmp_path / "c.pid"):
        assert path.exists()
        try:
            os.kill(int(path.read_text()), 0)
            raise AssertionError("process left running")
        except ProcessLookupError:
            pass


def test_web_api_is_still_the_original_18(client):
    spec = client.get("/openapi.json").json()
    operations = [
        (method, path)
        for path, methods in spec["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "delete", "patch"} and path.startswith("/api")
    ]
    assert len(operations) == 18
    assert "telegram" not in str(spec).lower()
    assert client.get("/docs").status_code == 200
