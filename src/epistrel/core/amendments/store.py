"""Recording, applying and reverting amendments (FR-REWRITE-9,12, FR-STORE-20,21; #221).

``record_amendment(conn, op, *, kind, parts, relies_on, rationale_record_ids)`` runs in a commit
step of an ``Operation(kind=rewrite_session)`` (no narrative position; the operation bumps the
story's revision). It writes one ``amendments`` row and its ``amendment_parts``, applies each
part, records one ``amendment``-category ``amendment_recorded`` event and the parts' dependency
edges (#220), and writes nothing when any part fails (the transaction rolls back):

- a **proposition part** (``PropositionPartDraft``) writes a position-less fact version through
  #133's ``commit_fact`` (``provenance = authority = author``, ``amendment_part_id`` set,
  valid-time from the part), superseding the key's overlapping current versions. A part on
  ``scheduled`` is 422 ``schedule_not_amendable``: a superseded ``scheduled`` fact does not move
  its event (#198), so the route to move it is named (the owner's decision 4). A relative time
  is 422 ``invalid_amendment_part`` (``unresolved_time``: #223 resolves it first);
- a **record part** (``RecordPartDraft``) corrects one exact record by its table's strategy in
  ``CORRECTABLE_TABLES`` and lives and dies with that record's membership, never with the
  amendment's operation:

  - ``overlay`` (``events``): nothing is rewritten; the corrected readers (``core.views``)
    apply the latest active parts per event (``replace`` merges ``patch_after`` into the
    payload, a ``null`` deleting a key; ``drop`` hides the event), and the transcript row of a
    beat takes the corrected text (a dropped beat's row is emptied: no placeholder);
  - ``version`` (``facts``): a successor version written under the corrected fact's own
    operation and position, so its membership follows that record; ``beliefs`` are corrected
    through their frames (#236) and refused here (``action_not_allowed``);
  - ``in_place`` (``memories.text``, ``utterances``' relations and classification,
    ``state_changes.value``/``delta``/``new_value``): the allow-listed columns rewritten under
    ``epistrel.allow_record_correction`` (migration 0112), the before-image kept in
    ``patch_before``. A memory is re-indexed and re-embedded (#145's sanitize path). A
    ``state_changes`` part applies only to a ``committed`` change of a story-owned,
    non-derived State that is not ``world_clock``, with no outbox row and no prerequisite
    effects (else ``contingent_change``, ``external_effect``, ``derived_state``,
    ``clock_advance``); its State's constraints are re-checked at the corrected value and every
    later active one (``state_constraint``), no later row is rewritten, and ``state_values`` is
    rebuilt for the key.

  A record outside the active timeline is 409 ``record_inactive``; any other failure is 422
  ``invalid_amendment_part`` with the part's ordinal and reason.

``revert_amendment(conn, op, amendment_id, *, by_amendment_id)`` writes the inverse parts
(``inverse_parts``, pure) into the caller's amendment ``by_amendment_id`` (recorded in the same
operation, e.g. ``kind = undo``), marks the original and its parts ``reverted`` and deletes
nothing: every view then equals the pre-amendment view except transaction-time history.

Amendments survive truncation (FR-STORE-21): a truncation moves membership only, and neither
table is a derived record. Log lines carry identifiers and counts only (invariant 3).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType, SimpleNamespace
from typing import Any, Literal

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import edges
from epistrel.core.enums import EventCategory, FactAuthority, Membership, RecordProvenance
from epistrel.core.errors import (
    AmendmentAlreadyReverted,
    AmendmentInvalidPart,
    AmendmentTargetInactive,
    IntentNotRetconnable,
    ScheduleNotAmendable,
    StateNotAmendable,
)
from epistrel.core.events.registry import validate
from epistrel.core.events.types import AmendmentRecorded, AmendmentReverted, TranscriptPatch
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_only, active_operation
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.tables import (
    amendment_parts,
    amendments,
    events,
    facts,
    memories,
    operations,
    scheduled_events,
    state_changes,
    transcript_messages,
    utterances,
)

CORRECTION_SETTING = "epistrel.allow_record_correction"
"""Set to the corrected row's id for its in-place correction (migration 0112's guards)."""
SCHEDULE_ROUTE = "PATCH /stories/{id}/scheduled-events/{event_id}"
PROPOSITION_CHANGES = True
"""Off only under ``epistrel.testing.faults.amendment_proposition_changes_skipped`` (the
negative run of S-ABSENT-FACT M7, #236, which must then fail)."""


class AmendmentKind(StrEnum):
    REWRITE = "rewrite"
    UNDO = "undo"
    REDO = "redo"
    DIRECTED_RESOLUTION = "directed_resolution"


Strategy = Literal["overlay", "version", "in_place"]
Action = Literal["replace", "drop", "relabel", "set_intent"]
INTENT_SETTING = "epistrel.allow_intent_correction"
"""Set to the utterance's id for an explicit intent change (#225; migration 0118's guard)."""


@dataclass(frozen=True, slots=True)
class Correctable:
    """How a table's records are corrected: its strategy, the actions it takes and, for
    ``in_place``, the columns a part may rewrite."""

    strategy: Strategy
    actions: frozenset[str]
    columns: frozenset[str] = frozenset()


CORRECTABLE_TABLES: Mapping[str, Correctable] = MappingProxyType(
    {
        "events": Correctable("overlay", frozenset({"replace", "drop"})),
        "facts": Correctable("version", frozenset({"replace"})),
        "beliefs": Correctable("version", frozenset({"replace", "drop"})),
        "memories": Correctable("in_place", frozenset({"replace"}), frozenset({"text"})),
        "utterances": Correctable(
            "in_place",
            frozenset({"replace", "relabel", "set_intent"}),
            frozenset(
                {
                    "objective_relation",
                    "belief_relation",
                    "classification",
                    "attribution_objective_relation",
                }
            ),
        ),
        "state_changes": Correctable(
            "in_place", frozenset({"replace"}), frozenset({"value", "delta", "new_value"})
        ),
    }
)
"""The tables holding correctable narrative content, by strategy (FR-REWRITE-12). A new table
that holds such content is added here by hand (``test_correctable_tables``)."""

NOT_CORRECTABLE: Mapping[str, str] = MappingProxyType(
    {
        "external_values": "provider-owned values (FR-EXT-8)",
        "external_effect_outbox": "a timeline-independent effect record",
        "outbox_outcomes": "a timeline-independent effect record",
        "reconciliation_marks": "a timeline-independent effect record",
        "state_definitions": "configuration, changed by update_definition",
        "scheduled_events": "moved only by reschedule or cancel (the owner's decision 4)",
    }
)
"""Tables deliberately not correctable, with the reason (#221's replan)."""


@dataclass(frozen=True, slots=True)
class PropositionPartDraft:
    """A correction to a proposition key over a valid-time: ``proposition`` is what is true
    instead; ``valid_from_minutes``/``valid_to_minutes`` clock offsets (``None``: open), or
    ``absolute`` an ISO-8601 partial date it begins at; ``relative`` (unresolved text) is
    refused."""

    proposition: Proposition
    valid_from_minutes: int | None = None
    valid_to_minutes: int | None = None
    absolute: str | None = None
    relative: str | None = None
    timing_uncertain: bool = False
    """#240: undecidable against the anchor and included under ``include_if_possible``."""
    position_bound: Mapping[str, Any] | None = None
    """#240: the author's placement under ``ask_author`` (``{story_start: true}`` or
    ``{relation, turn, variant}``)."""


@dataclass(frozen=True, slots=True)
class RecordPartDraft:
    """A correction to one exact record: its table and id, the action and the patch
    (``replace``/``relabel``: the new column values, or for an event the payload keys)."""

    table: str
    record_id: str
    action: Action = "replace"
    patch: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StatePartDraft:
    """A correction to a story-owned State's value over a valid-time (#236): the State
    ``state`` (its name; ``scope_key`` the character of a per-character State) held ``value``
    instead (a ``set``) from ``valid_from_minutes`` (``None``: since the story began) until
    ``valid_to_minutes`` (``None``: open), clock offsets. A relative correction is converted to
    a ``set`` by #223's parse."""

    state: str
    value: Any
    scope_key: str | None = None
    valid_from_minutes: int | None = None
    valid_to_minutes: int | None = None


PartDraft = PropositionPartDraft | RecordPartDraft | StatePartDraft


@dataclass(frozen=True, slots=True)
class RecordedAmendment:
    amendment_id: str
    part_ids: tuple[str, ...]
    event_id: str
    written_fact_ids: tuple[str, ...] = ()


def _invalid(ordinal: int, reason: str, **details: Any) -> AmendmentInvalidPart:
    return AmendmentInvalidPart(
        f"amendment part {ordinal} cannot apply: {reason}",
        {"ordinal": ordinal, "reason": reason, **details},
    )


async def _set_correction(conn: AsyncConnection, row_id: str) -> None:
    await conn.execute(select(func.set_config(CORRECTION_SETTING, row_id, True)))


async def _apply_transcript(conn: AsyncConnection, patches: Sequence[TranscriptPatch]) -> None:
    """The transcript projection's rows the patches correct, written in the commit (the
    projection's fold applies the same event on a rebuild)."""
    for patch in patches:
        if patch.row is not None:
            continue
        if patch.text is not None:
            await conn.execute(
                update(transcript_messages)
                .where(transcript_messages.c.message_id == patch.message_id)
                .values(text=patch.text)
            )


async def _append_event(
    conn: AsyncConnection, op: Any, story_id: str, type_: str, payload: Any
) -> str:
    """Append ``op``'s ``amendment``-category event (position-less), in its commit, and apply its
    transcript patches."""
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import event_record

    model = validate(type_, payload)
    body = model.model_dump(mode="json")
    last = await conn.scalar(
        select(func.coalesce(func.max(events.c.seq), 0)).where(events.c.story_id == story_id)
    )
    event_id = new_ulid()
    await conn.execute(
        insert(events).values(
            id=event_id,
            story_id=story_id,
            seq=int(last or 0) + 1,
            operation_id=op.operation_id,
            category=EventCategory.AMENDMENT.value,
            narrative_turn=None,
            turn_variant=None,
            principal=op.principal,
            type=type_,
            payload=body,
            provenance="consumer",
            authority="authoritative",
        )
    )
    await index_records(
        conn,
        [
            event_record(
                SimpleNamespace(id=event_id, story_id=story_id, character_id=None, payload=body)
            )
        ],
    )
    await _apply_transcript(conn, getattr(model, "transcript", ()))
    return event_id


# -- proposition parts -------------------------------------------------------------------------


def _valid_spec(draft: PropositionPartDraft) -> dict[str, Any] | None:
    if draft.valid_from_minutes is not None:
        return {"offset_minutes": draft.valid_from_minutes}
    if draft.absolute is not None:
        return {"absolute": draft.absolute}
    return None


async def _scheduled_event(conn: AsyncConnection, story_id: str, subject_id: str) -> str | None:
    found = await conn.scalar(
        select(scheduled_events.c.id)
        .join(facts, facts.c.proposition_id == scheduled_events.c.proposition_id)
        .where(scheduled_events.c.story_id == story_id, facts.c.subject_id == subject_id)
        .limit(1)
    )
    return None if found is None else str(found)


async def _apply_proposition(
    conn: AsyncConnection, op: Any, story_id: str, ordinal: int, part_id: str, draft: Any
) -> dict[str, Any]:
    from epistrel.core.amendments.eligibility import partial_period
    from epistrel.core.canon import facts as canon

    proposition: Proposition = draft.proposition
    if proposition.predicate_id == SCHEDULED:
        event_id = await _scheduled_event(conn, story_id, proposition.subject_id)
        raise ScheduleNotAmendable(
            "a scheduled event's plan is moved by rescheduling it, not by an amendment",
            {"ordinal": ordinal, "event_id": event_id, "route": SCHEDULE_ROUTE},
        )
    if draft.relative is not None:
        raise _invalid(ordinal, "unresolved_time")
    if draft.absolute is not None:
        try:
            partial_period(draft.absolute)
        except ValueError:
            raise _invalid(ordinal, "unresolved_time") from None
    valid = None
    if draft.valid_from_minutes is not None or draft.valid_to_minutes is not None:
        valid = Interval.of(draft.valid_from_minutes, draft.valid_to_minutes)
    from epistrel.core import readset

    # #136's ``proposition_changes`` writer runs as a fact hook (FR-CONC-12: a turn in flight
    # whose key the part touches regenerates); off only under the fault
    # ``epistrel.testing.faults.amendment_proposition_changes_skipped``.
    skipped = not PROPOSITION_CHANGES and readset.record_fact_change in canon.FACT_HOOKS
    if skipped:
        canon.remove_fact_hook(readset.record_fact_change)
    try:
        written = await canon.commit_fact(
            conn,
            story_id,
            proposition,
            provenance=RecordProvenance.AUTHOR,
            authority=FactAuthority.AUTHOR,
            operation=op,
            valid_time=valid,
            amendment_part_id=part_id,
        )
    finally:
        if skipped:
            canon.FACT_HOOKS.insert(0, readset.record_fact_change)
    return {
        "kind": "proposition",
        "subject_id": proposition.subject_id,
        "predicate_id": proposition.predicate_id,
        "qualifiers": dict(proposition.qualifiers) or None,
        "proposition_id": written.proposition_id,
        "valid_from_minutes": draft.valid_from_minutes,
        "valid_to_minutes": draft.valid_to_minutes,
        "valid_spec": _valid_spec(draft),
        "written_fact_id": written.id,
        "superseded_fact_ids": list(written.superseded_ids),
        "timing_uncertain": bool(draft.timing_uncertain),
        "position_bound": None if draft.position_bound is None else dict(draft.position_bound),
    }


# -- State proposition parts (#236) ------------------------------------------------------------


async def _state_definition(conn: AsyncConnection, story_id: str, ordinal: int, name: str) -> Any:
    """The active State ``name``; 422 ``state_not_amendable`` for one an amendment may not
    correct (see ``StateNotAmendable``)."""
    from epistrel.core import states
    from epistrel.core.enums import StateType
    from epistrel.core.errors import StateNotFound
    from epistrel.core.states.model import WORLD_CLOCK, derived_rule

    def refused(reason: str) -> StateNotAmendable:
        return StateNotAmendable(
            f"State {name!r} cannot be amended ({reason})",
            {"ordinal": ordinal, "state": name, "reason": reason},
        )

    try:
        state = await states.definition(conn, story_id, name)
    except StateNotFound:
        try:
            await states.definition(conn, story_id, name, view=states.View.AS_RECORDED)
        except StateNotFound:
            raise _invalid(ordinal, "record_not_found", state=name) from None
        raise refused("discarded") from None
    if state.name == WORLD_CLOCK or state.type is StateType.CLOCK:
        raise refused("clock")
    if derived_rule(state) is not None:
        raise refused("derived")
    if (state.transfer or {}).get("conserved"):
        raise refused("transfer_conserved")
    if str(getattr(state.owner, "value", state.owner)) != "story":
        raise refused("external")
    return state


def amended_checkpoints(state: Any, changes: Sequence[Any], change_id: str | None) -> Any:
    """The first checkpoint at which the folded value of ``changes`` (``ChangeRow``s, the
    amendment change ``change_id`` among them; ``None``: any checkpoint) breaks ``state``'s
    constraints while that change is eligible: ``(at, broken, folded)`` (``at`` the last
    positioned change's position, ``None`` before any), else ``None``. Each checkpoint is the
    fold through one more positioned change, with the amendment changes eligible at its
    story-time (#221's ``eligible``, carried forward from the last known minute;
    ``Undecidable`` is in)."""
    from epistrel.core.amendments.eligibility import PartTiming, eligible
    from epistrel.core.states.model import fold, violation

    positioned = sorted((c for c in changes if c.position is not None), key=lambda c: c.order())
    amended = [c for c in changes if c.position is None]
    latest: int | None = None
    for n in range(len(positioned) + 1):
        if n and positioned[n - 1].valid_time is not None:
            minute = positioned[n - 1].valid_time
            latest = minute if latest is None else max(latest, minute)
        inside = [
            a
            for a in amended
            if eligible(PartTiming(valid_from_minutes=a.valid_time), story_time=latest).folds
        ]
        if change_id is not None and change_id not in {a.id for a in inside}:
            continue
        folded = fold(state.initial_value, [*positioned[:n], *inside]).value
        broken = violation(state, folded)
        if broken is not None:
            at = positioned[n - 1].position if n else None
            return at, broken, folded
    return None


async def _apply_state(
    conn: AsyncConnection, op: Any, story_id: str, ordinal: int, part_id: str, draft: Any
) -> dict[str, Any]:
    """A State part: a position-less ``set`` (``source = author``, ``unconditional``,
    ``committed``) carrying ``amendment_part_id``, checked against the State's constraints at
    every checkpoint where it is eligible (422 ``state_constraint_violation``)."""
    from epistrel.core import states
    from epistrel.core.enums import StateChangeKind, StateChangeStatus
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import state_change_record
    from epistrel.core.errors import EpistrelError, StateConstraintViolation
    from epistrel.core.membership import insert_derived
    from epistrel.core.states.model import ChangeRow, coerce, to_json

    state = await _state_definition(conn, story_id, ordinal, draft.state)
    states._scope_ok(state, draft.scope_key)
    if (
        draft.valid_from_minutes is not None
        and draft.valid_to_minutes is not None
        and draft.valid_to_minutes <= draft.valid_from_minutes
    ):
        raise _invalid(ordinal, "empty_window")
    try:
        value = coerce(state.type, draft.value)
    except EpistrelError:
        raise StateConstraintViolation(
            f"State {state.name!r} takes no such value",
            {
                "ordinal": ordinal,
                "state_id": state.id,
                "scope_key": draft.scope_key,
                "constraint": "type",
                "attempted": _jsonable(draft.value),
            },
        ) from None
    change_id = new_ulid()
    existing = await states._changes(conn, story_id, state, draft.scope_key)
    mine = ChangeRow(
        id=change_id,
        kind=StateChangeKind.SET,
        value=value,
        delta=None,
        status=StateChangeStatus.COMMITTED,
        position=None,
        valid_time=draft.valid_from_minutes,
        valid_to=draft.valid_to_minutes,
    )
    failed = amended_checkpoints(state, [*existing, mine], change_id)
    if failed is not None:
        at, broken, folded = failed
        raise StateConstraintViolation(
            f"the amendment leaves State {state.name!r} outside its constraints",
            {
                "ordinal": ordinal,
                "state_id": state.id,
                "scope_key": draft.scope_key,
                "at_checkpoint": None if at is None else list(at.key()),
                "constraint": broken,
                "attempted": to_json(value),
                "folded": None if folded is None else _jsonable(to_json(folded)),
            },
        )
    row = {
        "id": change_id,
        "story_id": story_id,
        "state_id": state.id,
        "scope_key": draft.scope_key,
        "kind": StateChangeKind.SET.value,
        "value": to_json(value),
        "delta": None,
        "reason": "amendment",
        "source": "author",
        "operation_id": op.operation_id,
        "narrative_turn": None,
        "turn_variant": None,
        "valid_time_minutes": draft.valid_from_minutes,
        "tx_from": await conn.scalar(select(func.transaction_timestamp())),
        "amendment_part_id": part_id,
    }
    await insert_derived(conn, state_changes, [row])
    await index_records(conn, [state_change_record(SimpleNamespace(**row))])
    return {
        "kind": "proposition",
        "state_id": state.id,
        "scope_key": draft.scope_key,
        "valid_from_minutes": draft.valid_from_minutes,
        "valid_to_minutes": draft.valid_to_minutes,
        "valid_spec": None
        if draft.valid_from_minutes is None
        else {"offset_minutes": draft.valid_from_minutes},
        "patch_after": {"value": to_json(value)},
        "written_state_change_id": change_id,
    }


async def _recheck_state(conn: AsyncConnection, story_id: str, state_id: str, scope: Any) -> None:
    """After a revert: the key's fold still meets its constraints at every checkpoint (else
    422 ``state_constraint_violation``; #238 surfaces it as a conflict)."""
    from epistrel.core import states
    from epistrel.core.errors import StateConstraintViolation
    from epistrel.core.states.model import to_json

    by_id = {s.id: s for s in await states.definitions(conn, story_id)}
    state = by_id.get(state_id)
    if state is None:
        return
    failed = amended_checkpoints(state, await states._changes(conn, story_id, state, scope), None)
    if failed is not None:
        at, broken, folded = failed
        raise StateConstraintViolation(
            f"the revert leaves State {state.name!r} outside its constraints",
            {
                "state_id": state_id,
                "scope_key": scope,
                "at_checkpoint": None if at is None else list(at.key()),
                "constraint": broken,
                "folded": None if folded is None else _jsonable(to_json(folded)),
            },
        )


# -- record parts -----------------------------------------------------------------------------


async def _membership(conn: AsyncConnection, table: Any, record_id: str) -> tuple[str, Any] | None:
    row = (await conn.execute(select(table).where(table.c.id == record_id))).one_or_none()
    if row is None:
        return None
    return str(row.story_id), row


async def _operation_active(conn: AsyncConnection, operation_id: str) -> bool:
    found = await conn.scalar(
        select(operations.c.operation_id).where(
            operations.c.operation_id == operation_id, active_operation(operations)
        )
    )
    return found is not None


async def _row_active(conn: AsyncConnection, table: Any, record_id: str) -> bool:
    found = await conn.scalar(select(table.c.id).where(table.c.id == record_id, active_only(table)))
    return found is not None


def _inactive(table: str, record_id: str) -> AmendmentTargetInactive:
    return AmendmentTargetInactive(
        "the record is not in the active timeline",
        {"record_table": table, "record_id": record_id, "membership": "inactive"},
    )


def _check_columns(ordinal: int, spec: Correctable, patch: Mapping[str, Any]) -> None:
    extra = sorted(set(patch) - spec.columns)
    if extra or not patch:
        raise _invalid(ordinal, "column_not_allowed", columns=extra)


def merged_payload(payload: Mapping[str, Any], patch: Mapping[str, Any] | None) -> dict[str, Any]:
    """An overlay's ``replace``: a shallow merge of ``patch`` into ``payload`` (``null``
    deletes a key)."""
    out = dict(payload)
    for key, value in (patch or {}).items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


async def _transcript_patch(
    conn: AsyncConnection, event_id: str, text: str | None, *, dropped: bool
) -> TranscriptPatch | None:
    exists = await conn.scalar(
        select(transcript_messages.c.message_id).where(transcript_messages.c.message_id == event_id)
    )
    if exists is None:
        return None
    if dropped:
        return TranscriptPatch(message_id=event_id, text="")
    return None if text is None else TranscriptPatch(message_id=event_id, text=text)


async def _apply_record(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    ordinal: int,
    draft: RecordPartDraft,
    transcript: list[TranscriptPatch],
) -> dict[str, Any]:
    spec = CORRECTABLE_TABLES.get(draft.table)
    if spec is None:
        raise _invalid(ordinal, "table_not_correctable", record_table=draft.table)
    if draft.action not in spec.actions:
        raise _invalid(ordinal, "action_not_allowed", action=draft.action)
    common = {
        "kind": "record",
        "record_table": draft.table,
        "record_id": draft.record_id,
        "action": draft.action,
        "strategy": spec.strategy,
    }
    if draft.table == "events":
        return await _overlay(conn, story_id, ordinal, draft, transcript, common)
    if draft.table == "facts":
        return await _version_fact(conn, op, story_id, ordinal, draft, common)
    if draft.table == "beliefs":
        # #236 corrects a belief through its lazy frames; here it is refused.
        raise _invalid(ordinal, "action_not_allowed", action=draft.action, record_table="beliefs")
    table = {"memories": memories, "utterances": utterances, "state_changes": state_changes}[
        draft.table
    ]
    found = await _membership(conn, table, draft.record_id)
    if found is None:
        raise _invalid(ordinal, "record_not_found", record_id=draft.record_id)
    owner, row = found
    if owner != story_id:
        raise _invalid(ordinal, "wrong_story", record_id=draft.record_id)
    if not await _row_active(conn, table, draft.record_id):
        raise _inactive(draft.table, draft.record_id)
    if draft.table == "utterances" and draft.action == "set_intent":
        await _set_intent(conn, ordinal, draft, row)
        return {
            **common,
            "patch_before": {"intent": str(row.intent)},
            "patch_after": dict(draft.patch),
        }
    if "intent" in draft.patch:
        # FR-DIRECT-8: only an explicit intent change writes intent (a bug guard).
        raise IntentNotRetconnable(
            "an utterance's intent changes only through an explicit intent change",
            {"ordinal": ordinal, "record_id": draft.record_id},
        )
    _check_columns(ordinal, spec, draft.patch)
    if draft.table == "state_changes":
        await _check_state_change(conn, story_id, ordinal, row, draft.patch)
    before = {k: _jsonable(getattr(row, k)) for k in draft.patch}
    await _write_in_place(conn, story_id, table, draft.record_id, dict(draft.patch), row)
    return {**common, "patch_before": before, "patch_after": dict(draft.patch)}


INTENTS = ("sincere", "deceptive", "joking", "unspecified")


async def _set_intent(conn: AsyncConnection, ordinal: int, draft: Any, row: Any) -> None:
    """An explicit intent change (#225): the utterance's ``intent`` (and its classification,
    when the patch names one) rewritten under ``INTENT_SETTING``."""
    allowed = {"intent", "classification"}
    if set(draft.patch) - allowed or draft.patch.get("intent") not in INTENTS:
        raise _invalid(ordinal, "column_not_allowed", columns=sorted(draft.patch))
    await conn.execute(select(func.set_config(INTENT_SETTING, draft.record_id, True)))
    await conn.execute(
        update(utterances).where(utterances.c.id == draft.record_id).values(**dict(draft.patch))
    )
    await conn.execute(select(func.set_config(INTENT_SETTING, "", True)))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool | list | dict):
        return value
    return json.loads(json.dumps(value, default=str))


