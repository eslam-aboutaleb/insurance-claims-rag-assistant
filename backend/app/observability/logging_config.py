"""Structured log output with the correlation id on every record.

``logging.basicConfig`` writes one free-text line per record and knows nothing
about the request that produced it. Replacing the handler with a formatter that
emits a fixed JSON schema gives every record the same fields -- including the
correlation id, which is stamped at record-creation time so it belongs to the
context that logged it, not the context that later formats it.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.observability.request_context import display_request_id

# Attributes every LogRecord carries. Anything outside this set was passed by a
# caller as ``logging``'s ``extra=`` and is forwarded into the JSON payload, which
# is what lets a record carry structured fields such as a model name or a cost.
_RESERVED_RECORD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {
    "asctime",
    "message",
    "request_id",
    "taskName",
}

_TEXT_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [%(request_id)s] %(message)s"

_logging_configured = False
_original_record_factory = logging.getLogRecordFactory()


def _record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    """Create a LogRecord that already carries the current correlation id.

    Stamping the record when it is created rather than when a handler formats it
    means a record belongs to the context that emitted it, which stays correct
    even for handlers that format later or elsewhere.

    Args:
        args: Positional arguments forwarded to the previous record factory.
        kwargs: Keyword arguments forwarded to the previous record factory.

    Returns:
        logging.LogRecord: The record with ``request_id`` set.
    """
    record = _original_record_factory(*args, **kwargs)
    record.request_id = display_request_id()
    return record


class ObservabilityLogHandler(logging.StreamHandler):
    """Marker type for the handler this module installs.

    The type exists so reconfiguring logging replaces *this* handler instead of
    stacking another one, which would duplicate every line if ``app.main`` were
    imported twice in one process (the test suite does exactly that).
    """


class JsonLogFormatter(logging.Formatter):
    """Render one log record as a single JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise ``record`` into a JSON object.

        Args:
            record: The record to render.

        Returns:
            str: One-line JSON with the fixed fields plus any structured
            ``extra`` attributes the caller supplied.
        """
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        attributes = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
        }
        if attributes:
            payload["attributes"] = attributes
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


class TextLogFormatter(logging.Formatter):
    """Render one log record on a single human-readable line.

    The layout keeps the original field order and adds the correlation id in
    brackets, so an operator reading a local log can still eyeball a trace.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` with its correlation id guaranteed to be present.

        Args:
            record: The record to render.

        Returns:
            str: One formatted line.
        """
        if not getattr(record, "request_id", ""):
            # A record emitted before logging was configured has no id, and the
            # format string references the field directly.
            record.request_id = display_request_id()
        return super().format(record)


def build_formatter(log_format: str) -> logging.Formatter:
    """Return the formatter for ``log_format``.

    Args:
        log_format: Either ``"json"`` or ``"text"``; anything else falls back to
            the text layout rather than failing at import time.

    Returns:
        logging.Formatter: The requested formatter.
    """
    if log_format == "json":
        return JsonLogFormatter()
    return TextLogFormatter(_TEXT_FORMAT)


def configure_logging(log_level: str, log_format: str) -> None:
    """Install the process-wide log handler, formatter, and record factory.

    Args:
        log_level: Standard level name, e.g. ``"INFO"``; an unrecognised value is
            treated as ``INFO``.
        log_format: ``"json"`` or ``"text"``; see :func:`build_formatter`.
    """
    global _logging_configured  # noqa: PLW0603

    handler = ObservabilityLogHandler()
    handler.setFormatter(build_formatter(log_format))

    root = logging.getLogger()
    for installed in [h for h in root.handlers if isinstance(h, ObservabilityLogHandler)]:
        root.removeHandler(installed)
    root.addHandler(handler)
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    if not _logging_configured:
        logging.setLogRecordFactory(_record_factory)
        _logging_configured = True
