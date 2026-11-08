# Tests for the Telegram client bot. The Bot API is mocked; no network and no token.
import asyncio
import itertools
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

import database
import telegram_bot
import telegram_env
import telegram_operator
import telegram_service
import telegram_text
from approval import INTEREST_RATE, build_schedule, decide_application, run_checks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALPHABET = telegram_service.ALPHABET
CODE_RE = re.compile(rf"[{ALPHABET}]{{4}}-[{ALPHABET}]{{4}}")
TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
BUSINESS_TABLES = (
    "clients", "employees", "employment", "documents",
    "loan_applications", "client_checks", "loans", "repayments",
)
_SEQ = itertools.count(1)
NOW = datetime(2026, 11, 8, 12, 0, 0)


class MockSession(BaseSession):
    """In-memory stand-in for Telegram. It records calls and never opens a socket."""

    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        return None

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        raise AssertionError("unexpected Telegram method " + type(method).__name__)

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True,
    ):
        if False:
            yield b""
        raise AssertionError("Telegram file download is not used")


def use_db(tmp_path, monkeypatch, name="bot.db"):
    path = tmp_path / name
    monkeypatch.setattr(database, "DB_FILE", str(path))
    database.init_db()
    return path


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


def database_text():
    conn = database.get_connection()
    try:
        chunks = []
        names = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        for (name,) in names:
            rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
            for row in rows:
                chunks.append(" | ".join("" if value is None else str(value) for value in row))
        return "\n".join(chunks)
    finally:
        conn.close()


def outgoing_text(session):
    return "\n".join(call.text for call in session.calls if isinstance(call, SendMessage))


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


