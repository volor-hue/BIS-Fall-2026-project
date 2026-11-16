# Microfinance IS: как запустить и проверить прототип

Прототип системы выдачи микрокредитов (рынок Казахстана, суммы в KZT).
Стек: **FastAPI + sqlite3 (встроен в Python) + HTML/Bootstrap 5 + JS fetch**, проверка лица по веб-камере на
**OpenCV (YuNet + SFace)**, Telegram-бот на aiogram. Docker не нужен.

## Быстрый старт (3 команды)

```bash
pip install -r requirements.txt
cp .env.example .env          # Windows: copy .env.example .env  — и впишите TELEGRAM_BOT_TOKEN
python run.py                 # сайт http://127.0.0.1:8001 + Telegram-бот
```
- Без токена бота: `python run.py --web-only` (только сайт).
- Модели для проверки лица (~38 МБ) скачиваются сами в папку `models/` при первом запуске.
- Подробно про бота: [`README_TELEGRAM.md`](README_TELEGRAM.md), про проверку лица: [`docs/FACE_AUTH_RU.md`](docs/FACE_AUTH_RU.md).

## Структура проекта

```
mfo-prototype/
├── main.py              # FastAPI: REST API (JSON) + HTML-страницы
├── database.py          # SQLite: 8 таблиц по ERD + face_tokens + Telegram, все SQL-запросы
├── approval.py          # бизнес-правила: одобрение, автопроверки, график платежей
├── log_config.py        # логи: logs/app.log (запросы, ошибки) и logs/audit.log (кто что сделал)
├── rate_limit.py        # защита от спама запросами (slowapi): лимиты по IP, ответ 429
├── auth_jwt.py          # JWT (HS256): выдача и проверка подписанных токенов входа
├── face_verify.py       # проверка лица: YuNet находит лицо, SFace делает вектор, косинусное сходство
├── run.py               # одна команда: сайт (127.0.0.1:8001) + Telegram-бот
├── telegram_*.py        # Telegram-бот клиента (см. README_TELEGRAM.md)
├── .env.example         # образец .env: токен бота, лимиты запросов (скопировать в .env)
├── requirements.txt     # библиотеки
├── pytest.ini           # настройки pytest
├── docs/
│   ├── ERD.md           # ER-диаграмма (mermaid) и таблица связей
│   └── FACE_AUTH_RU.md  # как работает проверка лица (для защиты)
├── templates/           # HTML-страницы
│   ├── base.html        # общий шаблон (меню, Bootstrap, JS-помощники)
│   ├── register.html    # регистрация (clients + employment + documents)
│   ├── login.html       # вход клиента (email) или сотрудника (login)
│   ├── dashboard.html   # кабинет клиента: заявки, кредиты, график, оплата
│   ├── face.html        # веб-камера: снимок -> POST /api/face/verify -> отправка заявки
│   └── admin.html       # кабинет сотрудника: заявки, проверки, Approve/Reject, кредиты
├── tests/
│   ├── conftest.py      # общая настройка тестов (временная БД и папка логов)
│   ├── test_approval.py # тесты бизнес-правил
│   ├── test_api.py      # тесты API, внешних ключей, каскадного удаления, страниц
│   ├── test_logging.py  # тесты логов: события пишутся, пароли и ИИН не пишутся
│   ├── test_rate_limit.py # тесты лимитов: после N запросов ответ 429 и WARNING в логе
│   ├── test_jwt.py      # тесты JWT: подпись, срок, подделка, /api/auth/*, cookie
│   ├── test_access.py   # тесты доступа: 401 без токена, 403 к чужим данным, права сотрудника
│   ├── test_face.py     # тесты проверки лица и face-гейта (без настоящих фото, verify подменяется)
│   └── test_telegram.py, test_staff_launcher.py  # тесты бота и run.py
├── models/              # создаётся сама: модели YuNet + SFace (в git не попадает)
├── uploads/             # сюда сохраняются загруженные документы
├── logs/                # создаётся сама: app.log и audit.log (в git не попадает)
└── README_RUN.md        # эта инструкция
```