async def _write_in_place(
    conn: AsyncConnection,
    story_id: str,
    table: Any,
    record_id: str,
    values: dict[str, Any],
    row: Any,
) -> None:
    from epistrel.core.erasure.index import index_records

    if table is memories:
        values = {**values, "embedding": None, "embedding_model_version": None}
        if row.inputs_hash is not None:
            values["inputs_hash"] = None
    await _set_correction(conn, record_id)
    await conn.execute(update(table).where(table.c.id == record_id).values(**values))
    await _set_correction(conn, "")
    if table is memories:
        from epistrel.core.erasure.records import memory_record
        from epistrel.core.memory import records

        fresh = (
            await conn.execute(
                select(
                    memories.c.id,
                    memories.c.story_id,
                    memories.c.text,
                    memories.c.holder_character_id,
                    memories.c.character_id,
                    memories.c.quarantine_reason,
                ).where(memories.c.id == record_id)
            )
        ).one()
        await index_records(conn, [memory_record(fresh)])
        await records.after_rewrite(conn, story_id, (record_id,))
    if table is state_changes:
        from epistrel.core import states

        await states.refresh(conn, story_id, [(str(row.state_id), row.scope_key)])


async def _check_state_change(
    conn: AsyncConnection, story_id: str, ordinal: int, row: Any, patch: Mapping[str, Any]
) -> None:
    from epistrel.core import states
    from epistrel.core.enums import StateChangeKind
    from epistrel.core.states.model import WORLD_CLOCK, coerce, derived_rule, fold, violation
    from epistrel.core.tables import state_definitions

    name = await conn.scalar(
        select(state_definitions.c.name).where(state_definitions.c.id == row.state_id)
    )
    state = await states.definition(conn, story_id, str(name))
    if state.name == WORLD_CLOCK:
        raise _invalid(ordinal, "clock_advance")
    # A derived State has no rows of its own (it is computed); a rule's own row (an elapsed
    # tick, #192) is computed from the clock, so it is not a narrated change either.
    source = str(getattr(row.source, "value", row.source))
    if derived_rule(state) is not None or source == "rule":
        raise _invalid(ordinal, "derived_state", source=source)
    if str(getattr(row.status, "value", row.status)) != "committed":
        raise _invalid(ordinal, "contingent_change", status=str(row.status))
    contingency = str(getattr(row.contingency, "value", row.contingency))
    owner = str(getattr(state.owner, "value", state.owner))
    if row.prerequisite_effect_ids or contingency != "unconditional" or owner != "story":
        raise _invalid(ordinal, "external_effect")
    existing = await states._changes(conn, story_id, state, row.scope_key)
    corrected = []
    for change in existing:
        if change.id == str(row.id):
            if change.kind is StateChangeKind.SET:
                value = coerce(state.type, patch.get("value", _jsonable(change.value)))
                change = replace(change, value=value)
            elif "delta" in patch:
                delta = coerce(state.type, patch["delta"], field="delta")
                change = replace(change, delta=delta)  # type: ignore[arg-type]
        corrected.append(change)
    ordered = sorted(corrected, key=lambda c: c.order())
    at = _row_key(row)
    for n in range(1, len(ordered) + 1):
        position = ordered[n - 1].position
        if position is None or position.key() < at:
            continue
        broken = violation(state, fold(state.initial_value, ordered[:n]).value)
        if broken is not None:
            raise _invalid(ordinal, "state_constraint", constraint=broken)


