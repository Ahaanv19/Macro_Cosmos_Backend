"""
Central security utilities for the SD Auto backend.

This module is intentionally *additive* and *safe-by-default*: every protection
here is designed so that, when its configuration is absent, the application keeps
behaving exactly as it did before. Concretely:

- The IP allowlist allows everyone when ``ADMIN_IP_ALLOWLIST`` is unset.
- Secure cookies / HSTS only switch on in production (see ``is_production``).
- The CSP explicitly whitelists the CDNs the server-rendered admin templates use
  (jQuery, DataTables, Bootstrap) so existing pages render unchanged.
- Rate limits are generous and only meant to stop abuse, not normal traffic.

Public surface:
    init_security(app)          -> wire headers, rate limiting, proxy handling
    limiter                     -> Flask-Limiter instance (use as decorator)
    admin_required              -> RBAC guard for session (Flask-Login) routes
    ip_allowlist()              -> optional IP allowlist guard for admin routes
    audit(action, **details)    -> structured audit-log entry
    sanitize_text(value)        -> strip HTML/scripts from a string (XSS defense)
    sanitize_fields(data, keys) -> sanitize selected fields of a dict in place
    require_fields(data, keys)  -> return list of missing/empty required fields
    get_client_ip()             -> best-effort real client IP
"""

import ipaddress
import logging
import os
from functools import wraps
from logging.handlers import RotatingFileHandler

from flask import abort, g, jsonify, redirect, request, url_for

try:
    import bleach
except ImportError:  # pragma: no cover - bleach is a declared dependency
    bleach = None

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def is_production():
    """Best-effort production detection.

    We treat the app as production when an external DB endpoint is configured
    (matches the existing logic in ``__init__.py``) or when an explicit
    environment flag is set. This keeps local SQLite development fully
    permissive (no forced HTTPS, no secure-cookie lockout).
    """
    if os.environ.get("FLASK_ENV") == "production":
        return True
    if os.environ.get("ENV", "").lower() in ("prod", "production"):
        return True
    return bool(os.environ.get("DB_ENDPOINT"))


def get_client_ip():
    """Return the best-effort client IP.

    Honors ``X-Forwarded-For`` only when ``TRUST_PROXY`` is enabled, so that a
    direct (non-proxied) deployment cannot be spoofed via a forged header.
    """
    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        fwd = request.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

audit_logger = logging.getLogger("sd_auto.audit")


