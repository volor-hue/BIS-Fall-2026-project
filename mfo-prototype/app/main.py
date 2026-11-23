# app/main.py
# ------------------------------------------------------------
# The web application (FastAPI). This file only ASSEMBLES the app: logging, rate limits,
# error handlers, the request log, and the routers. The code itself lives in the layers:
#
#   app/api/        REST API routes (JSON), one module per entity; deps.py = who may do what
#   app/web/        HTML pages and templates
#   app/services/   business logic (approval rules, applications, loans, registration)
#   app/db/         the database layer (all SQL)
#   app/core/       settings, security (JWT, passwords), logging, rate limits
#   app/face_auth/  webcam face check (its own router, service and storage)
#   app/payments/   payment methods: cards, PayPal, Kaspi QR (demo)
#   app/repayment_rules/  overdue, penalty, early payoff
#
# Run it with:   python run.py --web-only    (or: uvicorn app.main:app --reload --port 8001)
# Pages:         http://127.0.0.1:8001
# API docs:      http://127.0.0.1:8001/docs   (Swagger, press "Authorize" to log in)
# ------------------------------------------------------------
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api import applications, auth, clients, loans
from app.api.deps import who
from app.core import config, rate_limit
from app.core.logs import audit, clean, log, setup_logging
from app.core.rate_limit import limiter
from app.db import database
from app.face_auth.router import router as face_router
from app.payments.router import router as payments_router
from app.repayment_rules.router import router as repayment_rules_router
from app.services.errors import ServiceError
from app.web import pages
from app.web.templates import templates

# Logs go to the console and to logs/app.log + logs/audit.log (see app/core/logs.py).
setup_logging()
# Settings that were wrong and replaced by defaults are written into the log once (app/core/config.py).
config.log_settings_problems()

app = FastAPI(title="Microfinance IS", description="Loan applications, checks, loans and repayments")

# Rate limiting (see app/core/rate_limit.py): this middleware applies the default limit to every
# endpoint. Endpoints with their own @limiter.limit(...) use only their own limit.
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# Create the tables and the default employee when the app starts.
database.init_db()
log.info("app started db=%s", clean(database.DB_FILE))


# ============================================================
# Errors
# ============================================================

TOO_MANY_REQUESTS = "Too many requests. Please wait a minute and try again."


def rate_limit_exceeded(request: Request, exc: RateLimitExceeded):
    # Called when an IP went over a limit: answer 429 Too Many Requests.
    ip = request.client.host if request.client else "-"
    if rate_limit.should_audit(ip, exc.detail):
        audit("rate_limited", request, logging.WARNING, method=request.method,
              path=request.url.path, limit=exc.detail)
    # Retry-After tells the client how many seconds to wait (the length of the limit window).
    headers = {"Retry-After": str(exc.limit.limit.get_expiry())}
    # The login and registration FORMS show the error on the same page, the API answers JSON.
    if request.url.path == "/login":
        return templates.TemplateResponse(request, "login.html",
                                          {"error": TOO_MANY_REQUESTS, "registered": False},
                                          status_code=429, headers=headers)
    if request.url.path == "/register":
        return pages.register_form(request, TOO_MANY_REQUESTS, "", status_code=429, headers=headers)
    return JSONResponse({"detail": TOO_MANY_REQUESTS}, status_code=429, headers=headers)


def service_error(request: Request, exc: ServiceError):
    # A business rule said no (app/services/errors.py): same JSON shape as FastAPI's own errors.
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded)
app.add_exception_handler(ServiceError, service_error)


# ============================================================
# Request log: this function runs around EVERY request (pages and API).
# It writes one line per request into logs/app.log, and the full traceback if the code crashed.
# It is added AFTER the rate limit middleware, so it runs first and also logs blocked (429) requests.
# ============================================================

@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.user = who(request)  # audit() in app/core/logs.py reads it
    ip = request.client.host if request.client else "-"
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # log.exception also saves the traceback, so the error can be found later.
        log.exception("request crashed method=%s path=%s ip=%s user=%s",
                      request.method, clean(request.url.path), ip, request.state.user)
        raise
    ms = (time.perf_counter() - start) * 1000
    # 4xx/5xx (wrong data, wrong password, not found, server error) are logged as WARNING.
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    log.log(level, "request method=%s path=%s status=%s ms=%.0f ip=%s user=%s",
            request.method, clean(request.url.path), response.status_code, ms, ip, request.state.user)
    return response


# ============================================================
# Routers. REST API (JSON) is protected with JWT:
#   public:    POST /api/auth/register, POST /api/auth/login, POST /api/clients (registration)
#   employee:  everything
#   client:    only their own data (another client's data -> 403)
#   no token:  401
# ============================================================

def add_routes(router):
    # Why not app.include_router(router)? Since FastAPI 0.16x an included router stays ONE object
    # inside app.routes, and slowapi's middleware only looks at the top-level routes. It would not
    # find these endpoints and the DEFAULT rate limit (MFO_RATE_LIMIT_DEFAULT) would silently not
    # apply to them. The routes of an APIRouter already carry their full path, tags and
    # dependencies, so adding them one by one keeps every endpoint visible to the limiter.
    # (tests/test_rate_limit.py checks that the default limit works.)
    app.router.routes.extend(router.routes)


add_routes(auth.router)            # /api/auth/*
add_routes(clients.router)         # /api/clients/*, /api/employees
add_routes(applications.router)    # /api/applications/*
add_routes(loans.router)           # /api/loans/*, /api/repayments/{id}/pay
add_routes(face_router)            # /api/face/*, /face        (app/face_auth/router.py)
add_routes(payments_router)        # /api/payment-methods/*, Kaspi QR (app/payments/router.py)
add_routes(repayment_rules_router)  # /api/loans/{id}/early-payoff (app/repayment_rules/router.py)
add_routes(pages.router)           # HTML pages
