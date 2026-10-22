"""Job-scoped standard logging files without a global console configuration."""

import logging
import os
from functools import wraps
from pathlib import Path

from .logging_context import logging_context

_handler = None


class JobFormatter(logging.Formatter):
    def format(self, record):
        fields = {**getattr(record, "context_fields", {}), **record.__dict__}
        job_id = fields.get("job_id", "-")
        standard = set(logging.makeLogRecord({}).__dict__) | {
            "context_fields",
            "message",
            "asctime",
            "job_id",
            "color_message",
        }
        extra = " ".join(
            f"{k}={v!r}" for k, v in fields.items() if k not in standard and not k.startswith("_")
        )
        output = (
            f"{self.formatTime(record, '%m-%d %H:%M:%S')}.{int(record.msecs):03d}"
            f" | {record.levelname:<5} | {record.name}:{record.lineno}"
            f" [job={str(job_id)[:8]}] | {record.getMessage()}"
        )
        if extra:
            output += " | " + extra
        if record.exc_info:
            output += "\n" + self.formatException(record.exc_info)
        return output


class JobFileHandler(logging.Handler):
    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self.files = {}
        self.setFormatter(JobFormatter())

    def emit(self, record):
        job_id = getattr(record, "job_id", None) or getattr(
            record, "context_fields", {}
        ).get("job_id")
        if not job_id or job_id == "-":
            return
        try:
            if job_id not in self.files:
                self.files[job_id] = (self.directory / f"{job_id}.log").open("a", encoding="utf-8")
            handle = self.files[job_id]
            handle.write(self.format(record) + "\n")
            handle.flush()
        except Exception:
            self.handleError(record)

    def close_job(self, job_id):
        self.acquire()
        try:
            handle = self.files.pop(job_id, None)
            if handle:
                handle.close()
        finally:
            self.release()

    def close(self):
        self.acquire()
        try:
            for handle in self.files.values():
                handle.close()
            self.files.clear()
        finally:
            self.release()
            super().close()


def setup_logger():
    global _handler
    directory = os.environ.get("JOB_LOG_DIR", "").strip()
    if not directory or _handler is not None:
        return
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    _handler = JobFileHandler(path)
    logging.getLogger().addHandler(_handler)


def with_job_log(job_id_getter):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            with logging_context(job_id=str(job_id_getter(*args, **kwargs))):
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def close_job_log(job_id):
    if _handler:
        _handler.close_job(job_id)
