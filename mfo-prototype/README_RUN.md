# Microfinance IS: как запустить и проверить прототип

Прототип системы выдачи микрокредитов (рынок Казахстана, суммы в KZT).
Стек: **FastAPI + sqlite3 (встроен в Python) + HTML/Bootstrap 5 + JS fetch**. Docker не нужен.

## Структура проекта

```
mfo-prototype/
├── main.py              # FastAPI: REST API (JSON) + HTML-страницы
├── database.py          # SQLite: 8 таблиц по ERD, внешние ключи, все SQL-запросы
├── approval.py          # бизнес-правила: одобрение, автопроверки, график платежей
├── requirements.txt     # библиотеки
├── pytest.ini           # настройки pytest
├── docs/
│   └── ERD.md           # ER-диаграмма (mermaid) и таблица связей
├── templates/           # HTML-страницы
│   ├── base.html        # общий шаблон (меню, Bootstrap, JS-помощники)
│   ├── register.html    # регистрация (clients + employment + documents)
│   ├── login.html       # вход клиента (email) или сотрудника (login)
│   ├── dashboard.html   # кабинет клиента: заявки, кредиты, график, оплата
│   └── admin.html       # кабинет сотрудника: заявки, проверки, Approve/Reject, кредиты
├── tests/
│   ├── conftest.py      # общая настройка тестов (временная БД)
│   ├── test_approval.py # тесты бизнес-правил
│   └── test_api.py      # тесты API, внешних ключей, каскадного удаления, страниц
├── uploads/             # сюда сохраняются загруженные документы
└── README_RUN.md        # эта инструкция
```

База `mfo.db` создаётся сама при первом запуске.

## База данных (по ERD команды)

8 таблиц: `clients`, `employees`, `employment`, `documents`, `loan_applications`, `client_checks`, `loans`, `repayments`.
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
uvicorn main:app --reload
```
- Сайт: **http://127.0.0.1:8000**
- Swagger (все эндпоинты): **http://127.0.0.1:8000/docs**
- Остановить сервер: `Ctrl + C`

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

> В прототипе API открыт (без авторизации), чтобы его было удобно проверять в `/docs`.
> Защита API входом запланирована как задача по безопасности.

## 5. Тестовый сценарий через сайт

1. http://127.0.0.1:8000/register: зарегистрируйте клиента
   - IIN `050101500123`, Birth date `2005-01-01`, Email `client@test.kz`, Password `pass123`;
   - Employer `Kaspi`, **Monthly income `300000`**, приложите любую картинку как паспорт.
   - Проверка ошибки: с IIN `123` появится сообщение «exactly 12 digits».
2. Войдите как `client@test.kz` / `pass123`.
3. Заявка **400000 на 12 мес.** даёт **Approved (Auto)**, в «My loans» появится кредит. Нажмите **Schedule**, чтобы увидеть 12 платежей по 37 823,84 ₸.
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
5. `POST /api/applications` три раза:
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
Ожидается: **`24 passed`**. Тесты используют временную базу, поэтому ваш `mfo.db` не трогают.
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

## Частые проблемы

- `uvicorn` не найден: не активирован venv или не установлены зависимости. Можно запустить так: `python -m uvicorn main:app --reload`.
- `Address already in use`: порт занят, запустите `uvicorn main:app --reload --port 8001`.
- Страница без стилей: Bootstrap грузится из интернета (CDN), нужен доступ к сети.
- Ошибка `no such column` после обновления кода: удалите старый `mfo.db` (см. раздел 8), потому что схема изменилась.

## Ограничения прототипа (честно)

- Вход хранит id в обычной cookie (её можно подделать). В реальной системе нужны подписанные сессии или JWT.
- Пароли хешируются SHA-256 без соли. В реальной системе нужен bcrypt или argon2.
- REST API открыт без авторизации, это сделано для проверки через `/docs`.
- Автопроверки `IIN_FORMAT` и `AGE_18` пока не влияют на решение.
- Ставка одна для всех (24% годовых), просрочки и штрафы не считаются.
