# tests/test_payments.py
# Payment methods for repayments: saved cards (only last4/brand/expiry/holder are stored),
# default card, PayPal and Kaspi QR (demo), and the old "Pay" without a method still working.
from datetime import date

import pytest

from conftest import face_token_for

CAROL = {"iin": "900101300444", "full_name": "Carol", "birth_date": "1990-01-01", "phone": "+77010000004",
         "email": "carol@test.kz", "password": "carol123",
         "employer_name": "Kaspi", "position": "Dev", "monthly_income": 300000}
DAN = dict(CAROL, iin="900101300555", full_name="Dan", phone="+77010000005", email="dan@test.kz",
           password="dan12345")
VISA = "4242 4242 4242 4242"          # test numbers that pass the Luhn check
MASTER = "5555555555554444"
NEXT_YEAR = f"12/{(date.today().year + 1) % 100:02d}"


def card(number=VISA, **extra):
    return dict({"holder": "Carol Test", "number": number, "expiry": NEXT_YEAR, "cvv": "123"}, **extra)


@pytest.fixture
def carol(client, as_user):
    # A client with an auto-approved loan -> a schedule of 12 repayments.
    assert client.post("/api/auth/register", json=CAROL).status_code == 201
    user = as_user(CAROL["email"], CAROL["password"])
    me = user.get("/api/auth/me").json()["user_id"]
    app = user.post("/api/applications", json={"client_id": me, "requested_amount": 100000, "term_months": 12,
                                                "face_token": face_token_for(me)}).json()
    user.payments = [p["payment_id"] for p in user.get(f"/api/loans/{app['loan_id']}/repayments").json()]
    user.loan_id = app["loan_id"]
    return user


# ---------- pure checks ----------

def test_luhn_brand_and_expiry():
    from payments import cards

    assert cards.luhn_ok("4242424242424242") and not cards.luhn_ok("4242424242424241")
    assert cards.brand_of("4242424242424242") == "Visa"
    assert cards.brand_of("5555555555554444") == "Mastercard"
    assert cards.brand_of("2200123456789019") == "Mir"
    assert cards.brand_of("378282246310005") == "Amex"
    today = date(2026, 11, 22)
    assert cards.parse_expiry("11/26", today) == (11, 2026)      # valid until the end of the month
    with pytest.raises(cards.CardError):
        cards.parse_expiry("10/26", today)                        # last month -> expired
    with pytest.raises(cards.CardError):
        cards.parse_expiry("13/27", today)
    with pytest.raises(cards.CardError):
        cards.validate_card("Carol", "378282246310005", "12/30", "123", today)   # Amex needs 4 digits
    assert cards.mask_email("carol@mail.com") == "c***@mail.com"


def test_qr_svg_is_a_real_qr():
    from payments import qr

    rows = qr.matrix("https://kaspi.kz/pay/MFO-DEMO?payment=1&amount=9455.73&ref=abc")
    assert len(rows) in (33, 37, 41) and all(len(r) == len(rows) for r in rows)
    assert rows[0][:7] == [True] * 7                              # finder pattern corner
    assert qr.svg("hi").startswith("<svg")
    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    text = "https://kaspi.kz/pay/MFO-DEMO?payment=7&amount=123.45&ref=xyz"
    m = qr.matrix(text)
    img = np.full((len(m) + 8, len(m) + 8), 255, np.uint8)
    for y, row in enumerate(m):
        for x, dark in enumerate(row):
            if dark:
                img[y + 4, x + 4] = 0
    img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    assert cv2.QRCodeDetector().detectAndDecode(img)[0] == text


# ---------- cards ----------

def test_card_is_saved_without_number_and_cvv(carol):
    r = carol.post("/api/payment-methods/cards", json=card())
    assert r.status_code == 201, r.text
    saved = r.json()
    assert saved["last4"] == "4242" and saved["brand"] == "Visa" and saved["holder"] == "CAROL TEST"
    assert saved["is_default"] is True                            # the first card is the default
    import database

    conn = database.get_connection()
    row = dict(conn.execute("SELECT * FROM payment_cards").fetchone())
    conn.close()
    assert "4242424242424242" not in str(row)                     # no full number (and no CVV column)
    assert set(row) == {"card_id", "client_id", "holder", "brand", "last4", "exp_month", "exp_year",
                        "is_default", "created_at"}


@pytest.mark.parametrize("bad", [
    card(number="4242424242424241"), card(expiry="01/20"), card(cvv="12"), card(holder=""),
    card(number="1234"), card(expiry="1/2"),
])
def test_bad_cards_are_refused(carol, bad):
    r = carol.post("/api/payment-methods/cards", json=bad)
    assert r.status_code == 400
    assert bad["number"].replace(" ", "") not in r.text           # the number is never echoed back