def _row_key(row: Any) -> tuple[Any, ...]:
    from epistrel.core.membership import Position

    return Position(
        int(row.narrative_turn or 0),
        int(row.turn_variant or 0),
        int(row.slot_k or 0),
        int(row.beat or 0),
        int(row.step or 0),
    ).key()


async def _overlay(
    conn: AsyncConnection,
    story_id: str,
    ordinal: int,
    draft: RecordPartDraft,
    transcript: list[TranscriptPatch],
    common: dict[str, Any],
) -> dict[str, Any]:
    row = (
        await conn.execute(
            select(events.c.story_id, events.c.operation_id, events.c.type, events.c.payload).where(
                events.c.id == draft.record_id
            )
        )
    ).one_or_none()
    if row is None:
        raise _invalid(ordinal, "record_not_found", record_id=draft.record_id)
    if str(row.story_id) != story_id:
        raise _invalid(ordinal, "wrong_story", record_id=draft.record_id)
    if not await _operation_active(conn, str(row.operation_id)):
        raise _inactive("events", draft.record_id)
    payload = dict(row.payload)
    if draft.action == "replace":
        if not draft.patch:
            raise _invalid(ordinal, "column_not_allowed")
        try:
            validate(str(row.type), merged_payload(payload, draft.patch))
        except Exception:
            raise _invalid(ordinal, "column_not_allowed", record_id=draft.record_id) from None
    before = {k: payload.get(k) for k in draft.patch} if draft.action == "replace" else payload
    text = draft.patch.get("text") if draft.action == "replace" else None
    patch = await _transcript_patch(
        conn,
        draft.record_id,
        text if isinstance(text, str) else None,
        dropped=draft.action == "drop",
    )
    if patch is not None:
        transcript.append(patch)
    return {
        **common,
        "patch_before": before,
        "patch_after": dict(draft.patch) if draft.action == "replace" else None,
    }


