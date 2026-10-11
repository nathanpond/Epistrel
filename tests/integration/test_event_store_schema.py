"""The migrated event-store schema agrees with the Python vocabularies and allow-lists."""

from __future__ import annotations

import re

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.enums import PG_ENUM_TYPES
from epistrel.core.tables.operations import MUTABLE_OPERATION_COLUMNS
from tests.conftest import alembic_config
from tests.event_support import rows, scalar


async def test_enum_labels_match_python_enums(engine: AsyncEngine) -> None:
    found = await rows(
        engine,
        "SELECT t.typname, e.enumlabel FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid "
        "ORDER BY t.typname, e.enumsortorder",
    )
    labels: dict[str, list[str]] = {}
    for row in found:
        labels.setdefault(row.typname, []).append(row.enumlabel)
    assert labels == {name: [m.value for m in cls] for name, cls in PG_ENUM_TYPES.items()}


async def test_operations_trigger_allow_list_matches_python(engine: AsyncEngine) -> None:
    source: str = await scalar(
        engine, "SELECT prosrc FROM pg_proc WHERE proname = 'epistrel_operations_guard'"
    )
    array = re.search(r"ARRAY\[(.*?)\]", source, re.DOTALL)
    assert array is not None
    assert tuple(re.findall(r"'([a-z_]+)'", array.group(1))) == MUTABLE_OPERATION_COLUMNS


def test_event_store_downgrades_cleanly(scratch_database: str) -> None:
    cfg = alembic_config(scratch_database)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")
