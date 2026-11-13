"""Cooperative cancellation for preparation; no elapsed or idle time limit."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from .core import FinixError

_CANCEL = ContextVar('finix_default_preview_cancel', default=())


class PreviewBudgetError(FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check():
    for cancelled in _CANCEL.get():
        if cancelled():
            raise PreviewBudgetError('PREVIEW_CANCELLED')


def cancellable(function):
    @wraps(function)
    def call(*args, **kwargs):
        check()
        result = function(*args, **kwargs)
        check()
        return result
    return call


@contextmanager
def scope(*, cancelled=None):
    callbacks = _CANCEL.get() + ((cancelled,) if cancelled is not None else ())
    token = _CANCEL.set(callbacks)
    try:
        check()
        yield
        check()
    finally:
        _CANCEL.reset(token)