async def _version_fact(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    ordinal: int,
    draft: RecordPartDraft,
    common: dict[str, Any],
    *,
    part_id: str | None = None,
) -> dict[str, Any]:
    from epistrel.core.canon import facts as canon
    from epistrel.core.canon.errors import FactInactive, FactNotFound

    try:
        old = (await canon._load(conn, story_id, draft.record_id))[0]
    except FactNotFound:
        raise _invalid(ordinal, "record_not_found", record_id=draft.record_id) from None
    if old.membership is not Membership.ACTIVE:
        raise AmendmentTargetInactive(
            "the record is not in the active timeline",
            {"record_table": "facts", "record_id": old.id, "membership": old.membership.value},
        )
    proposition = draft.patch.get("proposition")
    if not isinstance(proposition, Proposition):
        raise _invalid(ordinal, "column_not_allowed", record_id=old.id)
    shim = SimpleNamespace(
        operation_id=old.operation_id,
        story_id=story_id,
        principal=op.principal,
        narrative_turn=old.narrative_turn,
        turn_variant=old.turn_variant,
    )
    try:
        written = await canon.supersede(
            conn,
            story_id,
            old.id,
            proposition,
            operation=shim,
            authority=FactAuthority.AUTHOR,
            amendment_part_id=part_id,
        )
    except FactInactive:
        raise AmendmentTargetInactive(
            "the record is not in the active timeline",
            {"record_table": "facts", "record_id": old.id, "membership": "inactive"},
        ) from None
    return {
        **common,
        "patch_before": {"proposition": old.proposition.model_dump(mode="json")},
        "patch_after": {"proposition": proposition.model_dump(mode="json")},
        "written_fact_id": written.id,
        "superseded_fact_ids": [old.id],
    }


