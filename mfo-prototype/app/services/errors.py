# app/services/errors.py
# ------------------------------------------------------------
# Errors of the business layer. A service does not know about HTTP: it raises one of these,
# and app/main.py turns it into a JSON answer {"detail": ...} with the right status code.
# ------------------------------------------------------------


class ServiceError(Exception):
    status_code = 400

    def __init__(self, detail, reason=None):
        super().__init__(detail)
        self.detail = detail
        # Short code for the audit log (never personal data), e.g. "duplicate", "no_biometrics".
        self.reason = reason


class NotFoundError(ServiceError):
    """The object (client, application, payment...) does not exist -> 404."""
    status_code = 404


class RuleError(ServiceError):
    """The request breaks a business rule (already paid, already decided, duplicate...) -> 400."""
    status_code = 400
