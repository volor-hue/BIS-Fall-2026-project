# database.py
# ------------------------------------------------------------
# Everything about the SQLite database lives here.
# We use Python's built-in "sqlite3" module (no ORM, no SQLAlchemy).
# The whole database is ONE file: mfo.db (created automatically).
#
# The tables follow the team's ERD (see docs/ERD.md):
#   clients 1--* employment      (a client "has" jobs)
#   clients 1--* documents       (a client "uploads" documents)
#   clients 1--* loan_applications (a client "submits" applications)
#   employees 1--* loan_applications (an employee "reviews" applications)
#   loan_applications 1--* client_checks (an application "gets" checks)
#   loan_applications 1--1 loans (an approved application "becomes" a loan)
#   loans 1--* repayments        (a loan "has" monthly payments)
# ------------------------------------------------------------
import hashlib
import os
import sqlite3

# The file name can be changed with an environment variable (tests use a temp file).
DB_FILE = os.environ.get("MFO_DB", "mfo.db")

# Default employee (created on first start). See README_RUN.md.
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin123"


def get_connection():
    # sqlite3.connect() opens the file (and creates it if it does not exist).
    conn = sqlite3.connect(DB_FILE)
    # row_factory = sqlite3.Row lets us read columns by name: row["iin"]
    conn.row_factory = sqlite3.Row
    # IMPORTANT: SQLite checks FOREIGN KEY rules only if we switch them on,
    # and this must be done for EVERY new connection.
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def hash_password(password):
    # We never store the password itself, only its SHA-256 "fingerprint".
    # NOTE: simplified for the prototype. A real system must use bcrypt/argon2.
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def init_db():
    conn = get_connection()

    # executescript() runs several SQL commands at once.
    # CREATE TABLE IF NOT EXISTS -> create the table only the first time.
    #
    # FOREIGN KEY (x) REFERENCES other(y) -> x must be an existing id in "other".
    # ON DELETE CASCADE  -> if the parent row is deleted, child rows are deleted too.
    # ON DELETE SET NULL -> if the parent is deleted, the link becomes empty (NULL).
    conn.executescript("""
        -- CLIENTS: columns from the ERD + 4 extra columns we need for
        -- registration and login: email, password_hash, marital_status, created_at.
        CREATE TABLE IF NOT EXISTS clients (
            client_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            iin            TEXT NOT NULL UNIQUE,      -- UK in the ERD
            full_name      TEXT NOT NULL,
            birth_date     TEXT,                      -- 'YYYY-MM-DD'
            phone          TEXT,
            address        TEXT,
            email          TEXT NOT NULL UNIQUE,      -- added: login
            password_hash  TEXT NOT NULL,             -- added: login
            marital_status TEXT,                      -- added: profile field
            created_at     TEXT NOT NULL              -- added: registration time
        );

        -- EMPLOYEES: ERD columns + password_hash (added, for login).
        CREATE TABLE IF NOT EXISTS employees (
            employee_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name     TEXT NOT NULL,
            role          TEXT NOT NULL,              -- e.g. 'admin', 'credit_officer'
            login         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL               -- added: login
        );

        -- EMPLOYMENT: one client can have many jobs (1-to-many).
        CREATE TABLE IF NOT EXISTS employment (
            employment_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id      INTEGER NOT NULL,
            employer_name  TEXT NOT NULL,
            position       TEXT,
            monthly_income REAL NOT NULL,             -- KZT
            FOREIGN KEY (client_id) REFERENCES clients (client_id) ON DELETE CASCADE
        );

        -- DOCUMENTS: uploaded files (passport etc.). We store the path and a
        -- SHA-256 hash of the file content (to detect if the file was changed).
        CREATE TABLE IF NOT EXISTS documents (
            document_id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id   INTEGER NOT NULL,
            doc_type    TEXT NOT NULL,                -- e.g. 'passport'
            file_path   TEXT NOT NULL,
            file_hash   TEXT NOT NULL,
            FOREIGN KEY (client_id) REFERENCES clients (client_id) ON DELETE CASCADE
        );

        -- LOAN_APPLICATIONS: employee_id is NULL until an employee reviews it.
        CREATE TABLE IF NOT EXISTS loan_applications (
            application_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id        INTEGER NOT NULL,
            employee_id      INTEGER,                 -- NULLABLE
            requested_amount REAL NOT NULL,           -- KZT
            term_months      INTEGER NOT NULL,
            purpose          TEXT,
            status           TEXT NOT NULL,           -- 'Approved' / 'Rejected' / 'Pending Admin Review'
            approval_type    TEXT NOT NULL,           -- 'Auto' / 'Manual'
            created_at       TEXT NOT NULL,
            FOREIGN KEY (client_id)   REFERENCES clients (client_id)     ON DELETE CASCADE,
            FOREIGN KEY (employee_id) REFERENCES employees (employee_id) ON DELETE SET NULL
        );

        -- CLIENT_CHECKS: results of automatic checks for each application.
        CREATE TABLE IF NOT EXISTS client_checks (
            check_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id INTEGER NOT NULL,
            check_type     TEXT NOT NULL,             -- IIN_FORMAT, AGE_18, INCOME_DTI, AMOUNT_LIMIT
            result_status  TEXT NOT NULL,             -- PASS / FAIL / MANUAL_REVIEW
            checked_at     TEXT NOT NULL,
            FOREIGN KEY (application_id) REFERENCES loan_applications (application_id) ON DELETE CASCADE
        );

        -- LOANS: application_id is UNIQUE -> one application becomes at most
        -- ONE loan (this is how a 1-to-1 relationship is made in SQL).
        CREATE TABLE IF NOT EXISTS loans (
            loan_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id  INTEGER NOT NULL UNIQUE,
            approved_amount REAL NOT NULL,
            interest_rate   REAL NOT NULL,            -- % per year
            issue_date      TEXT NOT NULL,
            FOREIGN KEY (application_id) REFERENCES loan_applications (application_id) ON DELETE CASCADE
        );

        -- REPAYMENTS: the monthly payment schedule of a loan.
        CREATE TABLE IF NOT EXISTS repayments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            loan_id    INTEGER NOT NULL,
            due_date   TEXT NOT NULL,
            amount_due REAL NOT NULL,
            status     TEXT NOT NULL DEFAULT 'Pending',  -- 'Pending' / 'Paid'
            FOREIGN KEY (loan_id) REFERENCES loans (loan_id) ON DELETE CASCADE
        );
    """)

    # Create the default employee only if this login does not exist yet.
    # The "?" is a placeholder: sqlite3 safely inserts the value for us.
    # This protects against SQL injection (never build SQL with + or f-strings!).
    existing = conn.execute(
        "SELECT employee_id FROM employees WHERE login = ?", (ADMIN_LOGIN,)
    ).fetchone()  # fetchone() -> one row, or None
    if existing is None:
        conn.execute(
            "INSERT INTO employees (full_name, role, login, password_hash) VALUES (?, ?, ?, ?)",
            ("Credit Officer (Admin)", "admin", ADMIN_LOGIN, hash_password(ADMIN_PASSWORD)),
        )

    # commit() saves the changes to the file. Without it, nothing is saved.
    conn.commit()
    conn.close()


