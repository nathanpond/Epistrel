"""Undo and redo as checked rewrites (FR-REWRITE-10,11; #238).

Undo is a new amendment, not a bare revert: undoing amendment A opens a rewrite session of kind
``undo`` (``target_amendment_id = A``) whose changes are the inverse of A's active parts, planned
by the story's planner against the canon **as restored**, through the same questions, modes,
preview and final-commit revalidation (#223/#224/#237) as any rewrite.

- **Changes** (``undo_parsed``): each active proposition part of A becomes a change back to the
  value it superseded (``object_id`` the superseded fact's object, ``superseded_object_id`` the
  object A wrote, flagged ``undo``); each State part a change on its key. The planner finds their
  dependents (#220 ``dependents()`` at depth 3 of the key's current facts, A's among them, and
  mentions of the value A wrote); A's record parts plan nothing (the revert restores them).
  The apply writes no part for an ``undo`` change: it reverts A (#221 ``revert_amendment``).
- **Dependent amendments** (FR-REWRITE-11): a later amendment B with a part relying on A's facts
  is a ``dependent_amendment`` conflict whose options are ``cascade_undo`` (recommended; B is
  reverted by the same undo) or ``cancel`` (the whole undo session is cancelled). Contradicted
  records are repaired (``rewrite`` or ``drop``): ``keep`` is not offered for a record that
  contradicts the restored canon.
- **Redo** (``redo_parsed``): a session of kind ``redo`` re-plans A's original parts as new
  changes against the current story, like a new rewrite; its apply records a ``redo``
  amendment (``redoes = A``); A stays ``reverted``.

The checks (``check_undo``/``check_redo``): 404 ``amendment_not_found``; undoing an ``undo``
amendment 422 ``use_redo``; undoing a reverted one 409 ``amendment_already_reverted``; redoing
an active one 409 ``amendment_not_reverted``; redoing one an active amendment already redoes
409 ``amendment_already_redone``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.amendments.read import amendment_row, part_rows
from epistrel.core.amendments.store import (
    PropositionPartDraft,
    RecordPartDraft,
    StatePartDraft,
)
from epistrel.core.errors import (
    AmendmentAlreadyRedone,
    AmendmentAlreadyReverted,
    AmendmentNotReverted,
    UseRedo,
)
from epistrel.core.tables import amendments, state_definitions
from epistrel.orchestrator.rewrite import parse as parsing

UNDO_CONFLICT_OPTIONS = ("cascade_undo", "cancel")
"""A dependent amendment's options in an undo session."""
UNDO_RECORD_OPTIONS = ("rewrite", "drop")
"""A contradicted record's options in an undo session (``keep`` would leave it contradicting
the restored canon)."""


async def check_undo(conn: AsyncConnection, story_id: str, amendment_id: str) -> Any:
    row = await amendment_row(conn, story_id, amendment_id)
    if str(row.kind) == "undo":
        raise UseRedo(
            "an undo is taken back by redoing the amendment it undid",
            {"amendment_id": amendment_id, "undoes": row.undoes},
        )
    if str(row.status) == "reverted":
        raise AmendmentAlreadyReverted(
            "the amendment is already reverted",
            {"amendment_id": amendment_id, "reverted_by": row.reverted_by},
        )
    return row


async def check_redo(conn: AsyncConnection, story_id: str, amendment_id: str) -> Any:
    row = await amendment_row(conn, story_id, amendment_id)
    if str(row.status) != "reverted":
        raise AmendmentNotReverted(
            "only a reverted amendment can be redone", {"amendment_id": amendment_id}
        )
    redone = await conn.scalar(
        select(amendments.c.id).where(
            amendments.c.story_id == story_id,
            amendments.c.redoes == amendment_id,
            amendments.c.status == "active",
        )
    )
    if redone is not None:
        raise AmendmentAlreadyRedone(
            "an active amendment already redoes it",
            {"amendment_id": amendment_id, "redo_id": str(redone)},
        )
    return row


async def _fact(conn: AsyncConnection, story_id: str, fact_id: str | None) -> Any:
    from epistrel.core.canon import facts as canon

    if fact_id is None:
        return None
    found = await canon._load(conn, story_id, str(fact_id))
    return found[0] if found else None


async def _state_name(conn: AsyncConnection, state_id: str) -> str | None:
    found = await conn.scalar(
        select(state_definitions.c.name).where(state_definitions.c.id == state_id)
    )
    return None if found is None else str(found)


