"""The Flask application factory.

Thin by design. Everything the app *does* is in `massingplan/api/`, which knows
nothing about Flask, so this file is routing, security headers, auth plumbing
and error mapping.

Every import happens inside `create_app`. That is what lets the `offline` CI job
walk-import the package with the network dropped and still get a bootable app.
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger("massingplan.request")


def create_app(settings: Any = None) -> Any:
    from flask import Flask, g, jsonify, render_template, request

    from . import logging_config
    from .api.errors import ApiError
    from .blueprints import deps
    from .config import Settings
    from .database import init_engine
    from .extensions import csrf

    resolved = settings or Settings()
    logging_config.configure(resolved.log_level, json_output=resolved.is_production)

    app = Flask(__name__)
    app.config.update(resolved.flask_config())
    app.extensions["massingplan_settings"] = resolved

    init_engine(resolved.database_url)
    csrf.init_app(app)

    from .services.ratelimit import RateLimiter, warn_if_multi_worker

    limiter = RateLimiter(enabled=resolved.rate_limit_enabled)
    app.extensions["massingplan_ratelimit"] = limiter
    warn_if_multi_worker(limiter, resolved.web_concurrency)

    from .blueprints.auth import bp as auth_bp
    from .blueprints.main import bp as main_bp
    from .blueprints.schedule_api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api/massingplan/v1")
    # The JSON API authenticates with a bearer key and *refuses* a session
    # cookie, so a CSRF token would protect nothing: there is no ambient
    # credential to forge a request with. That refusal is what makes this
    # exemption sound, and it is enforced in `schedule_api.require_a_credential`
    # rather than assumed here.
    csrf.exempt(api_bp)

    app.teardown_appcontext(deps.close_db)

    @app.before_request
    def start_request() -> None:
        g.started_at = time.monotonic()
        g.request_id = (
            request.headers.get(logging_config.REQUEST_ID_HEADER) or logging_config.new_request_id()
        )
        deps.load_principal()

    @app.before_request
    def enforce_rate_limit():  # type: ignore[no-untyped-def]
        """Keyed by the authenticated subject where there is one, else the
        client address.

        Keying an unauthenticated endpoint by address is imperfect -- a NAT
        shares one -- but the alternative on a sign-in form is keying by the
        submitted email, which lets an attacker lock out an account they merely
        know the address of.
        """
        principal = deps.current_principal()
        identity = principal.subject_id or (request.remote_addr or "unknown")
        decision = limiter.check(request.endpoint or "", identity)
        if decision is None or decision.allowed:
            return None
        logging_config.log(
            logger,
            logging.WARNING,
            "rate limited",
            request_id=g.get("request_id", ""),
            endpoint=request.endpoint,
            limit=str(decision.limit),
        )
        if deps.wants_json():
            body = jsonify(
                {
                    "error": {
                        "code": "rate_limited",
                        "message": f"too many requests; the limit is {decision.limit}",
                        "retry_after_seconds": decision.retry_after_seconds,
                    }
                }
            )
            response = app.make_response((body, 429))
        else:
            response = app.make_response(
                (
                    render_template(
                        "error.html",
                        error=type(
                            "E",
                            (),
                            {
                                "message": "Too many requests.",
                                "detail": (
                                    f"The limit on this action is {decision.limit}. "
                                    f"Try again in {decision.retry_after_seconds} seconds."
                                ),
                            },
                        )(),
                    ),
                    429,
                )
            )
        response.headers["Retry-After"] = str(decision.retry_after_seconds)
        return response

    @app.after_request
    def finish_request(response):  # type: ignore[no-untyped-def]
        response.headers[logging_config.REQUEST_ID_HEADER] = g.get("request_id", "")
        # `default-src 'self'` with no CDN anywhere. The Gantt is a self-hosted
        # bundle precisely so this line can stay this strict; one third-party
        # chart library would force `script-src` open.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        if resolved.is_production:
            # Only over TLS, and only in production: sending HSTS from a plain
            # http:// dev server teaches the browser to refuse it afterwards.
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        if request.path.startswith("/api/") or response.status_code >= 400:
            principal = deps.current_principal()
            logging_config.log(
                logger,
                logging.INFO if response.status_code < 500 else logging.ERROR,
                "request",
                request_id=g.get("request_id", ""),
                method=request.method,
                path=request.path,
                status=response.status_code,
                ms=round((time.monotonic() - g.get("started_at", time.monotonic())) * 1000, 1),
                actor=principal.subject_id or None,
                via=principal.via if principal.is_authenticated else None,
                organization=principal.organization_id,
            )
        return response

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):  # type: ignore[no-untyped-def]
        if deps.wants_json():
            return jsonify(exc.to_dict()), exc.status_code
        return render_template("error.html", error=exc), exc.status_code

    @app.errorhandler(404)
    def handle_404(_exc: object):  # type: ignore[no-untyped-def]
        if deps.wants_json():
            return jsonify({"error": {"code": "not_found", "message": "No such endpoint"}}), 404
        return render_template("error.html", error=None), 404

    @app.errorhandler(413)
    def handle_too_large(_exc: object):  # type: ignore[no-untyped-def]
        limit = app.config["MAX_CONTENT_LENGTH"]
        return jsonify(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"the upload exceeds the {limit} byte limit",
                }
            }
        ), 413

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):  # type: ignore[no-untyped-def]
        """Log the cause; disclose the request id and nothing else.

        A stack trace in a response tells an attacker the framework, the file
        layout and often a query. The id is enough for support to find the log
        line that has all of it.
        """
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc
        request_id = g.get("request_id", "")
        logger.exception(
            "unhandled exception",
            extra={"context": {"request_id": request_id, "path": request.path}},
        )
        if deps.wants_json():
            return jsonify(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "Something went wrong.",
                        "request_id": request_id,
                    }
                }
            ), 500
        return render_template("error.html", error=None, request_id=request_id), 500

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        from .models.identity import Permission

        # `Permission` in the template context so a page can ask
        # `principal.can(Permission.BASELINE_SET)` rather than comparing role
        # strings. Comparing strings in a template is how a role rename becomes
        # a silently-visible button.
        return {
            "principal": deps.current_principal(),
            "request_id": g.get("request_id", ""),
            "Permission": Permission,
        }

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        """Liveness. Deliberately does not touch the database.

        A liveness probe that fails when the database is briefly unreachable
        gets the container killed and restarted, which does not bring the
        database back and does lose in-flight requests.
        """
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():  # type: ignore[no-untyped-def]
        """Readiness: the engine computes and the database answers.

        Both checked for real. Returning `ok` without checking is how a
        container reports itself healthy for its entire life while no traffic
        ever works.
        """
        from datetime import date

        from sqlalchemy import text

        from .core.network import Task
        from .core.schedule import schedule_network

        checks: dict[str, str] = {}
        try:
            outcome = schedule_network([Task("probe", "probe", 1)], data_date=date(2026, 1, 1))
            checks["engine"] = "ok" if outcome.dates["probe"].duration_days == 1 else "wrong"
        except Exception:  # noqa: BLE001 - the probe's failure is the answer
            checks["engine"] = "failed"
        try:
            deps.db().execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception:
            # The cause goes to the log, not to an unauthenticated response.
            logger.exception("readiness: database unreachable")
            checks["database"] = "unreachable"

        ready = all(value == "ok" for value in checks.values())
        return jsonify({"status": "ready" if ready else "degraded", "checks": checks}), (
            200 if ready else 503
        )

    return app
