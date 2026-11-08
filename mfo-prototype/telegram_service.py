# telegram_service.py
# Pairing codes, one-to-one Telegram links, and ownership-checked reads.
# The plaintext code is returned only to the local operator CLI.
# The database stores SHA-256(normalized code). Queries stay parameterized.
import hashlib
import logging
import os
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

import database

log = logging.getLogger("mfo.telegram")

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LENGTH = 8
DEFAULT_CODE_TTL_SECONDS = 600
MAX_FAILED_ATTEMPTS = 5
GLOBAL_MAX_FAILED_ATTEMPTS = 100
ATTEMPT_WINDOW_SECONDS = 15 * 60
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

class TelegramServiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _Rollback(Exception):
    def __init__(self, result):
        super().__init__(result.reason if hasattr(result, "reason") else "rollback")
        self.result = result


@dataclass(frozen=True)
class IssuedCode:
    client_id: int
    full_name: str
    code: str
    expires_at: str


@dataclass(frozen=True)
class RedeemResult:
    ok: bool
    reason: str
    client_id: int | None = None


@dataclass(frozen=True)
class UnlinkResult:
    client_found: bool
    link_removed: bool
    codes_revoked: int


def code_ttl_seconds():
    raw = os.environ.get("MFO_TELEGRAM_CODE_TTL_SECONDS", "").strip()
    if not raw:
        return DEFAULT_CODE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_CODE_TTL_SECONDS
    if value < 30 or value > 86400:
        return DEFAULT_CODE_TTL_SECONDS
    return value


def normalize_pairing_code(raw):
    if not isinstance(raw, str) or len(raw) > 40:
        return None
    compact = "".join(ch for ch in raw.upper() if ch not in " \t\r\n-_")
    if len(compact) != CODE_LENGTH or any(ch not in ALPHABET for ch in compact):
        return None
    return compact


def hash_pairing_code(raw):
    normalized = normalize_pairing_code(raw)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_pairing_code(raw):
    """Return the first token that looks like a pairing code, or None."""
    if not isinstance(raw, str):
        return None
    if normalize_pairing_code(raw):
        return raw.strip()
    for token in raw.split():
        if normalize_pairing_code(token):
            return token
    return None