async def feed(text=None, *, user_id=501, chat_type="private", callback_data=None):
    session = MockSession()
    bot = Bot(
        token="100000:test-token-not-a-real-secret",
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    user = User(id=user_id, is_bot=False, first_name="Ivan")
    chat_id = user_id if chat_type == "private" else -100123
    chat = Chat(id=chat_id, type=chat_type, title=None if chat_type == "private" else "Group")
    if callback_data is None:
        message = Message(
            message_id=next(_SEQ),
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text=text,
        )
        update = Update(update_id=next(_SEQ), message=message)
    else:
        origin = Message(
            message_id=next(_SEQ),
            date=datetime.now(timezone.utc),
            chat=chat,
            from_user=user,
            text="кнопка",
        )
        query = CallbackQuery(
            id=str(next(_SEQ)),
            from_user=user,
            chat_instance="chat-instance",
            data=callback_data,
            message=origin,
        )
        update = Update(update_id=next(_SEQ), callback_query=query)
    try:
        await telegram_bot.build_dispatcher().feed_update(bot, update)
    finally:
        await bot.session.close()
    return session


def run_feed(*args, **kwargs):
    return asyncio.run(feed(*args, **kwargs))


def test_migration_keeps_business_schema_and_rows(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person(full_name="Сохранённый клиент", email="keep@test.kz", iin="990101300456")
    conn = database.get_connection()
    before = {
        table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        for table in BUSINESS_TABLES
    }
    conn.execute("DROP TABLE telegram_link_attempts")
    conn.execute("DROP TABLE telegram_pairing_codes")
    conn.execute("DROP TABLE telegram_links")
    conn.commit()
    conn.close()

    database.init_db()
    database.init_db()

    conn = database.get_connection()
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"telegram_links", "telegram_pairing_codes", "telegram_link_attempts"} <= names
        for table in BUSINESS_TABLES:
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            assert columns == before[table]
        assert conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()
    assert database.get_client(client_id)["full_name"] == "Сохранённый клиент"


def test_telegram_tables_have_foreign_keys_and_cascade(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    application = add_application(client_id, 100000, 12, "cascade")
    issued = telegram_service.issue_pairing_code(client_id, now=NOW)
    assert telegram_service.redeem_pairing_code(4242, issued.code, now=NOW).ok

    conn = database.get_connection()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO telegram_links (client_id, telegram_user_id, linked_at) "
                "VALUES (99999, 77, '2026-11-08 12:00:00')"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            conn.execute(
                "INSERT INTO telegram_pairing_codes (client_id, code_hash, created_at, expires_at) "
                "VALUES (99999, 'abc', '2026-11-08 12:00:00', '2026-11-08 12:10:00')"
            )
        conn.rollback()
    finally:
        conn.close()

    assert database.delete_client(client_id) == 1
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM telegram_pairing_codes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM repayments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM loan_applications WHERE application_id = ?",
            (application["application_id"],),
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_code_is_random_single_use_and_not_stored(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    first = telegram_service.issue_pairing_code(client_id, now=NOW)
    second = telegram_service.issue_pairing_code(client_id, now=NOW + timedelta(seconds=1))
    assert CODE_RE.fullmatch(second.code)
    assert first.code != second.code
    assert all(ch in ALPHABET for ch in second.code if ch != "-")
    dumped = database_text()
    assert first.code not in dumped
    assert second.code not in dumped
    assert first.code.replace("-", "") not in dumped
    assert second.code.replace("-", "") not in dumped
    assert telegram_service.hash_pairing_code(second.code) in dumped
    assert telegram_service.redeem_pairing_code(7, first.code, now=NOW + timedelta(seconds=2)).reason == "replayed"
    linked = telegram_service.redeem_pairing_code(7, second.code.lower(), now=NOW + timedelta(seconds=2))
    assert linked.ok and linked.client_id == client_id
    replay = telegram_service.redeem_pairing_code(8, " " + second.code + " ", now=NOW + timedelta(seconds=3))
    assert replay.reason == "replayed"
    assert telegram_service.get_own_profile(8) is None


def test_expired_code_and_ttl_env(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    monkeypatch.setenv("MFO_TELEGRAM_CODE_TTL_SECONDS", "120")
    issued = telegram_service.issue_pairing_code(client_id, now=NOW)
    assert issued.expires_at == "2026-11-08 12:02:00"
    still_valid = telegram_service.redeem_pairing_code(
        11, issued.code, now=NOW + timedelta(seconds=119),
    )
    assert still_valid.ok
    telegram_service.unlink_client(client_id, now=NOW + timedelta(seconds=130))

    monkeypatch.setenv("MFO_TELEGRAM_CODE_TTL_SECONDS", "nope")
    fresh = telegram_service.issue_pairing_code(client_id, now=NOW)
    assert fresh.expires_at == "2026-11-08 12:10:00"
    expired = telegram_service.redeem_pairing_code(12, fresh.code, now=NOW + timedelta(seconds=600))
    assert expired.reason == "expired" and not expired.ok
    again = telegram_service.redeem_pairing_code(12, fresh.code, now=NOW + timedelta(seconds=601))
    assert again.reason == "expired"
    assert telegram_service.get_own_profile(12) is None


def test_brute_force_lock_does_not_burn_a_valid_code(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    issued = telegram_service.issue_pairing_code(client_id, now=NOW)
    for offset in range(4):
        denied = telegram_service.redeem_pairing_code(
            50, "ZZZZ-ZZZZ", now=NOW + timedelta(seconds=offset),
        )
        assert denied.reason == "invalid"
    allowed = telegram_service.redeem_pairing_code(50, issued.code, now=NOW + timedelta(seconds=4))
    assert allowed.ok
    telegram_service.unlink_client(client_id, now=NOW + timedelta(seconds=5))

    base = NOW + timedelta(minutes=30)
    issued = telegram_service.issue_pairing_code(client_id, now=base, ttl_seconds=3600)
    for offset in range(5):
        denied = telegram_service.redeem_pairing_code(
            51, "YYYY-YYYY", now=base + timedelta(seconds=offset),
        )
        assert denied.reason == "invalid"
    locked = telegram_service.redeem_pairing_code(51, issued.code, now=base + timedelta(seconds=10))
    assert locked.reason == "locked"
    assert telegram_service.get_own_profile(51) is None
    later = telegram_service.redeem_pairing_code(
        51, issued.code, now=base + timedelta(seconds=telegram_service.ATTEMPT_WINDOW_SECONDS + 20),
    )
    assert later.ok


def test_global_lock_and_one_to_one(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    monkeypatch.setattr(telegram_service, "GLOBAL_MAX_FAILED_ATTEMPTS", 3)
    first = create_person()
    second = create_person(iin="990101300456", email="other@test.kz", full_name="Второй")
    code_a = telegram_service.issue_pairing_code(first, now=NOW, ttl_seconds=7200)
    code_b = telegram_service.issue_pairing_code(second, now=NOW, ttl_seconds=7200)
    for user_id in (1, 2, 3):
        assert telegram_service.redeem_pairing_code(
            user_id, "00000000", now=NOW,
        ).reason == "malformed"
    blocked = telegram_service.redeem_pairing_code(4, code_b.code, now=NOW + timedelta(seconds=1))
    assert blocked.reason == "locked"
    # The global window is what blocked user 4. After it passes, user 4 can link.
    opened = telegram_service.redeem_pairing_code(
        4, code_b.code, now=NOW + timedelta(seconds=telegram_service.ATTEMPT_WINDOW_SECONDS + 5),
    )
    assert opened.ok and opened.client_id == second

    taken = telegram_service.redeem_pairing_code(
        4, code_a.code, now=NOW + timedelta(seconds=telegram_service.ATTEMPT_WINDOW_SECONDS + 6),
    )
    assert taken.reason == "user_already_linked"
    # User 4's attempt did not consume client A's code.
    other = telegram_service.redeem_pairing_code(
        5, code_a.code, now=NOW + timedelta(seconds=telegram_service.ATTEMPT_WINDOW_SECONDS + 7),
    )
    assert other.ok and other.client_id == first
    with pytest.raises(telegram_service.TelegramServiceError, match="already_linked"):
        telegram_service.issue_pairing_code(first, now=NOW + timedelta(hours=1))


def test_manual_existing_link_burns_leftover_code(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    issued = telegram_service.issue_pairing_code(client_id, now=NOW)
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO telegram_links (client_id, telegram_user_id, linked_at) VALUES (?, ?, ?)",
        (client_id, 90, "2026-11-08 12:00:01"),
    )
    conn.commit()
    conn.close()
    denied = telegram_service.redeem_pairing_code(91, issued.code, now=NOW + timedelta(seconds=5))
    assert denied.reason == "client_already_linked"
    assert telegram_service.redeem_pairing_code(92, issued.code, now=NOW + timedelta(seconds=6)).reason == "replayed"
    assert telegram_service.get_own_profile(90)["client_id"] == client_id
    assert telegram_service.get_own_profile(91) is None


def test_parallel_redeem_links_exactly_one_user(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    client_id = create_person()
    for round_number in range(12):
        issued = telegram_service.issue_pairing_code(client_id, now=NOW + timedelta(minutes=round_number))
        results = []
        barrier = threading.Barrier(2)

        def worker(user_id, code=issued.code, moment=NOW + timedelta(minutes=round_number, seconds=1)):
            barrier.wait()
            results.append(telegram_service.redeem_pairing_code(user_id, code, now=moment))

        threads = [
            threading.Thread(target=worker, args=(10_000 + round_number * 2,)),
            threading.Thread(target=worker, args=(10_000 + round_number * 2 + 1,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 2
        assert sum(item.ok for item in results) == 1
        assert {item.reason for item in results if not item.ok} <= {"replayed", "race", "client_already_linked"}
        conn = database.get_connection()
        try:
            assert conn.execute("SELECT COUNT(*) FROM telegram_links").fetchone()[0] == 1
            consumed = conn.execute(
                "SELECT consumed_at FROM telegram_pairing_codes WHERE code_hash = ?",
                (telegram_service.hash_pairing_code(issued.code),),
            ).fetchone()
            assert consumed["consumed_at"] is not None
        finally:
            conn.close()
        telegram_service.unlink_client(client_id, now=NOW + timedelta(minutes=round_number, seconds=2))


def test_ownership_hides_other_client_and_does_not_pay(tmp_path, monkeypatch):
    use_db(tmp_path, monkeypatch)
    owner = create_person(full_name="<Иван> Тестов", iin="850101300999", email="secret-mail@test.kz",
                          phone="+77009998877", address="SECRET ADDRESS 9")
    victim = create_person(full_name="Жертва Данных", iin="040404400404", email="victim@test.kz",
                           phone="+77001112233", address="Victim street")
    own = add_application(owner, 400000, 12, "O'Brien & <b>own</b>", iin="850101300999")
    victim_app = add_application(victim, 777001, 12, "VICTIM_PURPOSE_77", income=3_000_000,
                                 iin="040404400404", birth="2004-04-04")
    # add_application also inserts employment. The second call for a new client is fine.
    # The owner already has a job. Victim has income enough for 777001? 777001 > 500000 so manual,
    # and no loan. Give the victim an affordable approved loan too.
    victim_loan = add_application(victim, 100000, 12, "VICTIM_LOAN_SECRET", income=3_000_000,
                                  iin="040404400404", birth="2004-04-04")
    issued = telegram_service.issue_pairing_code(owner, now=NOW)
    assert telegram_service.redeem_pairing_code(777, issued.code, now=NOW).ok

    apps = telegram_service.list_own_applications(777)
    assert [row["application_id"] for row in apps] == [own["application_id"]]
    assert set(apps[0]) <= {
        "application_id", "requested_amount", "term_months", "purpose",
        "status", "approval_type", "created_at", "loan_id",
    }
    assert telegram_service.get_own_application(777, victim_app["application_id"]) is None
    assert telegram_service.get_own_application(777, "1 OR 1=1") is None
    assert telegram_service.get_own_application(778, own["application_id"]) is None
    assert telegram_service.get_own_schedule(777, victim_loan["loan_id"]) is None
    schedule = telegram_service.get_own_schedule(777, own["loan_id"])
    assert schedule is not None and len(schedule["payments"]) == 12
    assert all(item["status"] == "Pending" for item in schedule["payments"])
    profile = telegram_service.get_own_profile(777)
    assert set(profile) == {"client_id", "full_name", "linked_at"}
    assert "850101300999" not in str(profile)

    rendered = telegram_text.render_application_detail(
        telegram_service.get_own_application(777, own["application_id"])
    )
    assert "O&#x27;Brien &amp; &lt;b&gt;own&lt;/b&gt;" in rendered
    assert "VICTIM_PURPOSE_77" not in rendered
    assert "850101300999" not in rendered
    assert "+77009998877" not in rendered
    before = database.get_repayments(own["loan_id"])
    assert telegram_text.render_schedule(schedule)
    assert database.get_repayments(own["loan_id"]) == before


def test_operator_cli_prints_code_once_and_not_pii(tmp_path, monkeypatch, capsys, caplog):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("MFO_DB", str(db_path))
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    database.init_db()
    client_id = create_person(
        full_name="Операторский Клиент", iin="850101300999", email="secret-mail@test.kz",
        phone="+77009998877", address="SECRET ADDRESS 9",
    )
    caplog.set_level(logging.INFO)
    assert telegram_operator.main(["issue", "--client-id", str(client_id)]) == 0
    captured = capsys.readouterr()
    match = CODE_RE.search(captured.out)
    assert match
    code = match.group(0)
    assert code not in captured.err
    assert code not in caplog.text
    for secret in ("850101300999", "+77009998877", "secret-mail@test.kz", "SECRET ADDRESS 9", "pass123"):
        assert secret not in captured.out
        assert secret not in caplog.text
    assert "Операторский Клиент" in captured.out
    blob = db_path.read_bytes()
    assert code.encode() not in blob
    assert code.replace("-", "").encode() not in blob

    assert telegram_operator.main(["status", "--client-id", str(client_id)]) == 0
    status_out = capsys.readouterr().out
    assert "Активный код: есть" in status_out
    assert code not in status_out

    assert telegram_operator.main(["unlink", "--client-id", str(client_id)]) == 0
    assert "Отозвано неиспользованных кодов: 1" in capsys.readouterr().out
    assert telegram_service.redeem_pairing_code(5, code, now=NOW + timedelta(days=1)).reason == "replayed"
    assert telegram_operator.main(["issue", "--client-id", "999"]) == 2
    assert "Клиент не найден" in capsys.readouterr().err


def test_operator_entrypoint_uses_mfo_db(tmp_path):
    db_path = tmp_path / "sub.db"
    env = os.environ.copy()
    env["MFO_DB"] = str(db_path)
    env.pop("TELEGRAM_BOT_TOKEN", None)
    missing = subprocess.run(
        [sys.executable, "telegram_operator.py", "status", "--client-id", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert missing.returncode == 2
    assert "Клиент не найден" in missing.stderr
    assert "Код привязки" not in missing.stdout
    assert db_path.exists()

    create = (
        "import database; database.init_db();"
        "database.create_client('850101300999','Sub Client','1985-01-01','+77009998877',"
        "'SECRET ADDRESS 9','secret-mail@test.kz','pass123','Single','2026-11-08 09:00:00')"
    )
    subprocess.run([sys.executable, "-c", create], cwd=ROOT, env=env, check=True)
    issued = subprocess.run(
        [sys.executable, "telegram_operator.py", "issue", "--client-id", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert issued.returncode == 0
    assert "850101300999" not in issued.stdout
    assert "+77009998877" not in issued.stdout
    assert "secret-mail@test.kz" not in issued.stdout
    code = CODE_RE.search(issued.stdout).group(0)
    assert code.encode() not in db_path.read_bytes()


def test_env_file_sets_database_without_overriding_process(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MFO_DB=" + str(tmp_path / "from-file.db") + "\n"
        "TELEGRAM_BOT_TOKEN=PUT_YOUR_TOKEN_HERE\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MFO_DB", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(database, "DB_FILE", database.DB_FILE)
    bound = telegram_env.bind_database_path()
    assert bound == os.path.abspath(tmp_path / "from-file.db")
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "PUT_YOUR_TOKEN_HERE"
    monkeypatch.setenv("MFO_DB", str(tmp_path / "from-process.db"))
    assert telegram_env.bind_database_path() == os.path.abspath(tmp_path / "from-process.db")


def test_bot_ignores_start_payload_group_and_callback_tamper(tmp_path, monkeypatch, caplog):
    use_db(tmp_path, monkeypatch)
    owner = create_person(full_name="<Иван> Тестов", iin="850101300999", email="secret-mail@test.kz",
                          phone="+77009998877", address="SECRET ADDRESS 9")
    victim = create_person(full_name="Жертва Данных", iin="040404400404", email="victim@test.kz")
    own = add_application(owner, 400000, 12, "O'Brien & <b>own</b>", iin="850101300999")
    victim_app = add_application(victim, 777001, 6, "VICTIM_PURPOSE_77", income=5_000_000, iin="040404400404")
    issued = telegram_service.issue_pairing_code(owner, now=NOW)
    caplog.set_level(logging.INFO)

    start_plain = outgoing_text(run_feed("/start"))
    start_client = outgoing_text(run_feed("/start 1"))
    start_app = outgoing_text(run_feed(f"/start {victim_app['application_id']}"))
    start_code = outgoing_text(run_feed("/start " + issued.code))
    assert start_plain == start_client == start_app == start_code == telegram_text.START_TEXT
    assert "VICTIM_PURPOSE_77" not in start_plain
    assert telegram_service.get_own_profile(501) is None

    for chat_type in ("group", "supergroup", "channel"):
        group = run_feed("/link " + issued.code, chat_type=chat_type, user_id=501)
        assert telegram_text.GROUP_TEXT in outgoing_text(group)
        assert issued.code not in outgoing_text(group)
        assert "VICTIM_PURPOSE_77" not in outgoing_text(group)
        silent = run_feed(issued.code, chat_type=chat_type, user_id=501)
        assert silent.calls == []
    assert telegram_service.redeem_pairing_code(501, issued.code, now=NOW).ok

    help_text = outgoing_text(run_feed("/help", user_id=501))
    assert "/schedule" in help_text and "ИИН" in help_text

    caplog.clear()
    linked = run_feed("/link " + issued.code.lower(), user_id=501)
    assert "уже привязан" in outgoing_text(linked).lower() or "уже привязан" in outgoing_text(linked)
    assert issued.code not in caplog.text
    assert "850101300999" not in caplog.text

    status = run_feed("/status", user_id=501)
    text = outgoing_text(status)
    assert "одобрена" in text
    assert "400 000,00 ₸" in text
    assert "O&#x27;Brien" not in text
    assert "VICTIM_PURPOSE_77" not in text
    assert "850101300999" not in text
    assert "+77009998877" not in text
    assert "secret-mail@test.kz" not in text
    assert f"app:{own['application_id']}" in button_data(status)
    assert f"sch:{own['loan_id']}" in button_data(status)
    assert all(not item.startswith("pay:") for item in button_data(status))
    assert f"app:{victim_app['application_id']}" not in button_data(status)

    detail = run_feed("/app " + str(own["application_id"]), user_id=501)
    detail_text = outgoing_text(detail)
    assert "O&#x27;Brien &amp; &lt;b&gt;own&lt;/b&gt;" in detail_text
    assert "850101300999" not in detail_text
    assert "пройдена" in detail_text

    stolen = run_feed("/app " + str(victim_app["application_id"]), user_id=501)
    assert "не принадлежит" in outgoing_text(stolen)
    assert "VICTIM_PURPOSE_77" not in outgoing_text(stolen)
    assert "777 001,00 ₸" not in outgoing_text(stolen)
    injected = run_feed("/app 1 OR 1=1", user_id=501)
    assert "Укажите номер" in outgoing_text(injected)

    payments_before = database.get_repayments(own["loan_id"])
    schedule = run_feed("/schedule", user_id=501)
    schedule_text = outgoing_text(schedule)
    assert "37 823,84 ₸" in schedule_text
    assert "ожидается" in schedule_text
    assert "не принимает оплату" in schedule_text
    assert database.get_repayments(own["loan_id"]) == payments_before
    assert "VICTIM_PURPOSE_77" not in schedule_text

    tampered = run_feed(callback_data=f"app:{victim_app['application_id']}", user_id=501)
    alerts = [call.text for call in tampered.calls if isinstance(call, AnswerCallbackQuery)]
    assert alerts == ["Заявка недоступна"]
    assert "VICTIM_PURPOSE_77" not in outgoing_text(tampered)
    bad_callback = run_feed(callback_data="app:1 OR 1=1", user_id=501)
    assert [call.text for call in bad_callback.calls if isinstance(call, AnswerCallbackQuery)] == ["Недоступно"]

    own_button = run_feed(callback_data=f"sch:{own['loan_id']}", user_id=501)
    assert "37 823,84 ₸" in outgoing_text(own_button)
    assert database.get_repayments(own["loan_id"]) == payments_before

    group_button = run_feed(
        callback_data=f"app:{own['application_id']}", user_id=501, chat_type="group",
    )
    assert [call.text for call in group_button.calls if isinstance(call, AnswerCallbackQuery)] == ["Только личный чат"]
    assert outgoing_text(group_button) == ""

    bare = run_feed("ZZZZ-ZZZZ", user_id=501)
    assert telegram_text.UNKNOWN_TEXT in outgoing_text(bare)
    assert "ZZZZ-ZZZZ" not in outgoing_text(bare)

    stranger = run_feed("/status", user_id=999)
    assert telegram_text.NOT_LINKED_TEXT in outgoing_text(stranger)
    assert "400 000,00 ₸" not in outgoing_text(stranger)

    assert "Привязка удалена" in outgoing_text(run_feed("/unlink", user_id=501))
    assert telegram_service.get_own_profile(501) is None
    assert "не привязан" in outgoing_text(run_feed("/unlink", user_id=501))
    # A different user cannot remove a link that is already gone, and never saw the loan.
    assert telegram_text.NOT_LINKED_TEXT in outgoing_text(run_feed("/loan " + str(own["loan_id"]), user_id=999))


def test_link_flow_and_payment_status_from_api(client):
    created = client.post("/api/clients", json={
        "iin": "850101300999", "full_name": "<Иван> Тестов", "birth_date": "2005-01-01",
        "phone": "+77009998877", "address": "SECRET ADDRESS 9", "email": "secret-mail@test.kz",
        "password": "pass123", "marital_status": "Single",
    })
    assert created.status_code == 201
    client_id = created.json()["client_id"]
    assert client.post(
        f"/api/clients/{client_id}/employment",
        json={"employer_name": "Kaspi", "position": "Analyst", "monthly_income": 300000},
    ).status_code == 201
    approved = client.post("/api/applications", json={
        "client_id": client_id, "requested_amount": 400000, "term_months": 12, "purpose": "<tag>",
    }).json()
    assert approved["status"] == "Approved"
    rejected = client.post("/api/applications", json={
        "client_id": client_id, "requested_amount": 400000, "term_months": 1, "purpose": "too big",
    }).json()
    assert rejected["status"] == "Rejected"

    issued = telegram_service.issue_pairing_code(client_id)
    linked = run_feed("/link " + issued.code, user_id=321)
    assert "&lt;Иван&gt; Тестов" in outgoing_text(linked)
    assert "850101300999" not in outgoing_text(linked)
    status = outgoing_text(run_feed("/applications", user_id=321))
    assert "одобрена" in status and "отклонена" in status
    loan_text = outgoing_text(run_feed("/loan " + str(approved["loan_id"]), user_id=321))
    assert "37 823,84 ₸" in loan_text
    assert "График не найден" in outgoing_text(run_feed("/loan 99999", user_id=321))

    payment_id = client.get(f"/api/loans/{approved['loan_id']}/repayments").json()[0]["payment_id"]
    assert client.post(f"/api/repayments/{payment_id}/pay").status_code == 200
    paid_view = outgoing_text(run_feed("/payments", user_id=321))
    assert "оплачен" in paid_view
    # The bot has no way to mark the next payment paid.
    second = client.get(f"/api/loans/{approved['loan_id']}/repayments").json()[1]
    assert second["status"] == "Pending"


def test_openapi_stays_18_operations_without_telegram(client):
    spec = client.get("/openapi.json").json()
    operations = {
        (method, path)
        for path, methods in spec["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "delete", "patch"} and path.startswith("/api")
    }
    assert operations == {
        ("post", "/api/clients"),
        ("get", "/api/clients"),
        ("get", "/api/clients/{client_id}"),
        ("delete", "/api/clients/{client_id}"),
        ("post", "/api/clients/{client_id}/employment"),
        ("get", "/api/clients/{client_id}/employment"),
        ("post", "/api/clients/{client_id}/documents"),
        ("get", "/api/clients/{client_id}/documents"),
        ("get", "/api/employees"),
        ("post", "/api/applications"),
        ("get", "/api/applications"),
        ("get", "/api/applications/{application_id}"),
        ("get", "/api/applications/{application_id}/checks"),
        ("post", "/api/applications/{application_id}/decision"),
        ("get", "/api/loans"),
        ("get", "/api/loans/{loan_id}"),
        ("get", "/api/loans/{loan_id}/repayments"),
        ("post", "/api/repayments/{payment_id}/pay"),
    }
    assert len(operations) == 18
    blob = str(spec).lower()
    assert "telegram" not in blob and "pairing" not in blob
    assert client.get("/docs").status_code == 200
    assert "telegram" not in open(os.path.join(ROOT, "main.py"), encoding="utf-8").read().lower()


def test_bot_does_not_start_without_a_real_token(monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", database.DB_FILE)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

    def explode(token):
        raise AssertionError(token)

    monkeypatch.setattr(telegram_bot, "run_polling", explode)
    assert telegram_bot.main([]) == 2
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    assert telegram_bot.main([]) == 2


def test_sources_have_no_bot_token():
    allowed_suffixes = (".py", ".md", ".txt", ".example", ".ini", ".html")
    for folder, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [
            name for name in dirnames
            if name not in {"venv", ".venv", "__pycache__", ".pytest_cache", "tests"}
        ]
        for name in filenames:
            if not name.endswith(allowed_suffixes):
                continue
            text = open(os.path.join(folder, name), encoding="utf-8").read()
            assert TOKEN_RE.search(text) is None, name
    example = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    assert "PUT_YOUR_TOKEN_HERE" in example
    assert "TELEGRAM_BOT_TOKEN" in example


def test_callback_parser_and_long_schedule_split():
    assert telegram_text.parse_callback("app:12") == ("app", 12)
    assert telegram_text.parse_callback("sch:3") == ("sch", 3)
    for bad in ("app:0", "app:-1", "app:1 OR 1=1", "pay:1", "app:1:2", "", None, "sch:" + "9" * 19):
        assert telegram_text.parse_callback(bad) is None
    long_text = "\n".join(f"строка {index}" for index in range(500))
    parts = telegram_text.split_text(long_text, limit=200)
    assert len(parts) > 1
    assert "\n".join(parts) == long_text
    assert all(len(part) <= 200 for part in parts)