async def undo_parsed(conn: AsyncConnection, story_id: str, amendment_id: str) -> parsing.Parsed:
    """The undo's changes: the inverse of the amendment's active parts (see the module
    docstring)."""
    parsed = parsing.Parsed()
    for part in await part_rows(conn, amendment_id):
        if str(part["status"]) != "active":
            continue
        if part.get("state_id") is not None:
            key = f"state:{part['state_id']}:{part.get('scope_key') or ''}"
            summary = {
                "kind": "state",
                "key": key,
                "state_id": str(part["state_id"]),
                "scope_key": part.get("scope_key"),
                "undo": True,
                "part_id": str(part["id"]),
            }
            parsed.changes.append(parsing.Normalized(key, "state", None, summary))
            continue
        if part["kind"] != "proposition" or part.get("subject_id") is None:
            continue
        written = await _fact(conn, story_id, part.get("written_fact_id"))
        superseded = [str(f) for f in part.get("superseded_fact_ids") or []]
        restored = await _fact(conn, story_id, superseded[0] if superseded else None)
        new_object = None if written is None else written.proposition.object_id
        key = parsing.prop_key(str(part["subject_id"]), str(part["predicate_id"]))
        summary = {
            "kind": "proposition",
            "key": key,
            "subject_id": str(part["subject_id"]),
            "predicate_id": str(part["predicate_id"]),
            "object_id": None if restored is None else restored.proposition.object_id,
            "literal": None,
            "polarity": "affirmed",
            "valid_from_minutes": part.get("valid_from_minutes"),
            "valid_to_minutes": part.get("valid_to_minutes"),
            "superseded_object_id": new_object,
            "undo": True,
            "part_id": str(part["id"]),
        }
        parsed.changes.append(parsing.Normalized(key, "proposition", None, summary, new_object))
    return parsed


async def redo_parsed(conn: AsyncConnection, story_id: str, amendment_id: str) -> parsing.Parsed:
    """The redo's changes: the amendment's original parts again, as new changes."""
    from epistrel.core.canon import facts as canon

    parsed = parsing.Parsed()
    for part in await part_rows(conn, amendment_id):
        if part.get("state_id") is not None:
            name = await _state_name(conn, str(part["state_id"]))
            after = dict(part.get("patch_after") or {})
            if name is None or "value" not in after:
                continue
            key = f"state:{part['state_id']}:{part.get('scope_key') or ''}"
            draft = StatePartDraft(
                name,
                after["value"],
                scope_key=part.get("scope_key"),
                valid_from_minutes=part.get("valid_from_minutes"),
                valid_to_minutes=part.get("valid_to_minutes"),
            )
            summary = {
                "kind": "state",
                "key": key,
                "state": name,
                "state_id": str(part["state_id"]),
                "scope_key": part.get("scope_key"),
                "value": after["value"],
                "valid_from_minutes": part.get("valid_from_minutes"),
                "valid_to_minutes": part.get("valid_to_minutes"),
            }
            parsed.changes.append(parsing.Normalized(key, "state", draft, summary))
        elif part["kind"] == "record":
            record = {"table": str(part["record_table"]), "id": str(part["record_id"])}
            key = f"record:{record['table']}:{record['id']}"
            draft_record = RecordPartDraft(
                record["table"],
                record["id"],
                str(part.get("action") or "replace"),  # type: ignore[arg-type]
                dict(part.get("patch_after") or {}),
            )
            summary = {"kind": "record", "key": key, "record": record, "redo": True}
            parsed.changes.append(parsing.Normalized(key, "record", draft_record, summary))
        elif part.get("subject_id") is not None:
            written = await _fact(conn, story_id, part.get("written_fact_id"))
            if written is None:
                continue
            proposition = written.proposition
            current = await canon.canon(
                conn, story_id, subject=proposition.subject_id, predicate=proposition.predicate_id
            )
            superseded = next(
                (f.proposition.object_id for f in current if f.id != written.id), None
            )
            key = parsing.prop_key(proposition.subject_id, proposition.predicate_id)
            summary = {
                "kind": "proposition",
                "key": key,
                "subject_id": proposition.subject_id,
                "predicate_id": proposition.predicate_id,
                "object_id": proposition.object_id,
                "literal": None,
                "polarity": "affirmed",
                "valid_from_minutes": part.get("valid_from_minutes"),
                "valid_to_minutes": part.get("valid_to_minutes"),
                "superseded_object_id": superseded,
            }
            draft_prop = PropositionPartDraft(
                proposition,
                valid_from_minutes=part.get("valid_from_minutes"),
                valid_to_minutes=part.get("valid_to_minutes"),
            )
            parsed.changes.append(
                parsing.Normalized(key, "proposition", draft_prop, summary, superseded)
            )
    return parsed


async def relying_amendments(
    conn: AsyncConnection, conflicts: Sequence[Mapping[str, Any]], known: set[str]
) -> set[str]:
    """The active amendments (other than ``known``) whose parts are among ``conflicts``."""
    from epistrel.core.tables import amendment_parts

    part_ids = [
        str(c["record"]["id"]) for c in conflicts if c["record"]["table"] == "amendment_parts"
    ]
    if not part_ids:
        return set()
    p = amendment_parts.c
    found = await conn.execute(
        select(p.amendment_id).where(p.id.in_(part_ids), p.status == "active").distinct()
    )
    return {str(a) for (a,) in found} - known


def undo_options(irreducible: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An undo's conflicts with their options (see the module docstring)."""
    found = []
    for conflict in irreducible:
        table = conflict["record"]["table"]
        options = UNDO_CONFLICT_OPTIONS if table == "amendment_parts" else UNDO_RECORD_OPTIONS
        found.append({**conflict, "options": list(options)})
    return found


__all__ = [
    "UNDO_CONFLICT_OPTIONS",
    "UNDO_RECORD_OPTIONS",
    "check_redo",
    "check_undo",
    "redo_parsed",
    "relying_amendments",
    "undo_options",
    "undo_parsed",
]
