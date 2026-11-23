# app/payments/cards.py
# ------------------------------------------------------------
# Checking bank card details BEFORE saving (pure functions, easy to test).
# The full card number and the CVV are only checked here and then forgotten:
# the database keeps holder, brand, last 4 digits and the expiry date (see storage.py).
# ------------------------------------------------------------
import re
from datetime import date

HOLDER_RE = re.compile(r"^[A-Za-zА-Яа-яЁёҚқҒғҮүҰұӘәІіҢңӨөҺһ][A-Za-zА-Яа-яЁёҚқҒғҮүҰұӘәІіҢңӨөҺһ .'\-]{1,59}$")
EXPIRY_RE = re.compile(r"^\s*(\d{1,2})\s*/\s*(\d{2}|\d{4})\s*$")


class CardError(ValueError):
    """Wrong card details. The message is shown to the user (in Russian)."""


def digits_only(number):
    return re.sub(r"[\s\-]", "", str(number or ""))


def luhn_ok(number):
    """Luhn checksum (every bank card number passes it; a typo in one digit does not)."""
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def brand_of(number):
    """Card brand from the first digits (IIN ranges)."""
    if number.startswith("4"):
        return "Visa"
    if number[:2] in ("34", "37"):
        return "Amex"
    if 2200 <= int(number[:4]) <= 2204:
        return "Mir"
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return "Mastercard"
    if number.startswith("62"):
        return "UnionPay"
    return "Card"


def parse_expiry(text, today=None):
    """'MM/YY' (or MM/YYYY) -> (month, year). The card works until the END of that month."""
    match = EXPIRY_RE.match(str(text or ""))
    if not match:
        raise CardError("Срок действия в формате ММ/ГГ, например 08/29")
    month, year = int(match.group(1)), int(match.group(2))
    if year < 100:
        year += 2000
    if not 1 <= month <= 12:
        raise CardError("Месяц срока действия должен быть от 01 до 12")
    today = today or date.today()
    if (year, month) < (today.year, today.month):
        raise CardError("Срок действия карты истёк")
    if year > today.year + 20:
        raise CardError("Неверный срок действия")
    return month, year


def validate_card(holder, number, expiry, cvv, today=None):
    """Check everything. Returns ONLY what may be stored: holder, brand, last4, exp_month, exp_year."""
    holder = " ".join(str(holder or "").split()).upper()
    if not HOLDER_RE.match(holder):
        raise CardError("Укажите имя держателя карты (как на карте)")
    number = digits_only(number)
    if not number.isdigit() or not 12 <= len(number) <= 19:
        raise CardError("Номер карты: от 12 до 19 цифр")
    if not luhn_ok(number):
        raise CardError("Неверный номер карты (не проходит проверку Луна)")
    brand = brand_of(number)
    month, year = parse_expiry(expiry, today)
    cvv = str(cvv or "").strip()
    need = 4 if brand == "Amex" else 3
    if not (cvv.isdigit() and len(cvv) == need):
        raise CardError(f"CVV: {need} цифры с обратной стороны карты")
    return {"holder": holder, "brand": brand, "last4": number[-4:], "exp_month": month, "exp_year": year}


def mask_email(email):
    """a***@gmail.com - for the payment history and the audit log."""
    name, _, domain = str(email).partition("@")
    return (name[:1] + "***@" + domain) if domain else "***"