def test_default_card_switch_and_delete(carol):
    first = carol.post("/api/payment-methods/cards", json=card()).json()
    second = carol.post("/api/payment-methods/cards", json=card(MASTER)).json()
    assert second["is_default"] is False
    assert carol.post("/api/payment-methods/cards", json=card()).status_code == 409   # same card twice
    cards = carol.post(f"/api/payment-methods/cards/{second['card_id']}/default").json()
    assert [c["card_id"] for c in cards if c["is_default"]] == [second["card_id"]]
    assert cards[0]["card_id"] == second["card_id"]               # default first
    cards = carol.delete(f"/api/payment-methods/cards/{second['card_id']}").json()
    assert [(c["card_id"], c["is_default"]) for c in cards] == [(first["card_id"], True)]
    assert carol.delete(f"/api/payment-methods/cards/{second['card_id']}").status_code == 404


def test_cards_are_private(carol, client, as_user, staff):
    mine = carol.post("/api/payment-methods/cards", json=card()).json()
    assert client.post("/api/auth/register", json=DAN).status_code == 201
    dan = as_user(DAN["email"], DAN["password"])
    assert dan.get("/api/payment-methods/cards").json() == []
    assert dan.delete(f"/api/payment-methods/cards/{mine['card_id']}").status_code == 404
    assert dan.post(f"/api/payment-methods/cards/{mine['card_id']}/default").status_code == 404
    assert staff.get("/api/payment-methods/cards").status_code == 403
    assert client.get("/api/payment-methods/cards").status_code == 401
    # Dan cannot pay Carol's repayment, not even with his own method.
    r = dan.post(f"/api/repayments/{carol.payments[0]}/pay", json={"method": "paypal", "paypal_email": "d@x.kz"})
    assert r.status_code == 403


# ---------- paying ----------

def test_pay_with_default_card_records_the_method(carol, caplog):
    carol.post("/api/payment-methods/cards", json=card())
    r = carol.post(f"/api/repayments/{carol.payments[0]}/pay", json={"method": "card"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Paid" and r.json()["paid_method"] == "Visa •••• 4242"
    rows = carol.get(f"/api/loans/{carol.loan_id}/repayments").json()
    assert rows[0]["paid_method"] == "Visa •••• 4242" and rows[1]["paid_method"] is None
    assert "paid_with=card" in caplog.text


def test_pay_with_card_needs_a_card(carol):
    r = carol.post(f"/api/repayments/{carol.payments[0]}/pay", json={"method": "card"})
    assert r.status_code == 400
    assert carol.get(f"/api/loans/{carol.loan_id}/repayments").json()[0]["status"] == "Pending"


def test_pay_with_paypal(carol):
    assert carol.post(f"/api/repayments/{carol.payments[0]}/pay",
                      json={"method": "paypal", "paypal_email": "nope"}).status_code == 400
    r = carol.post(f"/api/repayments/{carol.payments[0]}/pay",
                   json={"method": "paypal", "paypal_email": "carol@mail.com"})
    assert r.json()["paid_method"] == "PayPal c***@mail.com"


def test_pay_with_kaspi_qr_is_one_time(carol):
    pid = carol.payments[0]
    assert carol.post(f"/api/repayments/{pid}/pay", json={"method": "kaspi_qr", "qr_ref": "fake"}).status_code == 400
    qr = carol.post(f"/api/repayments/{pid}/kaspi-qr").json()
    assert qr["svg"].startswith("<svg") and f"payment={pid}" in qr["qr_text"]
    # A QR of one repayment cannot pay another one.
    other = carol.post(f"/api/repayments/{carol.payments[1]}/pay", json={"method": "kaspi_qr", "qr_ref": qr["qr_ref"]})
    assert other.status_code == 400
    r = carol.post(f"/api/repayments/{pid}/pay", json={"method": "kaspi_qr", "qr_ref": qr["qr_ref"]})
    assert r.status_code == 200 and r.json()["paid_method"] == "Kaspi QR"
    assert carol.post(f"/api/repayments/{pid}/kaspi-qr").status_code == 400      # already paid


def test_pay_without_method_still_works_and_unknown_method_is_refused(carol, staff):
    assert carol.post(f"/api/repayments/{carol.payments[0]}/pay", json={"method": "bitcoin"}).status_code == 400
    assert carol.post(f"/api/repayments/{carol.payments[0]}/pay").json()["paid_method"].startswith("Manual")
    assert staff.post(f"/api/repayments/{carol.payments[1]}/pay").json()["status"] == "Paid"
    assert staff.post(f"/api/repayments/{carol.payments[2]}/pay", json={"method": "card"}).status_code == 403


def test_dashboard_has_the_payment_window(carol):
    page = carol.get("/dashboard").text
    assert 'id="pay-modal"' in page and "Kaspi QR" in page and "PayPal" in page
