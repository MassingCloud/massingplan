"""JSON logs with a request id.

One line per event, machine-parseable, correlatable. The request id is echoed
back as ``X-Request-Id`` so a user reporting "it broke at 14:32" can hand over
an identifier that finds the exact request rather than a timestamp that finds
four hundred.

What never reaches a log line: passwords, API keys, session cookies, or a whole
request body. A log that carries the credential it recorded the use of is a
second copy of the credential, in the place with the loosest access controls.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any

REQUEST_ID_HEADER = "X-Request-Id"

#: Never emitted, whatever a caller passes. Checked against the lowercase key.
_REDACTED_KEYS = frozenset(
    {"password", "api_key", "authorization", "cookie", "secret", "token", "key_hash"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "context", {}).items():
            if key.lower() in _REDACTED_KEYS:
                payload[key] = "[redacted]"
            else:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # `default=str` so an unexpected type degrades to its repr instead of
        # raising inside the logger -- a logging call that can throw turns a
        # handled error into an unhandled one.
        return json.dumps(payload, default=str)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def configure(level: str = "INFO", *, json_output: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-5s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    # The access log is the web server's job and duplicating it here doubles
    # the volume for no extra information.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, message: str, **context: object) -> None:
    """Log with structured context, redacted."""
    logger.log(level, message, extra={"context": context})
