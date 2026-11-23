# repayment_rules/storage.py
# ------------------------------------------------------------
# Database part of the repayment rules (same mfo.db as the rest of the app).
#
#   repayment_penalties - the penalty that was actually paid for an overdue repayment (BR-3)
#   early_payoffs       - one row per loan closed early: how much principal, how, by whom (BR-4)
#   early_payoff_items  - which scheduled repayments were closed by that early payoff
#
# Created on first use (CREATE TABLE IF NOT EXISTS), like payments/storage.py,
# so database.py stays untouched and an old mfo.db is upgraded automatically.
# ------------------------------------------------------------
from datetime import datetime, timezone

import database

SCHEMA = """
CREATE TABLE IF NOT EXISTS repayment_penalties (
    payment_id     INTEGER PRIMARY KEY,           -- one penalty per repayment
    days_overdue   INTEGER NOT NULL CHECK (days_overdue > 0),
    penalty_amount REAL NOT NULL CHECK (penalty_amount > 0),
    paid_at        TEXT NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES repayments (payment_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS early_payoffs (
    payoff_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id         INTEGER NOT NULL UNIQUE,      -- a loan can be closed early only once
    principal_paid  REAL NOT NULL CHECK (principal_paid >= 0),
    scheduled_left  REAL NOT NULL,                -- what the schedule still had (to show the saving)
    method_detail   TEXT NOT NULL,                -- "Visa •••• 4242", "PayPal a***@mail.com", "Manual ..."
    paid_by         TEXT NOT NULL,                -- client / employee
    paid_at         TEXT NOT NULL,
    FOREIGN KEY (loan_id) REFERENCES loans (loan_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS early_payoff_items (
    payment_id INTEGER PRIMARY KEY,
    payoff_id  INTEGER NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES repayments (payment_id) ON DELETE CASCADE,
    FOREIGN KEY (payoff_id) REFERENCES early_payoffs (payoff_id) ON DELETE CASCADE
);
"""


def _connect():
    conn = database.get_connection()
    conn.executescript(SCHEMA)
    return conn


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def record_penalty(payment_id, days_overdue, penalty_amount):
    conn = _connect()
    conn.execute("INSERT OR REPLACE INTO repayment_penalties (payment_id, days_overdue, penalty_amount, paid_at) "
                 "VALUES (?, ?, ?, ?)", (payment_id, days_overdue, penalty_amount, _now()))
    conn.commit()
    conn.close()


def penalties_for_loan(loan_id):
    """{payment_id: {"days_overdue": .., "penalty_amount": ..}} of the paid penalties of a loan."""
    conn = _connect()
    rows = conn.execute("""SELECT p.payment_id, p.days_overdue, p.penalty_amount FROM repayment_penalties p
                           JOIN repayments r ON r.payment_id = p.payment_id WHERE r.loan_id = ?""",
                        (loan_id,)).fetchall()
    conn.close()
    return {r["payment_id"]: dict(r) for r in rows}


def close_loan_early(loan_id, payment_ids, principal_paid, scheduled_left, method_detail, paid_by):
    """BR-4 in ONE transaction: save the payoff and mark all the left repayments Paid.
    Either everything is saved or nothing (rollback)."""
    conn = _connect()
    try:
        cursor = conn.execute("""INSERT INTO early_payoffs
                                 (loan_id, principal_paid, scheduled_left, method_detail, paid_by, paid_at)
                                 VALUES (?, ?, ?, ?, ?, ?)""",
                              (loan_id, principal_paid, scheduled_left, method_detail, paid_by, _now()))
        payoff_id = cursor.lastrowid
        for payment_id in payment_ids:
            updated = conn.execute("UPDATE repayments SET status = 'Paid' WHERE payment_id = ? AND loan_id = ? "
                                   "AND status != 'Paid'", (payment_id, loan_id)).rowcount
            if updated != 1:
                raise ValueError("The schedule changed while paying, try again")
            conn.execute("INSERT INTO early_payoff_items (payment_id, payoff_id) VALUES (?, ?)",
                         (payment_id, payoff_id))
        conn.commit()
        return payoff_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_payoff(loan_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM early_payoffs WHERE loan_id = ?", (loan_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def closed_early_for_loan(loan_id):
    """Set of payment_ids of this loan that were closed by an early payoff."""
    conn = _connect()
    rows = conn.execute("""SELECT i.payment_id FROM early_payoff_items i
                           JOIN early_payoffs e ON e.payoff_id = i.payoff_id WHERE e.loan_id = ?""",
                        (loan_id,)).fetchall()
    conn.close()
    return {r["payment_id"] for r in rows}
