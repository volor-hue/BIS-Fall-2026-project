# payments/storage.py
# ------------------------------------------------------------
# Database part of the payment methods (same mfo.db as the rest of the app).
#
#   payment_cards        - saved bank cards of a client: holder, brand, last 4 digits, expiry,
#                          is_default. NEVER the full card number and NEVER the CVV.
#   kaspi_qr_sessions    - a shown Kaspi QR for one repayment: one-time, expires in a few minutes.
#                          Only the SHA-256 of its random reference is stored.
#   payment_transactions - which method paid which repayment (card •••• 4242 / PayPal / Kaspi QR).
#
# The tables are created on first use (CREATE TABLE IF NOT EXISTS), like face_auth/storage.py,
# so database.py and the teammates' tables stay untouched and an old mfo.db is upgraded automatically.
# ------------------------------------------------------------
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import database

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_cards (
    card_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id  INTEGER NOT NULL,
    holder     TEXT NOT NULL,
    brand      TEXT NOT NULL,                 -- Visa / Mastercard / Mir / Amex / UnionPay / Card
    last4      TEXT NOT NULL,
    exp_month  INTEGER NOT NULL,
    exp_year   INTEGER NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,    -- 1 = preselected in the payment window
    created_at TEXT NOT NULL,
    FOREIGN KEY (client_id) REFERENCES clients (client_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS kaspi_qr_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    qr_hash    TEXT NOT NULL UNIQUE,
    payment_id INTEGER NOT NULL,
    client_id  INTEGER NOT NULL,
    amount     REAL NOT NULL,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES repayments (payment_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS payment_transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id    INTEGER NOT NULL,
    client_id     INTEGER NOT NULL,
    method        TEXT NOT NULL,              -- card / paypal / kaspi_qr / manual
    method_detail TEXT NOT NULL,              -- "Visa •••• 4242", "PayPal a***@mail.com", "Kaspi QR"
    amount        REAL NOT NULL,
    paid_by       TEXT NOT NULL,              -- client / employee
    paid_at       TEXT NOT NULL,
    FOREIGN KEY (payment_id) REFERENCES repayments (payment_id) ON DELETE CASCADE
);
"""

CARD_FIELDS = "card_id, holder, brand, last4, exp_month, exp_year, is_default, created_at"


def _connect():
    conn = database.get_connection()
    conn.executescript(SCHEMA)
    return conn


def _utc(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def _now():
    return _utc(datetime.now(timezone.utc))


def sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _card(row):
    card = dict(row)
    card["is_default"] = bool(card["is_default"])
    card["expiry"] = f"{card['exp_month']:02d}/{card['exp_year'] % 100:02d}"
    card["title"] = f"{card['brand']} •••• {card['last4']}"
    return card


# =============== CARDS ===============

def list_cards(client_id):
    conn = _connect()
    rows = conn.execute(f"SELECT {CARD_FIELDS} FROM payment_cards WHERE client_id = ? "
                        "ORDER BY is_default DESC, card_id", (client_id,)).fetchall()
    conn.close()
    return [_card(r) for r in rows]


def get_card(client_id, card_id):
    conn = _connect()
    row = conn.execute(f"SELECT {CARD_FIELDS} FROM payment_cards WHERE client_id = ? AND card_id = ?",
                       (client_id, card_id)).fetchone()
    conn.close()
    return _card(row) if row else None


def get_default_card(client_id):
    conn = _connect()
    row = conn.execute(f"SELECT {CARD_FIELDS} FROM payment_cards WHERE client_id = ? AND is_default = 1",
                       (client_id,)).fetchone()
    conn.close()
    return _card(row) if row else None


def find_same_card(client_id, card):
    conn = _connect()
    row = conn.execute("SELECT card_id FROM payment_cards WHERE client_id = ? AND brand = ? AND last4 = ? "
                       "AND exp_month = ? AND exp_year = ?",
                       (client_id, card["brand"], card["last4"], card["exp_month"], card["exp_year"])).fetchone()
    conn.close()
    return row is not None


def add_card(client_id, card, make_default=False):
    """Save a checked card (see cards.validate_card). The FIRST card becomes the default one."""
    conn = _connect()
    has_cards = conn.execute("SELECT COUNT(*) FROM payment_cards WHERE client_id = ?",
                             (client_id,)).fetchone()[0] > 0
    is_default = make_default or not has_cards
    if is_default:
        conn.execute("UPDATE payment_cards SET is_default = 0 WHERE client_id = ?", (client_id,))
    cursor = conn.execute(
        """INSERT INTO payment_cards (client_id, holder, brand, last4, exp_month, exp_year, is_default, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, card["holder"], card["brand"], card["last4"], card["exp_month"], card["exp_year"],
         1 if is_default else 0, _now()))
    conn.commit()
    card_id = cursor.lastrowid
    conn.close()
    return get_card(client_id, card_id)


def set_default(client_id, card_id):
    conn = _connect()
    exists = conn.execute("SELECT 1 FROM payment_cards WHERE client_id = ? AND card_id = ?",
                          (client_id, card_id)).fetchone()
    if exists:
        conn.execute("UPDATE payment_cards SET is_default = (card_id = ?) WHERE client_id = ?",
                     (card_id, client_id))
        conn.commit()
    conn.close()
    return bool(exists)


def delete_card(client_id, card_id):
    """Delete a card. If it was the default one, the oldest remaining card becomes the default."""
    conn = _connect()
    cursor = conn.execute("DELETE FROM payment_cards WHERE client_id = ? AND card_id = ?", (client_id, card_id))
    deleted = cursor.rowcount == 1
    if deleted and not conn.execute("SELECT 1 FROM payment_cards WHERE client_id = ? AND is_default = 1",
                                    (client_id,)).fetchone():
        conn.execute("""UPDATE payment_cards SET is_default = 1 WHERE card_id =
                        (SELECT MIN(card_id) FROM payment_cards WHERE client_id = ?)""", (client_id,))
    conn.commit()
    conn.close()
    return deleted


# =============== KASPI QR ===============

def create_qr_session(payment_id, client_id, amount, minutes):
    """Returns a random reference that goes into the QR (only its hash is stored)."""
    ref = secrets.token_urlsafe(12)
    now = datetime.now(timezone.utc)
    conn = _connect()
    conn.execute("DELETE FROM kaspi_qr_sessions WHERE expires_at < ? OR used_at IS NOT NULL", (_utc(now),))
    conn.execute("""INSERT INTO kaspi_qr_sessions (qr_hash, payment_id, client_id, amount, expires_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                 (sha256(ref), payment_id, client_id, amount, _utc(now + timedelta(minutes=minutes)), _utc(now)))
    conn.commit()
    conn.close()
    return ref


def consume_qr_session(ref, payment_id, client_id):
    """True only for a fresh, unused QR of THIS repayment and THIS client (one-time)."""
    now = _now()
    conn = _connect()
    cursor = conn.execute("""UPDATE kaspi_qr_sessions SET used_at = ?
                             WHERE qr_hash = ? AND payment_id = ? AND client_id = ?
                               AND used_at IS NULL AND expires_at > ?""",
                          (now, sha256(str(ref or "")), payment_id, client_id, now))
    conn.commit()
    ok = cursor.rowcount == 1
    conn.close()
    return ok


# =============== HISTORY ===============

def record_transaction(payment_id, client_id, method, detail, amount, paid_by):
    conn = _connect()
    conn.execute("""INSERT INTO payment_transactions
                    (payment_id, client_id, method, method_detail, amount, paid_by, paid_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                 (payment_id, client_id, method, detail, amount, paid_by, _now()))
    conn.commit()
    conn.close()


def methods_for_loan(loan_id):
    """{payment_id: "Visa •••• 4242"} for the repayment schedule table."""
    conn = _connect()
    rows = conn.execute("""SELECT t.payment_id, t.method_detail FROM payment_transactions t
                           JOIN repayments r ON r.payment_id = t.payment_id
                           WHERE r.loan_id = ? ORDER BY t.id""", (loan_id,)).fetchall()
    conn.close()
    return {r["payment_id"]: r["method_detail"] for r in rows}
