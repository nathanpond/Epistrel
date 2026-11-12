"""The query types for a ``date-time`` and a ``date`` (FR-API-4; #788): every date or
date-time query parameter is typed with one of them, so ``?until=258`` is 422
``validation_error`` at ``query.until`` instead of a time 258 seconds after the epoch. The
published schema is unchanged: ``format: date-time`` and ``format: date``."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BeforeValidator

from epistrel.core.iso import iso_date, iso_date_time

IsoDateTime = Annotated[datetime, BeforeValidator(iso_date_time)]
"""A query ``date-time``: no number is taken for one."""

IsoDate = Annotated[date, BeforeValidator(iso_date)]
"""A query ``date``: ``YYYY-MM-DD`` only."""