def issue_pairing_code(client_id, now=None, ttl_seconds=None):
    """Create one short-lived code for a known, not-yet-linked client.

    Older unused codes of this client are revoked in the same transaction.
    Raises TelegramServiceError('client_not_found' | 'already_linked').
    """
    if not _valid_id(client_id):
        raise TelegramServiceError("client_not_found")
    moment = _moment(now)
    ttl = code_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0:
        raise TelegramServiceError("bad_ttl")
    expires_at = _format_time(moment + timedelta(seconds=ttl))
    now_text = _format_time(moment)

    def op(conn):
        client = conn.execute(
            "SELECT client_id, full_name FROM clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if client is None:
            raise TelegramServiceError("client_not_found")
        linked = conn.execute(
            "SELECT link_id FROM telegram_links WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if linked is not None:
            raise TelegramServiceError("already_linked")
        conn.execute(
            """UPDATE telegram_pairing_codes
                  SET consumed_at = ?
                WHERE client_id = ? AND consumed_at IS NULL""",
            (now_text, client_id),
        )
        for _ in range(5):
            code = _generate_code()
            code_hash = hash_pairing_code(code)
            try:
                conn.execute(
                    """INSERT INTO telegram_pairing_codes
                       (client_id, code_hash, created_at, expires_at, consumed_at)
                       VALUES (?, ?, ?, ?, NULL)""",
                    (client_id, code_hash, now_text, expires_at),
                )
            except sqlite3.IntegrityError:
                continue
            log.info("pairing code issued client_id=%s expires_at=%s", client_id, expires_at)
            return IssuedCode(client["client_id"], client["full_name"], code, expires_at)
        raise TelegramServiceError("code_generation_failed")

    return _transaction(op)


def redeem_pairing_code(telegram_user_id, raw_code, now=None):
    """Consume a code and bind this Telegram user, or deny.

    Reasons: ok, malformed, invalid, expired, replayed, locked,
    user_already_linked, client_already_linked, race.
    The plaintext code is never written to the log.
    """
    moment = _moment(now)
    if not _valid_user_id(telegram_user_id):
        log.info("telegram link denied user_id=invalid reason=malformed")
        return RedeemResult(False, "malformed")
    log.info("telegram link attempt user_id=%s", telegram_user_id)

    def op(conn):
        _prune_attempts(conn, moment)
        if _linked_client_id(conn, telegram_user_id) is not None:
            return RedeemResult(False, "user_already_linked")
        if _attempt_count(conn, moment, telegram_user_id) >= MAX_FAILED_ATTEMPTS:
            return RedeemResult(False, "locked")
        if _attempt_count(conn, moment, None) >= GLOBAL_MAX_FAILED_ATTEMPTS:
            return RedeemResult(False, "locked")

        code_hash = hash_pairing_code(raw_code) if isinstance(raw_code, str) else None
        if code_hash is None:
            _record_failure(conn, telegram_user_id, moment)
            return RedeemResult(False, "malformed")

        row = conn.execute(
            """SELECT code_id, client_id, expires_at, consumed_at
                 FROM telegram_pairing_codes
                WHERE code_hash = ?""",
            (code_hash,),
        ).fetchone()
        if row is None:
            _record_failure(conn, telegram_user_id, moment)
            return RedeemResult(False, "invalid")
        if row["consumed_at"] is not None:
            _record_failure(conn, telegram_user_id, moment)
            return RedeemResult(False, "replayed")
        if row["expires_at"] <= _format_time(moment):
            _record_failure(conn, telegram_user_id, moment)
            return RedeemResult(False, "expired")

        owner = conn.execute(
            "SELECT telegram_user_id FROM telegram_links WHERE client_id = ?",
            (row["client_id"],),
        ).fetchone()
        if owner is not None:
            # The client already has a Telegram account. Burn the leftover code
            # so it cannot be used after a later unlink.
            conn.execute(
                """UPDATE telegram_pairing_codes
                      SET consumed_at = ?
                    WHERE code_id = ? AND consumed_at IS NULL""",
                (_format_time(moment), row["code_id"]),
            )
            return RedeemResult(False, "client_already_linked")

        updated = conn.execute(
            """UPDATE telegram_pairing_codes
                  SET consumed_at = ?
                WHERE code_id = ? AND consumed_at IS NULL AND expires_at > ?""",
            (_format_time(moment), row["code_id"], _format_time(moment)),
        )
        if updated.rowcount != 1:
            raise _Rollback(RedeemResult(False, "race"))
        try:
            conn.execute(
                """INSERT INTO telegram_links (client_id, telegram_user_id, linked_at)
                   VALUES (?, ?, ?)""",
                (row["client_id"], telegram_user_id, _format_time(moment)),
            )
        except sqlite3.IntegrityError:
            raise _Rollback(RedeemResult(False, "race"))
        conn.execute(
            "DELETE FROM telegram_link_attempts WHERE telegram_user_id = ?",
            (telegram_user_id,),
        )
        return RedeemResult(True, "ok", client_id=row["client_id"])

    result = _transaction(op)
    if result.ok:
        log.info("telegram link ok user_id=%s client_id=%s", telegram_user_id, result.client_id)
    else:
        log.info("telegram link denied user_id=%s reason=%s", telegram_user_id, result.reason)
    return result


def unlink_client(client_id, now=None):
    """Operator unlink: drop the link and revoke unused codes for this client."""
    if not _valid_id(client_id):
        return UnlinkResult(False, False, 0)
    moment = _moment(now)

    def op(conn):
        found = conn.execute(
            "SELECT client_id FROM clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if found is None:
            return UnlinkResult(False, False, 0)
        removed = conn.execute(
            "DELETE FROM telegram_links WHERE client_id = ?",
            (client_id,),
        )
        revoked = conn.execute(
            """UPDATE telegram_pairing_codes
                  SET consumed_at = ?
                WHERE client_id = ? AND consumed_at IS NULL""",
            (_format_time(moment), client_id),
        )
        return UnlinkResult(True, removed.rowcount > 0, revoked.rowcount)

    result = _transaction(op)
    log.info(
        "telegram unlink client_id=%s link_removed=%s codes_revoked=%s",
        client_id, result.link_removed, result.codes_revoked,
    )
    return result


def unlink_telegram_user(telegram_user_id, now=None):
    """Bot unlink: only the link of this numeric Telegram user id."""
    if not _valid_user_id(telegram_user_id):
        return UnlinkResult(False, False, 0)
    moment = _moment(now)

    def op(conn):
        row = conn.execute(
            "SELECT client_id FROM telegram_links WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if row is None:
            return UnlinkResult(True, False, 0)
        client_id = row["client_id"]
        removed = conn.execute(
            "DELETE FROM telegram_links WHERE telegram_user_id = ? AND client_id = ?",
            (telegram_user_id, client_id),
        )
        revoked = conn.execute(
            """UPDATE telegram_pairing_codes
                  SET consumed_at = ?
                WHERE client_id = ? AND consumed_at IS NULL""",
            (_format_time(moment), client_id),
        )
        return UnlinkResult(True, removed.rowcount > 0, revoked.rowcount)

    result = _transaction(op)
    log.info(
        "telegram unlink user_id=%s link_removed=%s codes_revoked=%s",
        telegram_user_id, result.link_removed, result.codes_revoked,
    )
    return result


def client_link_status(client_id, now=None):
    """Operator view. No pairing code, IIN, phone, email, address, or password."""
    if not _valid_id(client_id):
        return None
    moment = _moment(now)
    conn = _connect()
    try:
        client = conn.execute(
            "SELECT client_id, full_name FROM clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        if client is None:
            return None
        link = conn.execute(
            """SELECT telegram_user_id, linked_at
                 FROM telegram_links
                WHERE client_id = ?""",
            (client_id,),
        ).fetchone()
        active = conn.execute(
            """SELECT expires_at
                 FROM telegram_pairing_codes
                WHERE client_id = ? AND consumed_at IS NULL AND expires_at > ?
                ORDER BY code_id DESC
                LIMIT 1""",
            (client_id, _format_time(moment)),
        ).fetchone()
        return {
            "client_id": client["client_id"],
            "full_name": client["full_name"],
            "telegram_user_id": None if link is None else link["telegram_user_id"],
            "linked_at": None if link is None else link["linked_at"],
            "active_code_expires_at": None if active is None else active["expires_at"],
        }
    finally:
        conn.close()


def get_own_profile(telegram_user_id):
    """Name and link time of the bound client. No IIN, phone, email, or address."""
    if not _valid_user_id(telegram_user_id):
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT c.client_id, c.full_name, t.linked_at
                 FROM telegram_links t
                 JOIN clients c ON c.client_id = t.client_id
                WHERE t.telegram_user_id = ?""",
            (telegram_user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_own_applications(telegram_user_id):
    """None when the user is not linked. Otherwise only that client's rows."""
    client_id = _client_id_for_user(telegram_user_id)
    if client_id is None:
        return None
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT a.application_id, a.requested_amount, a.term_months, a.purpose,
                      a.status, a.approval_type, a.created_at, l.loan_id
                 FROM loan_applications a
                 LEFT JOIN loans l ON l.application_id = a.application_id
                WHERE a.client_id = ?
                ORDER BY a.application_id DESC""",
            (client_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_own_application(telegram_user_id, application_id):
    """One application plus its checks, only if this Telegram user owns it."""
    if not _valid_user_id(telegram_user_id) or not _valid_id(application_id):
        return None
    conn = _connect()
    try:
        client_id = _linked_client_id(conn, telegram_user_id)
        if client_id is None:
            return None
        row = conn.execute(
            """SELECT a.application_id, a.requested_amount, a.term_months, a.purpose,
                      a.status, a.approval_type, a.created_at, l.loan_id
                 FROM loan_applications a
                 LEFT JOIN loans l ON l.application_id = a.application_id
                WHERE a.application_id = ? AND a.client_id = ?""",
            (application_id, client_id),
        ).fetchone()
        if row is None:
            return None
        checks = conn.execute(
            """SELECT check_type, result_status
                 FROM client_checks
                WHERE application_id = ?
                ORDER BY check_id""",
            (application_id,),
        ).fetchall()
        data = dict(row)
        data["checks"] = [dict(item) for item in checks]
        return data
    finally:
        conn.close()


def list_own_loan_ids(telegram_user_id):
    client_id = _client_id_for_user(telegram_user_id)
    if client_id is None:
        return None
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT l.loan_id
                 FROM loans l
                 JOIN loan_applications a ON a.application_id = l.application_id
                WHERE a.client_id = ?
                ORDER BY l.loan_id DESC""",
            (client_id,),
        ).fetchall()
        return [row["loan_id"] for row in rows]
    finally:
        conn.close()


def normalize_name_query(raw):
    """Casefolded name fragment. None when the text must not be searched.

    Digits runs, e-mail, and very short or very long text are rejected so a
    staff member cannot look clients up by IIN or phone. Percent and underscore
    stay ordinary characters; they are never passed into SQL LIKE.
    """
    if not isinstance(raw, str):
        return None
    folded = " ".join(raw.casefold().split())
    if len(folded) < 2 or len(folded) > 80:
        return None
    if "@" in folded or "://" in folded:
        return None
    digits = 0
    for char in folded:
        if char.isdigit():
            digits += 1
            if digits >= 6:
                return None
        else:
            digits = 0
    return folded


def search_clients_by_name(query, page=1, page_size=5):
    """Partial, case-insensitive match on full_name only.

    Returns a dict with total/items or None when the query is rejected.
    Items contain client_id and full_name and nothing else.
    """
    folded = normalize_name_query(query)
    if folded is None:
        return None
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > 20:
        page = 1
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 10:
        page_size = 5
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT client_id, full_name FROM clients ORDER BY client_id"
        ).fetchall()
    finally:
        conn.close()
    matched = []
    for row in rows:
        name = " ".join(str(row["full_name"]).casefold().split())
        if folded in name:
            matched.append({"client_id": row["client_id"], "full_name": row["full_name"]})
    start = (page - 1) * page_size
    return {
        "query": folded,
        "page": page,
        "page_size": page_size,
        "total": len(matched),
        "items": matched[start:start + page_size],
    }


def client_name(client_id):
    """client_id and full_name, or None. No contact columns."""
    if not _valid_id(client_id):
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT client_id, full_name FROM clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_client_applications(client_id):
    """Read-only applications of one client. None if the client does not exist."""
    if client_name(client_id) is None:
        return None
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT a.application_id, a.requested_amount, a.term_months, a.purpose,
                      a.status, a.approval_type, a.created_at, l.loan_id
                 FROM loan_applications a
                 LEFT JOIN loans l ON l.application_id = a.application_id
                WHERE a.client_id = ?
                ORDER BY a.application_id DESC""",
            (client_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_client_application(client_id, application_id):
    """One application only when it belongs to this client."""
    if not _valid_id(client_id) or not _valid_id(application_id):
        return None
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT a.application_id, a.requested_amount, a.term_months, a.purpose,
                      a.status, a.approval_type, a.created_at, l.loan_id
                 FROM loan_applications a
                 LEFT JOIN loans l ON l.application_id = a.application_id
                WHERE a.application_id = ? AND a.client_id = ?""",
            (application_id, client_id),
        ).fetchone()
        if row is None:
            return None
        checks = conn.execute(
            """SELECT check_type, result_status
                 FROM client_checks
                WHERE application_id = ?
                ORDER BY check_id""",
            (application_id,),
        ).fetchall()
        data = dict(row)
        data["checks"] = [dict(item) for item in checks]
        return data
    finally:
        conn.close()


def get_client_schedule(client_id, loan_id):
    """Read-only schedule only when the loan belongs to this client."""
    if not _valid_id(client_id) or not _valid_id(loan_id):
        return None
    conn = _connect()
    try:
        loan = conn.execute(
            """SELECT l.loan_id, l.application_id, l.approved_amount, l.interest_rate,
                      l.issue_date, a.term_months
                 FROM loans l
                 JOIN loan_applications a ON a.application_id = l.application_id
                WHERE l.loan_id = ? AND a.client_id = ?""",
            (loan_id, client_id),
        ).fetchone()
        if loan is None:
            return None
        payments = conn.execute(
            """SELECT payment_id, due_date, amount_due, status
                 FROM repayments
                WHERE loan_id = ?
                ORDER BY due_date, payment_id""",
            (loan["loan_id"],),
        ).fetchall()
        data = dict(loan)
        data["payments"] = [dict(item) for item in payments]
        return data
    finally:
        conn.close()


def get_own_schedule(telegram_user_id, loan_id):
    """Repayment schedule only when the loan belongs to the linked client."""
    if not _valid_user_id(telegram_user_id) or not _valid_id(loan_id):
        return None
    conn = _connect()
    try:
        client_id = _linked_client_id(conn, telegram_user_id)
        if client_id is None:
            return None
        loan = conn.execute(
            """SELECT l.loan_id, l.application_id, l.approved_amount, l.interest_rate,
                      l.issue_date, a.term_months
                 FROM loans l
                 JOIN loan_applications a ON a.application_id = l.application_id
                WHERE l.loan_id = ? AND a.client_id = ?""",
            (loan_id, client_id),
        ).fetchone()
        if loan is None:
            return None
        payments = conn.execute(
            """SELECT payment_id, due_date, amount_due, status
                 FROM repayments
                WHERE loan_id = ?
                ORDER BY due_date, payment_id""",
            (loan["loan_id"],),
        ).fetchall()
        data = dict(loan)
        data["payments"] = [dict(item) for item in payments]
        return data
    finally:
        conn.close()


def _generate_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
    return raw[:4] + "-" + raw[4:]


def _connect():
    conn = sqlite3.connect(database.DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _transaction(fn):
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(conn)
        except _Rollback as signal:
            conn.execute("ROLLBACK")
            return signal.result
        conn.execute("COMMIT")
        return result
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _moment(now):
    if now is None:
        return datetime.now()
    if not isinstance(now, datetime):
        raise TelegramServiceError("bad_clock")
    return now


def _format_time(value):
    return value.strftime(TIME_FORMAT)


def _valid_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_user_id(value):
    return _valid_id(value)


def _linked_client_id(conn, telegram_user_id):
    row = conn.execute(
        "SELECT client_id FROM telegram_links WHERE telegram_user_id = ?",
        (telegram_user_id,),
    ).fetchone()
    return None if row is None else row["client_id"]


def _client_id_for_user(telegram_user_id):
    if not _valid_user_id(telegram_user_id):
        return None
    conn = _connect()
    try:
        return _linked_client_id(conn, telegram_user_id)
    finally:
        conn.close()


def _attempt_count(conn, moment, telegram_user_id):
    window_start = _format_time(moment - timedelta(seconds=ATTEMPT_WINDOW_SECONDS))
    if telegram_user_id is None:
        row = conn.execute(
            """SELECT COUNT(*) AS n
                 FROM telegram_link_attempts
                WHERE success = 0 AND attempted_at > ?""",
            (window_start,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COUNT(*) AS n
                 FROM telegram_link_attempts
                WHERE telegram_user_id = ? AND success = 0 AND attempted_at > ?""",
            (telegram_user_id, window_start),
        ).fetchone()
    return row["n"]


def _record_failure(conn, telegram_user_id, moment):
    conn.execute(
        """INSERT INTO telegram_link_attempts (telegram_user_id, attempted_at, success)
           VALUES (?, ?, 0)""",
        (telegram_user_id, _format_time(moment)),
    )


def _prune_attempts(conn, moment):
    cutoff = _format_time(moment - timedelta(seconds=ATTEMPT_WINDOW_SECONDS * 2))
    conn.execute(
        "DELETE FROM telegram_link_attempts WHERE attempted_at < ?",
        (cutoff,),
    )