def _configure_audit_logger(app):
    if audit_logger.handlers:
        return  # already configured
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s AUDIT %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z"
    )

    # Always log to console.
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    audit_logger.addHandler(stream)

    # Best-effort rotating file in the instance folder.
    try:
        log_path = os.path.join(app.instance_path, "audit.log")
        os.makedirs(app.instance_path, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(fmt)
        audit_logger.addHandler(file_handler)
    except Exception as exc:  # pragma: no cover - never fail app startup on this
        audit_logger.warning("Could not open audit log file: %s", exc)


def audit(action, **details):
    """Record a structured audit event for a security-relevant action."""
    try:
        actor = "anonymous"
        # Session (Flask-Login) user, if present.
        try:
            from flask_login import current_user

            if getattr(current_user, "is_authenticated", False):
                actor = getattr(current_user, "uid", None) or getattr(
                    current_user, "id", "authenticated"
                )
        except Exception:
            pass
        # JWT user, if present.
        if actor == "anonymous" and getattr(g, "current_user", None) is not None:
            actor = getattr(g.current_user, "uid", None) or getattr(
                g.current_user, "id", "jwt-user"
            )

        ip = get_client_ip()
        method = request.method if request else "-"
        path = request.path if request else "-"
        detail_str = " ".join(f"{k}={v}" for k, v in details.items())
        audit_logger.info(
            "action=%s actor=%s ip=%s method=%s path=%s %s",
            action, actor, ip, method, path, detail_str,
        )
    except Exception:  # never let auditing break a request
        pass


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

# Generous global limits: meant to stop scripted abuse, not real users.
limiter = Limiter(
    key_func=get_client_ip,
    default_limits=["2000 per hour", "200 per minute"],
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    headers_enabled=True,
)


# ---------------------------------------------------------------------------
# RBAC + IP allowlist guards (for session / Flask-Login routes)
# ---------------------------------------------------------------------------

def admin_required(func):
    """Require an authenticated user with the ``Admin`` role (session auth).

    Use *in addition to* ``@login_required`` on server-rendered admin routes.
    JWT API endpoints should keep using ``token_required(['Admin'])``.
    """

    @wraps(func)
    def wrapper(*args, **kwargs):
        from flask_login import current_user

        if not getattr(current_user, "is_authenticated", False):
            audit("admin_access_denied", reason="unauthenticated")
            # Mirror existing unauthorized behavior: redirect to login.
            return redirect(url_for("login", next=request.path))
        if getattr(current_user, "role", None) != "Admin":
            audit("admin_access_denied", reason="not_admin")
            return jsonify({"error": "Forbidden: admin role required"}), 403
        return func(*args, **kwargs)

    return wrapper


def ip_allowlist(env_var="ADMIN_IP_ALLOWLIST"):
    """Restrict a route to a comma-separated allowlist of IPs / CIDR ranges.

    Safe by default: if ``env_var`` is unset or empty, every request is allowed
    so existing behavior is unchanged. Set e.g.
    ``ADMIN_IP_ALLOWLIST="203.0.113.4,10.0.0.0/8"`` to enforce.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            raw = os.environ.get(env_var, "").strip()
            if not raw:
                return func(*args, **kwargs)  # not configured -> allow

            client_raw = get_client_ip()
            try:
                client_ip = ipaddress.ip_address(client_raw)
            except ValueError:
                audit("ip_allowlist_denied", reason="unparseable_ip", ip=client_raw)
                return jsonify({"error": "Forbidden"}), 403

            for entry in (e.strip() for e in raw.split(",") if e.strip()):
                try:
                    if "/" in entry:
                        if client_ip in ipaddress.ip_network(entry, strict=False):
                            return func(*args, **kwargs)
                    elif client_ip == ipaddress.ip_address(entry):
                        return func(*args, **kwargs)
                except ValueError:
                    continue  # skip malformed allowlist entries

            audit("ip_allowlist_denied", reason="not_in_allowlist", ip=client_raw)
            return jsonify({"error": "Forbidden: IP not allowed"}), 403

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Input sanitization / validation (XSS + request validation at the edge)
# ---------------------------------------------------------------------------

def sanitize_text(value, max_length=None):
    """Strip all HTML/script content from a string to neutralize stored XSS.

    Non-string values are returned untouched so callers can pass dict values
    blindly. Optionally truncates to ``max_length``.
    """
    if not isinstance(value, str):
        return value
    if bleach is not None:
        cleaned = bleach.clean(value, tags=[], attributes={}, strip=True)
    else:  # fallback: drop angle brackets if bleach is somehow unavailable
        cleaned = value.replace("<", "&lt;").replace(">", "&gt;")
    cleaned = cleaned.strip()
    if max_length is not None:
        cleaned = cleaned[:max_length]
    return cleaned


def sanitize_fields(data, fields, max_length=None):
    """Sanitize selected string fields of a dict in place and return it."""
    if not isinstance(data, dict):
        return data
    for field in fields:
        if field in data:
            data[field] = sanitize_text(data[field], max_length=max_length)
    return data


def require_fields(data, fields):
    """Return the list of required fields that are missing or empty."""
    if not isinstance(data, dict):
        return list(fields)
    missing = []
    for field in fields:
        value = data.get(field)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing.append(field)
    return missing


# ---------------------------------------------------------------------------
# Security headers (XSS / clickjacking / sniffing defenses)
# ---------------------------------------------------------------------------

# CDNs used by the server-rendered admin templates (utable/u2table/login).
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' "
    "https://cdn.datatables.net https://cdn.jsdelivr.net https://code.jquery.com; "
    "style-src 'self' 'unsafe-inline' "
    "https://cdn.datatables.net https://cdn.jsdelivr.net; "
    "img-src 'self' data: https:; "
    "font-src 'self' https: data:; "
    "connect-src 'self' https:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'none'"
)


def _apply_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(self), microphone=(), camera=()"
    )
    response.headers.setdefault("Content-Security-Policy", _CSP)
    # Only assert HSTS over real HTTPS so local HTTP dev is unaffected.
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def init_security(app):
    """Attach all app-wide security middleware. Idempotent and safe by default."""
    _configure_audit_logger(app)

    # Respect the reverse proxy's forwarded headers only when explicitly trusted.
    if os.environ.get("TRUST_PROXY", "").lower() in ("1", "true", "yes"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Harden session cookies. HTTPONLY is safe everywhere; Secure/SameSite=None
    # (required for the cross-origin GitHub Pages frontend) only in production.
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    if is_production():
        app.config["SESSION_COOKIE_SECURE"] = True
        app.config.setdefault("SESSION_COOKIE_SAMESITE", "None")

    # Rate limiting.
    limiter.init_app(app)

    # Security response headers on every response.
    app.after_request(_apply_security_headers)

    # Fail loudly (prod) / warn (dev) on default secrets.
    _check_secret_strength(app)

    return app


def _check_secret_strength(app):
    weak_secret = app.config.get("SECRET_KEY") in (None, "", "SECRET_KEY")
    weak_admin_pw = app.config.get("ADMIN_PASSWORD") in (None, "", "password")
    problems = []
    if weak_secret:
        problems.append("SECRET_KEY is using an insecure default")
    if weak_admin_pw:
        problems.append("ADMIN_PASSWORD is using an insecure default")
    if not problems:
        return
    message = "; ".join(problems)
    if is_production():
        raise RuntimeError(
            f"Refusing to start in production with weak secrets: {message}. "
            "Set strong SECRET_KEY / ADMIN_PASSWORD environment variables."
        )
    app.logger.warning("SECURITY WARNING: %s (insecure for production).", message)