База `mfo.db` создаётся сама при первом запуске.

## База данных (по ERD команды)

8 таблиц: `clients`, `employees`, `employment`, `documents`, `loan_applications`, `client_checks`, `loans`, `repayments`.
Плюс служебные таблицы вне ERD: `face_tokens` (одноразовые токены проверки лица, хранится только SHA-256) и таблицы Telegram-бота (`telegram_links`, `telegram_pairing_codes`, `telegram_link_attempts`).
Диаграмма и все связи описаны в [`docs/ERD.md`](docs/ERD.md).

**Что мы добавили к ERD** (это отмечено и в комментариях в `database.py`):
- `clients`: `email`, `password_hash`, `marital_status`, `created_at` нужны для регистрации и входа;
- `employees`: `password_hash` нужен для входа сотрудника;
- `loan_applications`: `term_months`, `purpose`, `approval_type`, `created_at` нужны для правила одобрения.

## 1. Установить Python 3.10+

- Скачать: https://www.python.org/downloads/
- **Windows:** при установке отметьте **«Add Python to PATH»**.
- Проверка: `python --version` (Windows) или `python3 --version` (macOS/Linux).

## 2. Создать виртуальное окружение (venv)

Откройте терминал **в папке проекта** `mfo-prototype`.

**Windows (PowerShell или cmd):**
```bat
python -m venv venv
venv\Scripts\activate
```
> Если PowerShell пишет, что выполнение скриптов запрещено, выполните один раз
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, затем снова `venv\Scripts\activate`.

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```
Если всё получилось, в начале строки появится `(venv)`.

## 3. Установить зависимости
```bash
pip install -r requirements.txt
```

## 4. Запустить сервер
```bash
python run.py              # сайт + Telegram-бот (нужен TELEGRAM_BOT_TOKEN в .env)
python run.py --web-only   # только сайт
```
- Сайт: **http://127.0.0.1:8001**
- Swagger (все эндпоинты): **http://127.0.0.1:8001/docs**
- Остановить: `Ctrl + C`
- Можно и без run.py: `uvicorn main:app --reload --port 8001`.
- При первом запуске в `models/` скачиваются модели для проверки лица (~38 МБ, нужен интернет).

> Ниже в примерах адрес `127.0.0.1:8000` — если вы запускали через `run.py`, используйте порт **8001**.

## Сотрудник по умолчанию (создаётся автоматически)

| Логин | Пароль | Роль |
|-------|--------|------|
| `admin` | `admin123` | admin (кредитный специалист) |

На странице входа одно поле: сотрудник вводит **логин** (`admin`), клиент вводит свой **email**.

## Правило одобрения

Доход клиента считается как **сумма `monthly_income` по всем его местам работы** (таблица `employment`).

| Сумма | Условие | status | approval_type |
|-------|---------|--------|---------------|
| ≤ 500 000 KZT | доход × 0.4 ≥ сумма / срок | Approved | Auto |
| ≤ 500 000 KZT | иначе | Rejected | Auto |
| > 500 000 KZT | всегда | Pending Admin Review, дальше решает сотрудник | Manual |

- Для каждой заявки записываются автопроверки в `client_checks`: `IIN_FORMAT`, `AGE_18`, `INCOME_DTI` (правило 40%), `AMOUNT_LIMIT`.
  В прототипе они информационные: решение принимает только правило из таблицы выше.
- Когда заявка получает статус **Approved** (автоматически или сотрудником), создаётся запись в `loans`
  (ставка 24% годовых) и **график из `term_months` ежемесячных платежей** в `repayments` (аннуитет: каждый месяц одинаковая сумма).
- Когда сотрудник принимает решение, в заявку записывается его `employee_id`.

## Эндпоинты API

| Метод | Путь | Таблица | Что делает |
|-------|------|---------|-----------|
| POST | `/api/clients` | clients | создать клиента (JSON) |
| GET | `/api/clients` | clients | список клиентов |
| GET | `/api/clients/{client_id}` | clients | один клиент + суммарный доход |
| DELETE | `/api/clients/{client_id}` | clients | удалить клиента (каскадно удаляет всё связанное) |
| POST | `/api/clients/{client_id}/employment` | employment | добавить место работы |
| GET | `/api/clients/{client_id}/employment` | employment | места работы клиента |
| POST | `/api/clients/{client_id}/documents` | documents | загрузить файл (form-data: `doc_type`, `file`); файл попадает в `uploads/`, в БД пишутся путь и sha256 |
| GET | `/api/clients/{client_id}/documents` | documents | документы клиента |
| GET | `/api/employees` | employees | список сотрудников |
| POST | `/api/applications` | loan_applications (+ client_checks, loans, repayments) | подать заявку |
| GET | `/api/applications` | loan_applications | все заявки (можно `?client_id=1`) |
| GET | `/api/applications/{application_id}` | loan_applications | одна заявка |
| GET | `/api/applications/{application_id}/checks` | client_checks | автопроверки заявки |
| POST | `/api/applications/{application_id}/decision` | loan_applications (+ loans, repayments) | решение сотрудника `{"employee_id":1,"decision":"approve"/"reject"}` |
| GET | `/api/loans` | loans | все кредиты (можно `?client_id=1`) |
| GET | `/api/loans/{loan_id}` | loans | один кредит (сколько платежей оплачено) |
| GET | `/api/loans/{loan_id}/repayments` | repayments | график платежей |
| POST | `/api/repayments/{payment_id}/pay` | repayments | отметить платёж как Paid |
| POST | `/api/face/verify` | documents, face_tokens | снимок с веб-камеры (form-data `file` или `image_base64`) сравнивается с фото 3x4; при совпадении выдаётся одноразовый `face_token` |

> **Face-гейт:** клиент может подать заявку (`POST /api/applications`) только с полем `face_token` из успешной
> проверки лица (одноразовый, живёт 5 минут). Без него ответ **403**. Сотрудник, заводящий заявку за клиента, проверку не проходит.

> API защищён JWT (см. раздел «JWT-авторизация» ниже). В `/docs` сначала нажмите **Authorize**.

## 5. Тестовый сценарий через сайт

1. http://127.0.0.1:8000/register: зарегистрируйте клиента
   - IIN `050101500123`, Birth date `2005-01-01`, Email `client@test.kz`, Password `pass123`;
   - Employer `Kaspi`, **Monthly income `300000`**, приложите любую картинку как паспорт
     и **своё фото 3x4** (JPG/PNG) в поле «Photo 3x4» — с ним будет сравниваться веб-камера.
   - Проверка ошибки: с IIN `123` появится сообщение «exactly 12 digits».
2. Войдите как `client@test.kz` / `pass123`.
3. Каждая заявка: **Submit application** → страница `/face` (разрешите доступ к камере) → **Take snapshot and verify**.
   Если лицо совпало, заявка отправится сама; если нет, заявка не отправляется.
   Заявка **400000 на 12 мес.** даёт **Approved (Auto)**, в «My loans» появится кредит. Нажмите **Schedule**, чтобы увидеть 12 платежей по 37 823,84 ₸.
4. Заявка **400000 на 1 мес.** даёт **Rejected (Auto)**: 400 000 больше 40% от 300 000.
5. Заявка **1000000 на 12 мес.** даёт **Pending Admin Review (Manual)**.
6. Logout, затем войдите как `admin` / `admin123`. Нажмите **Checks** у заявки (видно `AMOUNT_LIMIT: MANUAL_REVIEW`), потом **Approve**.
   В колонке «Reviewed by» появится имя сотрудника, а в «Issued loans» появится второй кредит.
7. Снова войдите как клиент: заявка на 1 000 000 в статусе **Approved (Manual)**, у неё есть кредит и график.
   Нажмите **Pay** у первого платежа, он станет **Paid**, а в столбце «Paid» будет 1 / 12.

## 6. Тестовый сценарий через Swagger (/docs)

Откройте http://127.0.0.1:8000/docs. У каждого эндпоинта нажмите **Try it out**, затем **Execute**.

1. `POST /api/clients` с телом-примером (уже заполнено). Ответ 201, запомните `client_id` (например 1).
2. `POST /api/clients/1/employment` с телом `{"employer_name":"Kaspi","position":"Analyst","monthly_income":300000}`.
3. `POST /api/clients/1/documents`: выберите файл. В ответе будут `file_path` и `file_hash`.
4. `GET /api/clients/1` покажет `total_monthly_income: 300000`.
5. `POST /api/applications` три раза (вы вошли через Authorize как `admin`, поэтому `face_token` не нужен):
   - `{"client_id":1,"requested_amount":400000,"term_months":12}` даёт Approved / Auto и `loan_id`;
   - `{"client_id":1,"requested_amount":400000,"term_months":1}` даёт Rejected / Auto;
   - `{"client_id":1,"requested_amount":1000000,"term_months":12}` даёт Pending Admin Review / Manual.
6. `GET /api/applications/3/checks` вернёт 4 проверки.
7. `GET /api/employees` вернёт сотрудника с `employee_id = 1`.
8. `POST /api/applications/3/decision` с телом `{"employee_id":1,"decision":"approve"}` даёт Approved и `employee_id: 1`.
9. `GET /api/loans` вернёт 2 кредита. `GET /api/loans/1/repayments` вернёт 12 платежей.
10. `POST /api/repayments/1/pay` даёт `Paid`. Повторный вызов вернёт 400 «already paid».
11. **Проверка внешнего ключа:** `POST /api/clients/999/employment` вернёт 400 «FOREIGN KEY constraint failed».
12. **Проверка каскадного удаления (по желанию):** `DELETE /api/clients/1`. После этого заявки, проверки, кредиты и платежи этого клиента тоже исчезнут.

## 7. Автотесты (pytest)

При активированном venv в папке проекта:
```bash
pytest
```
Исходные сценарии этого файла по-прежнему входят в набор (24 теста в `test_api.py` и `test_approval.py`). Полный `pytest` вместе с Telegram-ботом описан в [`README_TELEGRAM.md`](README_TELEGRAM.md). Сайт и бот одной командой: `python run.py` (порт сайта по умолчанию 8001). Тесты используют временную базу, поэтому ваш `mfo.db` не трогают.
- `tests/test_approval.py`: правило одобрения (границы 500 000 и 40%), автопроверки, график платежей.
- `tests/test_api.py`: создание клиента, уникальность ИИН, суммирование дохода, загрузка документа с sha256,
  полный процесс с тремя заявками и решением сотрудника, оплата платежа, **FOREIGN KEY** (API и напрямую в SQLite),
  связь 1-к-1 «заявка → кредит» (UNIQUE), **ON DELETE CASCADE**, HTML-регистрация и вход, наличие всех эндпоинтов в `/docs`.

## 8. Как сбросить базу данных

Остановите сервер (`Ctrl + C`), затем удалите `mfo.db` и, если нужно, файлы в `uploads/` (кроме `.gitkeep`):
- Windows: `del mfo.db`
- macOS/Linux: `rm mfo.db`

При следующем запуске база создастся заново: пустая, но с сотрудником `admin`.

Посмотреть таблицы глазами можно в бесплатной программе **DB Browser for SQLite**, открыв `mfo.db`.

## 9. Логи

Сайт пишет логи в консоль и в папку `logs/` (она создаётся сама). Используется стандартный модуль Python `logging`,
настройка лежит в `log_config.py`.

| Файл | Что внутри |
|------|------------|
| `logs/app.log` | каждый HTTP-запрос (метод, путь, статус, время в мс, IP, пользователь), ошибки с traceback, а также копия строк аудита |
| `logs/audit.log` | журнал безопасности: **кто, что и когда** сделал |

События в `audit.log`: `register_ok` / `register_failed`, `login_ok` / `login_failed`, `logout`, `document_uploaded`,
`application_submitted`, `application_decided`, `loan_issued`, `payment_paid`, `client_deleted`,
`face_verify` (результат проверки лица: `match` и `score`, сам снимок не пишется), `face_gate_denied` (заявка без
действующего `face_token`), `rate_limited` (превышен лимит запросов, см. раздел 10).

Пример:
```
2026-11-09 12:53:17,384 WARNING mfo.audit event=login_failed ip=127.0.0.1 user=anon login=ad***
2026-11-09 12:53:17,401 INFO mfo.audit event=login_ok ip=127.0.0.1 user=anon role=employee user_id=1
2026-11-09 12:53:17,445 INFO mfo.audit event=logout ip=127.0.0.1 user=employee:1
```

Правила:
- **Не пишем в логи** пароль, хеш пароля, ИИН, телефон, cookie и токены. Логин при неудачном входе маскируется (`ad***`).
- Всё, что ввёл пользователь, очищается от переносов строк, чтобы нельзя было подделать строку лога (log injection).
- Файлы ротируются: при 1 МБ `app.log` становится `app.log.1` и т. д. Хранится 5 старых `app.log` и 20 старых `audit.log`.
- 4xx/5xx-ответы и неудачные входы пишутся с уровнем `WARNING`, падения кода с уровнем `ERROR`.

Настройка через переменные окружения: `MFO_LOG_DIR` (папка, по умолчанию `logs`), `MFO_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`...).

Полезные команды (macOS/Linux):
```bash
tail -f logs/app.log                         # смотреть запросы в реальном времени
grep login_failed logs/audit.log             # все неудачные входы
grep "user=employee:1" logs/audit.log        # всё, что сделал сотрудник 1
```

## 10. Защита от спама запросами (rate limit)

Библиотека **slowapi** считает запросы с каждого IP-адреса. Настройка лежит в `rate_limit.py`.

| Что | Лимит по умолчанию | Переменная |
|-----|--------------------|------------|
| Вход `POST /login` и регистрация `POST /register` | 5 в минуту | `MFO_RATE_LIMIT_AUTH` |
| Подача заявки `POST /api/applications`, загрузка документов `POST /api/clients/{id}/documents` и проверка лица `POST /api/face/verify` | 10 в минуту | `MFO_RATE_LIMIT_SUBMIT` |
| Все остальные адреса (API и страницы) | 60 в минуту | `MFO_RATE_LIMIT_DEFAULT` |

- У каждого адреса свой счётчик. `/api/clients/1` и `/api/clients/2` считаются одним адресом, поэтому перебор id лимит не обходит.
- Окно «скользящее»: «5 в минуту» значит не больше 5 запросов за любые 60 секунд подряд.
- При превышении ответ **429 Too Many Requests** и заголовок `Retry-After` (сколько секунд ждать).
  Формы входа и регистрации показывают ошибку на странице, API отвечает JSON `{"detail": "Too many requests..."}`.
- В `logs/app.log` каждый заблокированный запрос записывается как `WARNING ... status=429`. В `logs/audit.log` пишется
  `WARNING event=rate_limited`, но не чаще одного раза в минуту на IP и лимит. Иначе спамер мог бы забить журнал
  аудита, и ротация удалила бы старые важные записи.

Лимиты меняются в `.env` (образец в `.env.example`). Формат: `<число>/<second|minute|hour|day>`, несколько лимитов через `;`:
```
MFO_RATE_LIMIT_AUTH=5/minute;50/hour
```
Сайт берёт из `.env` только эти три переменные. Переменная окружения важнее файла. Если значение записано с ошибкой,
используется значение по умолчанию и в лог пишется `WARNING` (защита не выключается молча). После изменения
перезапустите сервер.

## Частые проблемы

- `uvicorn` не найден: не активирован venv или не установлены зависимости. Можно запустить так: `python -m uvicorn main:app --reload`.
- `Address already in use`: порт занят, запустите `uvicorn main:app --reload --port 8001`.
- Страница без стилей: Bootstrap грузится из интернета (CDN), нужен доступ к сети.
- Ошибка `no such column` после обновления кода: удалите старый `mfo.db` (см. раздел 8), потому что схема изменилась.

## Ограничения прототипа (честно)

- JWT нельзя отозвать до истечения срока (нет чёрного списка токенов): после выхода украденный токен живёт до `exp`.
- Пароли хешируются SHA-256 без соли. В реальной системе нужен bcrypt или argon2.
- Автопроверки `IIN_FORMAT` и `AGE_18` пока не влияют на решение.
- Счётчики rate limit хранятся в памяти: сбрасываются при перезапуске, а при нескольких воркерах uvicorn у каждого
  свой счётчик. В реальной системе нужно общее хранилище (Redis). За обратным прокси (nginx) все запросы приходят
  с IP прокси и делят один лимит: нужно настроить доверенный заголовок `X-Forwarded-For`.
- Ставка одна для всех (24% годовых), просрочки и штрафы не считаются.
- Проверка лица без liveness: распечатанное фото перед камерой может пройти (см. `docs/FACE_AUTH_RU.md`).

## JWT-авторизация

Вход на сайте и через API подтверждается **JWT-токеном** (HS256, подпись секретом сервера, `auth_jwt.py`).
Раньше в cookie лежал просто `client_id`/`employee_id`, и его мог подменить кто угодно; теперь это
подписанный токен в HttpOnly-cookie `access_token`, изменённый или просроченный токен не принимается.

API (раздел **auth** в `/docs`, кнопка **Authorize** работает с логином и паролем):

| Метод | Адрес | Что делает |
|---|---|---|
| POST | `/api/auth/register` | регистрация клиента (JSON, как `/api/clients` + `employer_name`, `position`, `monthly_income`), сразу возвращает токен |
| POST | `/api/auth/login` | форма `username` (email клиента или логин сотрудника) + `password`, возвращает `access_token` |
| GET | `/api/auth/me` | кто владелец токена, заголовок `Authorization: Bearer <token>` |

Пример:
```bash
curl -X POST http://127.0.0.1:8001/api/auth/login -d "username=admin&password=admin123"
curl http://127.0.0.1:8001/api/auth/me -H "Authorization: Bearer <access_token>"
```

Настройки в `.env`: `MFO_JWT_SECRET` (обязательно задайте на сервере, минимум 32 символа) и
`MFO_JWT_EXPIRE_MINUTES` (по умолчанию 60).

### Кто что может в API

Все `/api/*` защищены. Без токена ответ **401**, к чужим данным **403**.

| Кто | Доступ |
|---|---|
| без входа | только `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/clients` (регистрация) |
| клиент | только свои данные: профиль, работа, документы, свои заявки (подача только за себя), свои кредиты, график и оплата своих платежей; списки `/api/applications` и `/api/loans` всегда только свои |
| сотрудник | всё, включая список клиентов, удаление клиента, `/api/employees`, автопроверки заявки и решение по заявке |

Решение по заявке записывается на сотрудника **из токена**: поле `employee_id` в теле
необязательно, а если оно не совпадает с токеном, ответ 403.

Страницы `/dashboard` и `/admin` обращаются к API со своей cookie `access_token`, отдельный заголовок им не нужен.
В `/docs` нажмите **Authorize**, введите логин и пароль (например `admin` / `admin123`), и все запросы пойдут с токеном.
