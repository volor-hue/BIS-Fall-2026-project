# app/services/auth.py
# ------------------------------------------------------------
# Login check shared by the web form (/login) and the API (/api/auth/login).
# ------------------------------------------------------------
from app.core.security import password_matches
from app.db import database


def check_credentials(login, password):
    """Returns ("employee", id), ("client", id) or None."""
    login = login.strip().lower()
    # First we look for an EMPLOYEE with this login, then for a CLIENT with this email.
    employee = database.get_employee_login(login)
    if employee is not None and password_matches(password, employee["password_hash"]):
        return "employee", employee["employee_id"]
    client = database.get_client_login(login)
    if client is not None and password_matches(password, client["password_hash"]):
        return "client", client["client_id"]
    return None
