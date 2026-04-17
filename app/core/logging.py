import logging
import json
import os
from datetime import datetime

SERVICE_NAME = os.getenv("SERVICE_NAME", "biocodetechpay")


class JsonFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.utcfromtimestamp(record.created).isoformat() + "Z"

    def format(self, record):
        base = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # include useful extras (correlation_id, user_id, transaction_id, etc.)
        extras = {}
        for k, v in record.__dict__.items():
            if k in ("name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread", "threadName", "processName", "process"):
                continue
            if k.startswith("_"):
                continue
            extras[k] = v

        base.update(extras)

        # attach exception info if present
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)

        try:
            return json.dumps(base, default=str, ensure_ascii=False)
        except Exception:
            # fallback plain text
            return json.dumps({"service": SERVICE_NAME, "message": record.getMessage()})


def configure_logging(level: int = logging.INFO):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)

    # replace handlers to avoid duplication in long-lived processes
    root.handlers = [handler]


def get_logger(name: str = __name__):
    return logging.getLogger(name)
