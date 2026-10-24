"""Prevent ambiguous channel POST retries while retaining Discord rate limiting."""
from contextlib import contextmanager
from contextvars import ContextVar

import aiohttp


class AmbiguousCreate(RuntimeError):
    """A previous POST may have succeeded; its automatic retry was stopped."""


_attempt = ContextVar("channel_create_attempt", default=None)


@contextmanager
def guarded_create():
    # Mutable per-operation state also survives awaits within the request task.
    token = _attempt.set({"sent": False})
    try:
        yield
    finally:
        _attempt.reset(token)


def create_trace():
    trace = aiohttp.TraceConfig()

    async def start(session, context, params):
        state = _attempt.get()
        if state is None or params.method != "POST":
            return
        if state["sent"]:
            raise AmbiguousCreate("Automatic retry of an uncertain create was prevented.")
        state["sent"] = True

    async def end(session, context, params):
        state = _attempt.get()
        # A received 429 is a definite rejection, so a rate-limit retry is safe.
        if state is not None and params.method == "POST" and params.response.status == 429:
            state["sent"] = False

    trace.on_request_start.append(start)
    trace.on_request_end.append(end)
    return trace
