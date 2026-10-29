"""Context attached at record creation, before handlers queue or write it."""

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_context = ContextVar("hyperrag_log_context", default=None)
_previous_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs):
    record = _previous_factory(*args, **kwargs)
    record.context_fields = {**getattr(record, "context_fields", {}), **(_context.get() or {})}
    return record


logging.setLogRecordFactory(_record_factory)


@contextmanager
def logging_context(**fields):
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)
