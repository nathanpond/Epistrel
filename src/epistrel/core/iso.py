"""The ISO forms a date-time or a date is accepted in (FR-API-3, FR-API-4; #788).

pydantic's lax ``datetime`` and ``date`` also take a number of seconds since the epoch (``258``,
``0.0``, ``-1`` or ``"258"``), which the schema's ``date-time`` and ``date`` do not promise.
These before-validators refuse it: ``epistrel.api.iso`` builds the query types from them, and
the Engine checks its library callers' ``datetime`` and ``date`` arguments with them. They live
here because the Engine cannot import ``epistrel.api``."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE_TIME_EXAMPLE = "2026-10-11T12:00:00Z"
DATE_EXAMPLE = "2026-11-08"


def iso_date_time(value: Any) -> Any:
    """A ``datetime``, or a string that is no number (pydantic then parses it: an RFC 3339
    date-time, or a naive or date-only one as before #788)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            float(value)
        except ValueError:
            return value
    raise ValueError(f"must be an RFC 3339 date-time, e.g. {DATE_TIME_EXAMPLE}")


def iso_date(value: Any) -> Any:
    """A ``date``, or a ``YYYY-MM-DD`` string."""
    if isinstance(value, date):
        return value
    if isinstance(value, str) and ISO_DATE.fullmatch(value):
        return value
    raise ValueError(f"must be a date, e.g. {DATE_EXAMPLE}")
