"""The Flask application factory.

Thin by design. Everything the app does is in `massingplan/api/`, which knows
nothing about Flask, so this file is routing, security headers and error
mapping -- and massing mounts the same `api` functions under FastAPI with an
adapter of about the same size.

Every import happens inside `create_app`. That is what lets the `offline` CI job
walk-import the package with the network dropped and still get a bootable app.
"""

from __future__ import annotations

from typing import Any


def create_app(settings: Any = None) -> Any:
    from flask import Flask, jsonify, render_template, request

    from .api.errors import ApiError
    from .config import Settings
    from .extensions import csrf

    resolved = settings or Settings()
    app = Flask(__name__)
    app.config.update(resolved.flask_config())
    app.extensions["massingplan_settings"] = resolved

    csrf.init_app(app)

    from .blueprints.main import bp as main_bp
    from .blueprints.schedule_api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api/massingplan/v1")
    # The JSON API authenticates with a bearer key rather than a session cookie,
    # so a CSRF token would be a token protecting nothing. SameSite=Lax plus the
    # required JSON content type carry the defence instead.
    csrf.exempt(api_bp)

    @app.after_request
    def security_headers(response):  # type: ignore[no-untyped-def]
        # `default-src 'self'` with no CDN anywhere. The Gantt is a self-hosted
        # bundle precisely so this line can stay as strict as it is; a single
        # third-party chart library would force `script-src` open.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def wants_json() -> bool:
        return (
            request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"
        )

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):  # type: ignore[no-untyped-def]
        if wants_json():
            return jsonify(exc.to_dict()), exc.status_code
        return render_template("error.html", error=exc), exc.status_code

    @app.errorhandler(404)
    def handle_404(_exc: object):  # type: ignore[no-untyped-def]
        if wants_json():
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

    @app.get("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        return jsonify({"status": "ok"})

    @app.get("/readyz")
    def readyz():  # type: ignore[no-untyped-def]
        # The engine has no external dependencies, so readiness is "the engine
        # computes". Answering `ok` without checking would make the probe a
        # decoration.
        from datetime import date

        from .core.network import Task
        from .core.schedule import schedule_network

        try:
            outcome = schedule_network([Task("probe", "probe", 1)], data_date=date(2026, 1, 1))
            ready = outcome.dates["probe"].duration_days == 1
        except Exception:  # noqa: BLE001 - the probe's failure is the answer
            ready = False
        return jsonify({"status": "ready" if ready else "degraded"}), (200 if ready else 503)

    return app


app = create_app() if __name__ != "__main__" else None
