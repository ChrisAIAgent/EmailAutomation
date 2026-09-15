"""Structured logging with per-request correlation IDs.

The backend already calls ``logging.getLogger(...)`` all over the code base. This
module does NOT change those call sites; it only installs a consistent formatter
and a request middleware so that every log line carries a ``trace_id``. That lets
a single HTTP request, an Agent run, a Gmail sync, or a send be traced end-to-end
through ``logs/backend.log`` — the core of "find the bug" diagnostics.

Usage:
    from .logging_config import configure_logging, trace, get_trace_id
    configure_logging()                       # once, at process start
    with trace("run-123"):                    # stamp a pipeline run
        logger.info("processing run")
"""
from __future__ import annotations

import contextlib
import contextvars
import datetime as _dt
import logging
import uuid

# Default value when no request/run context is active (background threads).
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ea_trace_id", default="-"
)

_LOG_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s [tid=%(trace_id)s] %(message)s"
)


class _TraceFormatter(logging.Formatter):
    """Formatter that renders asctime with milliseconds without ``%f``.

    ``time.strftime`` (used by the stdlib when ``datefmt`` contains ``%f``) does
    not support ``%f`` on some platforms, so we build the timestamp ourselves.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = _dt.datetime.fromtimestamp(record.created, tz=_dt.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(round((record.created % 1) * 1_000_000)):06d}"


class _TraceFilter(logging.Filter):
    """Inject the current ``trace_id`` into every record (idempotent)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get()
        return True


def _apply(handler: logging.Handler) -> None:
    if getattr(handler, "_ea_configured", False):
        return
    handler.setFormatter(_TraceFormatter(_LOG_FORMAT))
    handler.addFilter(_TraceFilter())
    handler._ea_configured = True  # type: ignore[attr-defined]


def configure_logging(level: int = logging.INFO) -> None:
    """Install the structured formatter on the root logger's handlers.

    Idempotent: safe to call more than once. Existing handlers are re-used (no
    duplicate lines). Also quiets noisy third-party loggers that would otherwise
    drown out the diagnostic signal.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        _apply(h)
    if not root.handlers:
        h = logging.StreamHandler()
        _apply(h)
        root.addHandler(h)
    # uvicorn ships its own access/error handlers; give them the same formatter
    # so access lines also carry the trace id, and silence the duplicate access
    # stream (our request middleware logs requests once, with timing).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            _apply(h)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("google.auth", "googleapiclient.discovery", "httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_trace_id() -> str:
    return trace_id_var.get()


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


@contextlib.contextmanager
def trace(tid: str | None = None):
    """Context manager that stamps ``tid`` (or a fresh id) on all logs inside.

    Use around a pipeline entry point (sync run, agent run, send) so every log
    line emitted while processing that unit shares one id and can be grepped.
    """
    token = trace_id_var.set(tid or new_trace_id())
    try:
        yield trace_id_var.get()
    finally:
        trace_id_var.reset(token)
