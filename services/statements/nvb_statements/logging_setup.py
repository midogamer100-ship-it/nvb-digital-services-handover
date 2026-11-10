"""Structured JSON logs to stdout with request correlation ids. Never log tokens or cookies."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_STD = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}
_REDACT = {"authorization", "cookie", "session_token", "csrf_token", "token", "password"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance: str) -> None:
        super().__init__()
        self.service, self.instance = service, instance

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "service": self.service,
            "instance": self.instance,
            "request_id": request_id_var.get(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD and not k.startswith("_"):
                out[k] = "[redacted]" if k.lower() in _REDACT else v
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        return json.dumps(out, default=str)


def configure_logging(level: str, service: str, instance: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, instance))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO lines include full URLs