# -- recording ----------------------------------------------------------------------------------


def _mark_control(op: Any, parts: Sequence[Any]) -> None:
    """An amendment with any record or State part is a control action (#236, FR-CONC-12): the
    commit records ``stories.last_control_revision``, so ordinary turns in flight abandon with
    ``timeline_changed`` (the Lite read set does not see overlays). A proposition-only
    amendment reaches them through ``proposition_changes`` (#133's fact hooks) instead."""
    if getattr(op, "bumps_revision", False) and any(
        not isinstance(p, PropositionPartDraft) for p in parts
    ):
        op.control_action = True


async def record_amendment(
    conn: AsyncConnection,
    op: Any,
    *,
    kind: AmendmentKind | str,
    parts: Sequence[PartDraft],
    relies_on: Sequence[str] = (),
    rationale_record_ids: Sequence[Mapping[str, str]] = (),
    payload: Mapping[str, Any] | None = None,
    undoes: str | None = None,
    redoes: str | None = None,
    session_id: str | None = None,
) -> RecordedAmendment:
    """Record one amendment of ``parts`` in ``op``'s commit (see the module docstring)."""
    story_id = op.story_id
    if story_id is None:
        raise ValueError("an amendment belongs to a story")
    amendment_id = new_ulid()
    tx_time = (await conn.execute(select(func.transaction_timestamp()))).scalar_one()
    await conn.execute(
        insert(amendments).values(
            id=amendment_id,
            story_id=story_id,
            operation_id=op.operation_id,
            tx_time=tx_time,
            author=op.principal,
            kind=AmendmentKind(kind).value,
            payload=dict(payload or {"parts": len(parts), "plan": None}),
            undoes=undoes,
            redoes=redoes,
            session_id=session_id,
            rationale_record_ids=[dict(r) for r in rationale_record_ids],
        )
    )
    part_ids: list[str] = []
    written: list[str] = []
    transcript: list[TranscriptPatch] = []
    state_keys: list[tuple[str, str | None]] = []
    timing: list[dict[str, Any]] = []
    for ordinal, draft in enumerate(parts):
        part_id = new_ulid()
        # The part row first: a version written for it names it (deferred FK either way).
        if isinstance(draft, PropositionPartDraft):
            values = await _apply_proposition(conn, op, story_id, ordinal, part_id, draft)
        elif isinstance(draft, StatePartDraft):
            values = await _apply_state(conn, op, story_id, ordinal, part_id, draft)
            state_keys.append((str(values["state_id"]), draft.scope_key))
        elif draft.table == "facts":
            spec = CORRECTABLE_TABLES["facts"]
            if draft.action not in spec.actions:
                raise _invalid(ordinal, "action_not_allowed", action=draft.action)
            values = await _version_fact(
                conn,
                op,
                story_id,
                ordinal,
                draft,
                {
                    "kind": "record",
                    "record_table": "facts",
                    "record_id": draft.record_id,
                    "action": draft.action,
                    "strategy": spec.strategy,
                },
                part_id=part_id,
            )
        else:
            values = await _apply_record(conn, op, story_id, ordinal, draft, transcript)
        await conn.execute(
            insert(amendment_parts).values(
                id=part_id,
                story_id=story_id,
                amendment_id=amendment_id,
                ordinal=ordinal,
                tx_from=tx_time,
                **values,
            )
        )
        part_ids.append(part_id)
        if values.get("timing_uncertain") or values.get("position_bound"):
            timing.append(
                {
                    "part_id": part_id,
                    "timing_uncertain": bool(values.get("timing_uncertain")),
                    "position_bound": values.get("position_bound"),
                }
            )
        fact_id = values.get("written_fact_id")
        if isinstance(fact_id, str):
            written.append(fact_id)
        await edges.amendment_edges(conn, op, part_id, list(relies_on))
    if state_keys:
        from epistrel.core import states

        # After the part rows: a read folds an amendment change by its part's status.
        await states.refresh(conn, story_id, state_keys)
    _mark_control(op, parts)
    await _index(conn, op, amendment_id)
    event_id = await _append_event(
        conn,
        op,
        story_id,
        "amendment_recorded",
        AmendmentRecorded(
            amendment_id=amendment_id,
            part_ids=tuple(part_ids),
            transcript=tuple(transcript),
            timing=tuple(timing),
        ),
    )
    return RecordedAmendment(amendment_id, tuple(part_ids), event_id, tuple(written))


