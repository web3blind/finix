"""Request-context cooperative budget; no authority or cancellation of Apply."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_CURRENT = ContextVar('finix_default_preview_budget', default=None)
SECONDS = 20.0


from .core import FinixError


class PreviewBudgetError(FinixError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def check():
    budget = _CURRENT.get()
    if budget is not None:
        deadline, cancelled = budget
        if cancelled is not None and cancelled():
            raise PreviewBudgetError('PREVIEW_CANCELLED')
        if time.monotonic() >= deadline:
            raise PreviewBudgetError('PREVIEW_DEADLINE')


from functools import wraps


def bounded(function):
    @wraps(function)
    def call(*args, **kwargs):
        with scope():
            return function(*args, **kwargs)
    return call


@contextmanager
def scope(*, seconds=SECONDS, cancelled=None):
    previous = _CURRENT.get()
    deadline = time.monotonic() + seconds
    if previous is not None:
        deadline = min(deadline, previous[0])
        if cancelled is None:
            cancelled = previous[1]
    token = _CURRENT.set((deadline, cancelled))
    try:
        check()
        yield
        check()
    finally:
        _CURRENT.reset(token)