def rows_to_dicts(rows):
    # sqlite3.Row -> normal dict, so FastAPI can turn it into JSON.
    return [dict(row) for row in rows]


# =============== CLIENTS ===============

# Columns we may show publicly (we never return password_hash!)
CLIENT_COLUMNS = "client_id, iin, full_name, birth_date, phone, address, email, marital_status, created_at"


def create_client(iin, full_name, birth_date, phone, address, email, password,
                  marital_status, created_at):
    conn = get_connection()
    cursor = conn.execute(
        """INSERT INTO clients (iin, full_name, birth_date, phone, address, email,
                                password_hash, marital_status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (iin, full_name, birth_date, phone, address, email,
         hash_password(password), marital_status, created_at),
    )
    conn.commit()
    new_id = cursor.lastrowid  # lastrowid = the id SQLite just gave to the new row
    conn.close()
    return new_id


def get_clients():
    conn = get_connection()
    rows = conn.execute(f"SELECT {CLIENT_COLUMNS} FROM clients ORDER BY client_id").fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_client(client_id):
    conn = get_connection()
    row = conn.execute(f"SELECT {CLIENT_COLUMNS} FROM clients WHERE client_id = ?", (client_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_client_login(email):
    # Used only for login: here we DO need password_hash.
    conn = get_connection()
    row = conn.execute("SELECT client_id, password_hash FROM clients WHERE email = ?", (email,)).fetchone()
    conn.close()
    return row


def delete_client(client_id):
    # Thanks to ON DELETE CASCADE, SQLite also deletes the client's employment,
    # documents, applications, their checks, loans and repayments.
    conn = get_connection()
    cursor = conn.execute("DELETE FROM clients WHERE client_id = ?", (client_id,))
    conn.commit()
    deleted = cursor.rowcount  # how many rows were deleted (0 or 1)
    conn.close()
    return deleted


def get_total_income(client_id):
    # SUM() adds up monthly_income of ALL jobs of this client.
    # COALESCE(x, 0) returns 0 when the client has no jobs (SUM gives NULL then).
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(monthly_income), 0) AS total FROM employment WHERE client_id = ?",
        (client_id,),
    ).fetchone()
    conn.close()
    return row["total"]


# =============== EMPLOYEES ===============

def get_employees():
    conn = get_connection()
    rows = conn.execute("SELECT employee_id, full_name, role, login FROM employees").fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_employee(employee_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT employee_id, full_name, role, login FROM employees WHERE employee_id = ?", (employee_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_employee_login(login):
    conn = get_connection()
    row = conn.execute("SELECT employee_id, password_hash FROM employees WHERE login = ?", (login,)).fetchone()
    conn.close()
    return row


# =============== EMPLOYMENT ===============

def create_employment(client_id, employer_name, position, monthly_income):
    # If client_id does not exist, SQLite raises sqlite3.IntegrityError
    # ("FOREIGN KEY constraint failed"). main.py catches it.
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO employment (client_id, employer_name, position, monthly_income) VALUES (?, ?, ?, ?)",
            (client_id, employer_name, position, monthly_income),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()  # "finally" runs even if there was an error


def get_employment(client_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM employment WHERE client_id = ?", (client_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# =============== DOCUMENTS ===============

def create_document(client_id, doc_type, file_path, file_hash):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO documents (client_id, doc_type, file_path, file_hash) VALUES (?, ?, ?, ?)",
            (client_id, doc_type, file_path, file_hash),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_documents(client_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM documents WHERE client_id = ?", (client_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# =============== LOAN APPLICATIONS ===============

def create_application(client_id, requested_amount, term_months, purpose, status,
                       approval_type, created_at):
    conn = get_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO loan_applications (client_id, requested_amount, term_months, purpose,
                                              status, approval_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, requested_amount, term_months, purpose, status, approval_type, created_at),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


# JOIN connects tables: for every application we also take the client's data,
# the total income (a small sub-query) and the reviewer's name.
# LEFT JOIN keeps the application even when employee_id is NULL (not reviewed yet).
APPLICATION_SELECT = """
    SELECT a.*, c.full_name, c.iin,
           (SELECT COALESCE(SUM(e.monthly_income), 0) FROM employment e
             WHERE e.client_id = a.client_id) AS monthly_income,
           emp.full_name AS reviewed_by
    FROM loan_applications a
    JOIN clients c        ON c.client_id = a.client_id
    LEFT JOIN employees emp ON emp.employee_id = a.employee_id
"""


def get_applications(client_id=None):
    conn = get_connection()
    # The if-condition decides whether we filter by client or return everything.
    if client_id is None:
        rows = conn.execute(APPLICATION_SELECT + " ORDER BY a.application_id DESC").fetchall()
    else:
        rows = conn.execute(
            APPLICATION_SELECT + " WHERE a.client_id = ? ORDER BY a.application_id DESC", (client_id,)
        ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_application(application_id):
    conn = get_connection()
    row = conn.execute(APPLICATION_SELECT + " WHERE a.application_id = ?", (application_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_application_decision(application_id, status, employee_id):
    # UPDATE changes an existing row. WHERE says which row.
    conn = get_connection()
    conn.execute(
        "UPDATE loan_applications SET status = ?, employee_id = ? WHERE application_id = ?",
        (status, employee_id, application_id),
    )
    conn.commit()
    conn.close()


# =============== CLIENT CHECKS ===============

def create_checks(application_id, checks, checked_at):
    # checks is a list of pairs like [("IIN_FORMAT", "PASS"), ("AGE_18", "PASS")]
    conn = get_connection()
    for check_type, result_status in checks:
        conn.execute(
            "INSERT INTO client_checks (application_id, check_type, result_status, checked_at) VALUES (?, ?, ?, ?)",
            (application_id, check_type, result_status, checked_at),
        )
    conn.commit()
    conn.close()


def get_checks(application_id):
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM client_checks WHERE application_id = ? ORDER BY check_id", (application_id,)
    ).fetchall()
    conn.close()
    return rows_to_dicts(rows)


# =============== LOANS + REPAYMENTS ===============

def create_loan_with_schedule(application_id, approved_amount, interest_rate, issue_date, schedule):
    # A loan and its schedule are saved in ONE transaction:
    # either everything is saved (commit) or nothing (rollback).
    # schedule = list of (due_date, amount_due)
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO loans (application_id, approved_amount, interest_rate, issue_date) VALUES (?, ?, ?, ?)",
            (application_id, approved_amount, interest_rate, issue_date),
        )
        loan_id = cursor.lastrowid
        for due_date, amount_due in schedule:
            conn.execute(
                "INSERT INTO repayments (loan_id, due_date, amount_due, status) VALUES (?, ?, ?, 'Pending')",
                (loan_id, due_date, amount_due),
            )
        conn.commit()
        return loan_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


LOAN_SELECT = """
    SELECT l.*, a.client_id, a.term_months, c.full_name,
           (SELECT COUNT(*) FROM repayments r WHERE r.loan_id = l.loan_id AND r.status = 'Paid') AS paid_count,
           (SELECT COUNT(*) FROM repayments r WHERE r.loan_id = l.loan_id) AS payments_count
    FROM loans l
    JOIN loan_applications a ON a.application_id = l.application_id
    JOIN clients c ON c.client_id = a.client_id
"""


def get_loans(client_id=None):
    conn = get_connection()
    if client_id is None:
        rows = conn.execute(LOAN_SELECT + " ORDER BY l.loan_id DESC").fetchall()
    else:
        rows = conn.execute(LOAN_SELECT + " WHERE a.client_id = ? ORDER BY l.loan_id DESC", (client_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_loan(loan_id):
    conn = get_connection()
    row = conn.execute(LOAN_SELECT + " WHERE l.loan_id = ?", (loan_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_repayments(loan_id):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM repayments WHERE loan_id = ? ORDER BY due_date, payment_id", (loan_id,)).fetchall()
    conn.close()
    return rows_to_dicts(rows)


def get_repayment(payment_id):
    conn = get_connection()
    row = conn.execute("SELECT * FROM repayments WHERE payment_id = ?", (payment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_repayment_paid(payment_id):
    conn = get_connection()
    conn.execute("UPDATE repayments SET status = 'Paid' WHERE payment_id = ?", (payment_id,))
    conn.commit()
    conn.close()