async def _index(conn: AsyncConnection, op: Any, amendment_id: str) -> None:
    """The amendment and its parts checked against the story's erasures (409
    ``erased_subject_referenced``: parts are rows, not events, so ``Operation._write`` does
    not see them) and put in the subject index (#123), in its transaction."""
    from epistrel.core.erasure import guard
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import (
        amendment_part_record,
        amendment_record,
        character_ids,
        text_fields,
    )

    head = (await conn.execute(select(amendments).where(amendments.c.id == amendment_id))).one()
    parts = list(
        await conn.execute(
            select(amendment_parts).where(amendment_parts.c.amendment_id == amendment_id)
        )
    )

    def content(document: Mapping[str, Any], subject: Any = None) -> Any:
        references = {s for _, s, _ in character_ids(document)}
        if subject is not None:
            references.add(str(subject))
        texts = tuple(v for v in text_fields(document).values() if v)
        return guard.Content(texts, frozenset(references), consumer=True)

    contents = [
        content({"payload": head.payload}),
        *(
            content(
                {
                    "patch_after": p.patch_after,
                    "valid_spec": p.valid_spec,
                    "qualifiers": p.qualifiers,
                },
                p.subject_id,
            )
            for p in parts
        ),
    ]
    await guard.check_content(
        conn, head.story_id, contents, base_revision=op.base_revision, path=__name__
    )
    await index_records(conn, [amendment_record(head), *(amendment_part_record(p) for p in parts)])


# -- reverting ---------------------------------------------------------------------------------


def inverse_parts(parts: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The parts that undo ``parts`` (each an ``amendment_parts`` row as a mapping), in reverse
    order: a record part's ``patch_before`` becomes the inverse's ``patch_after``; a proposition
    part's inverse restores the versions it superseded. Pure (#238's preview uses it)."""
    found: list[dict[str, Any]] = []
    for part in reversed(list(parts)):
        if part["kind"] == "record":
            found.append(
                {
                    "kind": "record",
                    "record_table": part["record_table"],
                    "record_id": part["record_id"],
                    "action": "replace" if part["action"] != "relabel" else "relabel",
                    "strategy": part["strategy"],
                    "patch_before": part.get("patch_after"),
                    "patch_after": part.get("patch_before"),
                    "inverts": part["id"],
                }
            )
        elif part.get("state_id") is not None:
            # A State part's inverse writes nothing: the reverted part's change leaves every
            # fold (``core.states`` reads an amendment change by its part's status).
            found.append(
                {
                    "kind": "proposition",
                    "state_id": part["state_id"],
                    "scope_key": part.get("scope_key"),
                    "valid_from_minutes": part.get("valid_from_minutes"),
                    "valid_to_minutes": part.get("valid_to_minutes"),
                    "valid_spec": part.get("valid_spec"),
                    "patch_before": part.get("patch_after"),
                    "inverts": part["id"],
                }
            )
        else:
            found.append(
                {
                    "kind": "proposition",
                    "subject_id": part["subject_id"],
                    "predicate_id": part["predicate_id"],
                    "qualifiers": part.get("qualifiers"),
                    "valid_from_minutes": part.get("valid_from_minutes"),
                    "valid_to_minutes": part.get("valid_to_minutes"),
                    "valid_spec": part.get("valid_spec"),
                    "restores": list(part.get("superseded_fact_ids") or []),
                    "closes": part.get("written_fact_id"),
                    "inverts": part["id"],
                }
            )
    return found


async def revert_amendment(
    conn: AsyncConnection, op: Any, amendment_id: str, *, by_amendment_id: str
) -> list[str]:
    """Revert ``amendment_id`` into ``by_amendment_id`` (see the module docstring); answers the
    inverse part ids."""
    from epistrel.core.canon import facts as canon

    story_id = op.story_id
    head = (
        await conn.execute(
            select(amendments).where(amendments.c.id == amendment_id).with_for_update()
        )
    ).one_or_none()
    if head is None or str(head.story_id) != story_id:
        raise _invalid(0, "record_not_found", amendment_id=amendment_id)
    if head.status == "reverted":
        raise AmendmentAlreadyReverted(
            "the amendment is already reverted",
            {"amendment_id": amendment_id, "reverted_by": head.reverted_by},
        )
    rows = [
        dict(r._mapping)
        for r in await conn.execute(
            select(amendment_parts)
            .where(amendment_parts.c.amendment_id == amendment_id)
            .order_by(amendment_parts.c.ordinal)
        )
    ]
    start = await conn.scalar(
        select(func.coalesce(func.max(amendment_parts.c.ordinal) + 1, 0)).where(
            amendment_parts.c.amendment_id == by_amendment_id
        )
    )
    now = (await conn.execute(select(func.transaction_timestamp()))).scalar_one()
    transcript: list[TranscriptPatch] = []
    written: list[str] = []
    state_keys: list[tuple[str, str | None]] = []
    if getattr(op, "bumps_revision", False) and any(
        r["kind"] == "record" or r.get("state_id") is not None for r in rows
    ):
        op.control_action = True  # as the original was (``_mark_control``)
    for n, inverse in enumerate(inverse_parts(rows)):
        part_id = new_ulid()
        values: dict[str, Any] = {
            k: v for k, v in inverse.items() if k not in ("inverts", "restores", "closes")
        }
        if inverse.get("state_id") is not None:
            state_keys.append((str(inverse["state_id"]), inverse.get("scope_key")))
        elif inverse["kind"] == "proposition":
            restored: list[str] = []
            closes = inverse.get("closes")
            for fact_id in inverse.get("restores") or []:
                old = (await canon._load(conn, story_id, fact_id))[0]
                made = await canon.commit_fact(
                    conn,
                    story_id,
                    old.proposition,
                    provenance=old.provenance,
                    authority=old.authority,
                    operation=op,
                    valid_time=old.proposition.valid_time,
                    visibility=old.visibility,
                    amendment_part_id=part_id,
                )
                restored.append(made.id)
            if not restored and isinstance(closes, str):
                current = (await canon._load(conn, story_id, closes))[0]
                values["superseded_fact_ids"] = [current.id]
            values["written_fact_id"] = restored[0] if restored else None
            if restored:
                values["superseded_fact_ids"] = [closes] if isinstance(closes, str) else []
        else:
            await _invert_record(conn, op, story_id, inverse, transcript, part_id)
        await conn.execute(
            insert(amendment_parts).values(
                id=part_id,
                story_id=story_id,
                amendment_id=by_amendment_id,
                ordinal=int(start or 0) + n,
                tx_from=now,
                **values,
            )
        )
        written.append(part_id)
    await conn.execute(
        update(amendment_parts)
        .where(amendment_parts.c.amendment_id == amendment_id)
        .values(status="reverted", tx_to=now)
    )
    await conn.execute(
        update(amendments)
        .where(amendments.c.id == amendment_id)
        .values(status="reverted", reverted_by=by_amendment_id)
    )
    if state_keys:
        from epistrel.core import states

        for state_id, scope in state_keys:
            await _recheck_state(conn, story_id, state_id, scope)
        await states.refresh(conn, story_id, state_keys)
    await _index(conn, op, by_amendment_id)
    await _append_event(
        conn,
        op,
        story_id,
        "amendment_reverted",
        AmendmentReverted(
            amendment_id=amendment_id, by=by_amendment_id, transcript=tuple(transcript)
        ),
    )
    return written


async def _invert_record(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    inverse: Mapping[str, Any],
    transcript: list[TranscriptPatch],
    part_id: str,
) -> None:
    table_name = str(inverse["record_table"])
    record_id = str(inverse["record_id"])
    restore = inverse.get("patch_after") or {}
    if table_name == "events":
        row = (
            await conn.execute(
                select(events.c.type, events.c.payload).where(events.c.id == record_id)
            )
        ).one()
        text = dict(row.payload).get("text")
        if isinstance(text, str):
            exists = await conn.scalar(
                select(transcript_messages.c.message_id).where(
                    transcript_messages.c.message_id == record_id
                )
            )
            if exists is not None:
                transcript.append(TranscriptPatch(message_id=record_id, text=text))
        return
    if table_name == "facts":
        from epistrel.core.canon import facts as canon

        proposition = Proposition.model_validate(restore["proposition"])
        current_id = await conn.scalar(
            select(facts.c.id).where(
                facts.c.story_id == story_id,
                facts.c.superseded_by.is_(None),
                facts.c.lineage_id
                == select(facts.c.lineage_id).where(facts.c.id == record_id).scalar_subquery(),
            )
        )
        old = (await canon._load(conn, story_id, str(current_id or record_id)))[0]
        shim = SimpleNamespace(
            operation_id=old.operation_id,
            story_id=story_id,
            principal=op.principal,
            narrative_turn=old.narrative_turn,
            turn_variant=old.turn_variant,
        )
        await canon.supersede(
            conn,
            story_id,
            old.id,
            proposition,
            operation=shim,
            authority=FactAuthority.AUTHOR,
            amendment_part_id=part_id,
        )
        return
    table = {"memories": memories, "utterances": utterances, "state_changes": state_changes}[
        table_name
    ]
    row = (await conn.execute(select(table).where(table.c.id == record_id))).one()
    await _write_in_place(conn, story_id, table, record_id, dict(restore), row)


__all__ = [
    "CORRECTABLE_TABLES",
    "CORRECTION_SETTING",
    "NOT_CORRECTABLE",
    "AmendmentKind",
    "Correctable",
    "PropositionPartDraft",
    "RecordPartDraft",
    "RecordedAmendment",
    "StatePartDraft",
    "amended_checkpoints",
    "inverse_parts",
    "merged_payload",
    "record_amendment",
    "revert_amendment",
]
