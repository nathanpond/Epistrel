"""Directives: an author's directions to a story (FR-DIRECT-1,2,6; docs/02's
``apply_directive``; #226).

``apply_directive`` runs after the Engine's checks (#125's erasure gate, the ``omniscient``
viewpoint, ``precheck``: 404, 409 ``story_paused``, a stale expected revision) and #56's input
gate on the text. It refuses text that names an erased subject (409
``erased_subject_referenced``), dispatches on the story's pipeline (a Lite story is 409
the Lite forms of FR-LITE-8 for a Lite story, #244: a nudge, or a lasting author fact recorded
at the next slot as its ``directive_applied`` event; one about the past is routed into a Lite
rewrite session, #245) and classifies:

- **ephemeral** (guidance for the next reply: the request's ``kind``, or ``directive_parse``'s
  classification): a ``directives`` row ``steer_queued``. The next structured turn's renders
  carry it as an author direction (``resolution_steering``), and that turn's commit marks it
  consumed; an abandoned or failed turn does not. Nothing else is written; the revision does not
  move.
- **durable, now**: one ``inter_turn`` operation at the next slot ``N⁺k`` (under the story's
  turn slot: it waits for a turn in flight under ``queue``, 423 ``story_busy`` under
  ``reject``) records a ``directive_applied`` control event and applies each change with the
  author's authority: a fact (#145's author path: #140's check, ``commit_fact`` superseding
  conflicting canon from the clock's minute, an author memory), a story-owned State set
  (``check_settable(…, source=author)``, then ``apply_change``: the author is not capped), a clock
  advance (``world_clock.advance``, ``source = author``: forward only, 422 ``clock_backward``) and
  a belief (#160's ``author`` transition citing the directive's event). The revision moves by one.
- **durable, past**, or durable with referents the engine cannot pin down: the whole directive is
  routed into a rewrite session of kind ``directive`` (#223) under the request's modes; nothing
  is written at a slot (REST 202).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.config_changes import read_section
from epistrel.core.enums import (
    BeliefConfidenceLevel,
    FactAuthority,
    Provenance,
    RecordProvenance,
    StateChangeSource,
)
from epistrel.core.errors import (
    AuthorFactLimit,
    DirectiveAmbiguous,
    DirectiveNoChanges,
    DirectiveSplitRequired,
    DirectiveUnparseable,
    SteerBudgetExceeded,
)
from epistrel.core.ids import new_ulid
from epistrel.core.propositions.model import Interval, Polarity, Proposition
from epistrel.core.settings_schema import RewriteSettings
from epistrel.core.tables import directives, events, stories
from epistrel.core.trimmed import trim_as_sent
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    DIRECTIVE_PARSE,
    DirectiveChange,
    DirectiveParseResult,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.rewrite import parse as parsing
from epistrel.orchestrator.rewrite.api import (
    PreviewMode,
    ResolutionMode,
    RewriteRequest,
    RewriteSession,
)

LITE_DIRECTIVES = "lite_directives"
TEXT_MAX = 4_000
PARSE_CALLS = 4
PARSE_DEADLINE_S = 120.0
AUTHOR = "author"
STEER_BUDGET_TOKENS = 500
"""#696: the most tokens the story's unconsumed steers may take (one #170 constraint block)."""
USE_CUSTOM_INSTRUCTIONS = "use_custom_instructions"
"""#696: the hint on a steer the parse read as standing guidance (FR-LITE-3's custom
instructions hold it from now on; a steer applies to one turn)."""


class DirectiveRequest(BaseModel):
    """``POST /stories/{id}/directives``: the author's text, optionally its ``kind`` (which
    overrides the classification), and the modes a rewrite it routes to runs with."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"text": "It starts raining.", "kind": "durable"},
                {"text": "Make this scene tense."},
            ]
        },
    )

    text: Annotated[
        str,
        StringConstraints(min_length=1, max_length=TEXT_MAX, strip_whitespace=True),
        BeforeValidator(trim_as_sent(TEXT_MAX)),
    ]
    kind: Literal["ephemeral", "durable"] | None = None
    resolution_mode: ResolutionMode = "synchronous"
    preview: PreviewMode = "preview_first"


class DirectiveApplied(BaseModel):
    """What a durable directive applied at its slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: list[str] = Field(default_factory=list)
    state_changes: list[str] = Field(default_factory=list)
    beliefs: list[str] = Field(default_factory=list)
    clock: list[str] = Field(default_factory=list)
    scheduled_events: list[str] = Field(default_factory=list)
    outbox_ids: list[str] = Field(default_factory=list)
    """#242: the game-owned changes the directive asked the game to make (``routed_write``)."""
    author_fact: dict[str, str] | None = None
    """#244 (FR-LITE-8): a Lite story's lasting author fact: its id, position and text."""


@dataclass(frozen=True, slots=True)
class DirectiveResult:
    """A directive's answer: its id, classification and disposition, the slot it applied at
    (``"N⁺k"``) and what it applied, or the rewrite session it routed to."""

    directive_id: str
    classification: str
    disposition: str
    position: str | None = None
    applied: DirectiveApplied | None = None
    rewrite_session: RewriteSession | None = None
    steer_text: str | None = None
    timeline_revision: int = 0
    replayed: bool = False
    hints: tuple[str, ...] = ()
    """#696: ``use_custom_instructions`` for standing guidance."""


# -- the checks ---------------------------------------------------------------------------------


async def precheck(db: AsyncEngine, story_id: str, *, expected_revision: int | None) -> None:
    """404, 409 ``story_paused``, a stale expected revision (409 ``revision_conflict``)."""
    from epistrel.core import pause
    from epistrel.core.errors import RevisionConflict, StoryNotFound

    async with db.connect() as conn:
        found = (
            await conn.execute(
                select(stories.c.status, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one_or_none()
        if found is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        await pause.refuse(conn, story_id, found.status)
        if expected_revision is not None and expected_revision != int(found.timeline_revision):
            raise RevisionConflict(
                "the story moved past the expected revision",
                {
                    "current_revision": int(found.timeline_revision),
                    "attempts": 0,
                    "reason": "expected_revision",
                },
            )


async def require_pipeline(db: AsyncEngine, story_id: str) -> str:
    """The story's pipeline (#244: a Lite story takes directives in their Lite forms)."""
    from epistrel.orchestrator.rewrite.session import pipeline_of

    async with db.connect() as conn:
        return str(await pipeline_of(conn, story_id))


async def _guard(conn: AsyncConnection, story_id: str, text: str) -> None:
    from epistrel.core.erasure import guard

    await guard.check_content(
        conn,
        story_id,
        [guard.Content((text,), frozenset(), consumer=True)],
        base_revision=None,
        path=__name__,
    )


async def _parse(models: ModelAccessLayer, story: parsing.Story, text: str) -> DirectiveParseResult:
    found = await models.structured_call(
        DIRECTIVE_PARSE.name,
        DirectiveParseResult,
        parsing.summary_inputs(story, text, []),
        params=ModelParams(temperature=0.0),
        budget=TurnBudget(max_calls=PARSE_CALLS, deadline_s=PARSE_DEADLINE_S),
    )
    if isinstance(found, Uncertain):
        raise DirectiveUnparseable(
            "the directive could not be parsed",
            {"reason": str(found.reason), "attempts": found.attempts},
        )
    return found


async def _index(conn: AsyncConnection, directive_id: str) -> None:
    from epistrel.core.erasure.index import index_records
    from epistrel.core.erasure.records import directive_record

    row = (await conn.execute(select(directives).where(directives.c.id == directive_id))).one()
    await index_records(conn, [directive_record(row)])


# -- normalizing durable changes ------------------------------------------------------------------


@dataclass
class _Normalized:
    facts: list[Proposition] = field(default_factory=list)
    states: list[Any] = field(default_factory=list)
    clock: list[dict[str, Any]] = field(default_factory=list)
    beliefs: list[tuple[str, Proposition, BeliefConfidenceLevel]] = field(default_factory=list)
    events: list[tuple[str, Any]] = field(default_factory=list)
    """#696: ``("create", ScheduledEventCreate)``, ``("reschedule", (event_id, planned))`` or
    ``("cancel", event_id)``, through ``core.world_clock.scheduled`` with ``owner = author``."""
    ambiguous: bool = False
    past: bool = False


async def _normalize(
    conn: AsyncConnection, story: parsing.Story, changes: Sequence[DirectiveChange]
) -> _Normalized:
    """The durable changes, normalized against the story; ``ambiguous`` when a name needs a
    question (the directive then routes to a rewrite, whose clarifications ask it); ``past``
    when a change's valid start precedes the clock's minute."""
    from epistrel.core.states.api import consumer_change

    ctx = parsing._Context(story, {}, parsing.Parsed())
    found = _Normalized()
    now = story.now_minutes

    def entity(name: str | None) -> str | None:
        return None if name is None else parsing._entity(ctx, name, "directive")

    for change in changes:
        if change.kind in ("proposition", "belief"):
            if not change.subject or not change.predicate:
                continue
            predicate = parsing._predicate(ctx, change.predicate)
            subject = entity(change.subject)
            obj = entity(change.object) if change.object is not None else None
            if (
                predicate is None
                or subject is None
                or (change.object is not None and obj is None)
                or str(predicate).startswith("register:")
                or str(subject).startswith("new:")
                or str(obj or "").startswith("new:")
            ):
                found.ambiguous = True
                continue
            placed, start = parsing._time(ctx, change.valid.from_, "directive")
            if not placed:
                found.ambiguous = True
                continue
            if change.kind == "proposition" and start is not None and now is not None:
                # A fact about before now is a change to the past (a belief about the past is
                # held from now: the author directs what the character believes now).
                found.past = found.past or start < now
            valid_from = start if start is not None else now
            proposition = Proposition(
                subject_id=subject,
                predicate_id=predicate,
                object_id=obj,
                literal=None
                if change.literal is None or obj is not None
                else parsing._literal(change.literal),
                polarity=Polarity.NEGATED if change.polarity == "negated" else Polarity.AFFIRMED,
                qualifiers=dict(change.qualifiers or {}),
                valid_time=Interval.of(valid_from, None),
            )
            if change.kind == "proposition":
                found.facts.append(proposition)
            else:
                holder = entity(change.holder)
                if holder is None or str(holder).startswith("new:"):
                    found.ambiguous = True
                    continue
                level = BeliefConfidenceLevel(change.confidence or "believed")
                found.beliefs.append((holder, proposition, level))
        elif change.kind == "state":
            if not change.state:
                continue
            definition = next(
                (
                    s
                    for s in story.states
                    if s.name.casefold() == change.state.casefold()
                    or change.state.casefold() in {a.casefold() for a in s.aliases}
                ),
                None,
            )
            if definition is None:
                found.ambiguous = True
                continue
            scope = None
            if change.scope:
                scope = entity(change.scope)
                if scope is None:
                    found.ambiguous = True
                    continue
            set_to = consumer_change(
                definition.name,
                value=change.value if change.delta is None else None,
                delta=change.delta,
                scope_key=scope,
                reason="author directive",
            )
            found.states.append(dataclasses.replace(set_to, source=StateChangeSource.AUTHOR))
        elif change.kind == "clock":
            found.clock.append({"minutes": change.minutes, "to": change.to})
        elif change.kind in ("event", "reschedule", "cancel_event"):
            made = await _event_change(conn, story.story_id, change, entity)
            if made is None:
                found.ambiguous = True
            else:
                found.events.append(made)
    if ctx.parsed.questions:
        found.ambiguous = True
    return found


async def _event_change(
    conn: AsyncConnection, story_id: str, change: DirectiveChange, entity: Any
) -> tuple[str, Any] | None:
    """#696: a scheduled-event change, resolved like #196's API: a new event's participants
    (characters) and place (a place entity) by name, an existing event by its name among the
    story's open events (422 ``directive_ambiguous {candidates}`` when the name is not exactly
    one); ``None`` when a name needs a question (the directive routes to a rewrite)."""
    from epistrel.core.tables.propositions import entities
    from epistrel.core.world_clock import stops
    from epistrel.core.world_clock.scheduled import ScheduledEventCreate, event_names

    if change.kind == "event":
        if not change.name or change.planned is None:
            return None
        participants: list[str] = []
        for name in change.participants:
            found = entity(name)
            if found is None or str(found).startswith("new:"):
                return None
            character = await conn.scalar(
                select(entities.c.character_id).where(entities.c.id == found)
            )
            if character is None:
                return None
            participants.append(str(character))
        place = None
        if change.place:
            place = entity(change.place)
            if place is None or str(place).startswith("new:"):
                return None
        return (
            "create",
            ScheduledEventCreate(
                proposition=change.name,
                planned=change.planned,
                participants=participants,
                place=place,
                owner="author",
            ),
        )
    if not change.event:
        return None
    open_events = [e for e in await stops.events_at(conn, story_id) if e.is_open]
    names = await event_names(conn, [e.event_id for e in open_events])
    wanted = change.event.strip().casefold()
    matches = [e for e in open_events if names.get(e.event_id, "").casefold() == wanted]
    if len(matches) != 1:
        raise DirectiveAmbiguous(
            "the directive names no single open scheduled event",
            {"candidates": sorted(names.get(e.event_id, "") for e in open_events)[:20]},
        )
    if change.kind == "cancel_event":
        return ("cancel", matches[0].event_id)
    if change.planned is None:
        return None
    return ("reschedule", (matches[0].event_id, change.planned))


async def _steer_tokens(conn: AsyncConnection, story_id: str, steer: str) -> int:
    """#696: the tokens the story's unconsumed steers would take with ``steer`` queued
    (#53's estimator, one line each)."""
    from epistrel.core.summary import estimate_tokens

    queued = await queued_steers(conn, story_id)
    return sum(estimate_tokens(text) for _, text in queued) + estimate_tokens(steer)


async def expire_steers(db: AsyncEngine) -> int:
    """#696: unconsumed steers older than ``directive_steer_ttl_minutes`` are dropped
    (disposition ``expired``) under the deployment's setting; how many."""
    from sqlalchemy import func

    async with db.begin() as conn:
        settings = await read_section(conn, RewriteSettings)
        d = directives.c
        done = await conn.execute(
            update(directives)
            .where(
                d.disposition == "steer_queued",
                d.consumed_by_operation_id.is_(None),
                d.created_at
                < func.now()
                - func.make_interval(0, 0, 0, 0, 0, int(settings.directive_steer_ttl_minutes)),
            )
            .values(disposition="expired")
            .returning(d.id)
        )
        return len(list(done))


# -- applying ----------------------------------------------------------------------------------


async def _apply_now(
    db: AsyncEngine,
    story_id: str,
    directive_id: str,
    request: DirectiveRequest,
    normalized: _Normalized,
    payload: list[dict[str, Any]],
    *,
    principal: str,
    routed: Sequence[tuple[Any, Any]] = (),
    lite_fact: str | None = None,
    fact_limit: int | None = None,
) -> tuple[DirectiveApplied, str, int]:
    """One inter-turn slot operation applying every change (see the module docstring); #242:
    each ``routed`` change of a game-owned value is written as an ``intended`` outbox row the
    provider applies (its dispatch enqueued in the same commit), never a ``state_changes``
    row."""
    from epistrel.core import states, world_clock
    from epistrel.core.canon import facts as canon
    from epistrel.core.epistemic import transitions
    from epistrel.core.membership import Position
    from epistrel.core.mutation import (
        _author_memory,
        _check,
        _retire_author_memories,
        require_canon,
    )
    from epistrel.core.states.api import check_settable, refused, turn_slot
    from epistrel.core.states.model import Violation
    from epistrel.core.world_clock.service import ClockAdvanceRequest
    from epistrel.core.world_clock.service import _precheck as clock_precheck
    from epistrel.core.world_clock.slots import open_inter_turn, slot_position

    settings = await require_canon(db, story_id) if normalized.facts else None
    targets: list[tuple[int | None, int | None]] = []
    if normalized.clock:
        async with db.connect() as conn:
            for advance in normalized.clock:
                request_ = ClockAdvanceRequest(
                    minutes=advance["minutes"], to=advance["to"], reason="author directive"
                )
                placed = await clock_precheck(conn, story_id, request_)
                targets.append((advance["minutes"], placed))
    from epistrel.core.enums import ScheduledStatus
    from epistrel.core.world_clock import scheduled, stops

    planned_events: list[tuple[str, Any, Any, Any]] = []
    if normalized.events:
        async with db.connect() as conn:
            for kind, change in normalized.events:
                event_offset: int | None
                event_placed: Any
                if kind == "create":
                    event_offset, event_placed = await scheduled.prepare_create(
                        conn, story_id, change
                    )
                elif kind == "reschedule":
                    event_offset, event_placed = await scheduled.prepare_change(
                        conn, story_id, change[0], change[1]
                    )
                else:
                    event_offset, event_placed = await scheduled.prepare_change(
                        conn, story_id, change, None
                    )
                planned_events.append((kind, change, event_offset, event_placed))
    applied: dict[str, list[str]] = {
        "facts": [],
        "state_changes": [],
        "beliefs": [],
        "clock": [],
        "scheduled_events": [],
        "outbox_ids": [],
    }

    async def step(conn: AsyncConnection) -> None:
        position = slot_position(op)
        if lite_fact and fact_limit is not None:
            # #699: the cap, under the story lock (concurrent requests cannot both pass it).
            active = await lite_fact_count(conn, story_id, excluding=op.operation_id)
            if active >= fact_limit:
                raise AuthorFactLimit(
                    "the story holds the most lasting author facts it may",
                    {"active": active, "max": fact_limit},
                )
        if routed:
            applied["outbox_ids"] = await _route_writes(
                conn, op, story_id, directive_id, routed, position.slot_k, principal=principal
            )
        event_id = await conn.scalar(
            select(events.c.id).where(events.c.operation_id == op.operation_id)
        )
        for proposition in normalized.facts:
            await _check(conn, story_id, proposition)
            fact = await canon.commit_fact(
                conn,
                story_id,
                proposition,
                provenance=RecordProvenance.AUTHOR,
                authority=FactAuthority.AUTHOR,
                operation=op,
                valid_time=proposition.valid_time,
            )
            await _retire_author_memories(conn, story_id, fact.superseded_ids)
            assert settings is not None  # noqa: S101 - read when there are facts
            await _author_memory(conn, op, fact, settings)
            applied["facts"].append(fact.id)
        for change in normalized.states:
            await check_settable(conn, story_id, change.state, source=AUTHOR)
            made = await states.apply_change(
                conn, op, dataclasses.replace(change, position=position)
            )
            if isinstance(made, Violation):
                raise refused(change.state, made)
            applied["state_changes"].append(made.change_id)
        for minutes, to_offset in targets:
            moved = await world_clock.advance(
                conn,
                op,
                story_id,
                minutes=minutes,
                to_offset=to_offset,
                source=StateChangeSource.AUTHOR,
                position=position,
                reason="author directive",
            )
            applied["clock"].append(moved.change_id)
        for holder, proposition, level in normalized.beliefs:
            held = await transitions.transition(
                conn,
                op,
                holder=holder,
                kind=transitions.Kind.AUTHOR,
                proposition=proposition,
                antecedent=transitions.Antecedent.record("events", str(event_id)),
                position=Position(
                    position.turn, position.variant, position.slot_k, position.beat, 1
                ),
                confidence=transitions.Confidence(level),
            )
            if held.belief is not None:
                applied["beliefs"].append(held.belief.id)
        for kind, change, offset, placed in planned_events:
            # #696: scheduled-event changes through #196's writers, owner ``author``.
            if kind == "create":
                event = await scheduled.create_in(conn, op, story_id, change, offset, placed)
            else:
                event = await scheduled.change_in(
                    conn,
                    op,
                    story_id,
                    change[0] if kind == "reschedule" else change,
                    status=ScheduledStatus.PENDING
                    if kind == "reschedule"
                    else ScheduledStatus.CANCELLED,
                    cause=stops.RESCHEDULED if kind == "reschedule" else stops.CANCELLED,
                    offset=offset,
                    placed=placed,
                )
            applied["scheduled_events"].append(event.event_id)
        await conn.execute(
            insert(directives).values(
                id=directive_id,
                story_id=story_id,
                principal=principal,
                text=request.text,
                kind="durable",
                classified_by="request" if request.kind is not None else "model",
                disposition="routed_write" if routed else "applied",
                temporal_scope="now",
                changes=payload,
                operation_id=op.operation_id,
            )
        )
        await _index(conn, directive_id)

    operation_id = new_ulid()
    async with turn_slot(db, story_id, operation_id):
        op = open_inter_turn(
            db, story_id, source=Provenance.CONSUMER, principal=principal, operation_id=operation_id
        )
        op.holds_turn_slot = True
        async with op:
            op.append(
                "directive_applied",
                {
                    "directive_id": directive_id,
                    "text": request.text if lite_fact is None else lite_fact,
                    "changes": payload,
                    **({"lite": True} if lite_fact else {}),
                },
                provenance=Provenance.CONSUMER,
            )
            op.add_commit_step(step)
            await op.commit()
    assert op.timeline_revision is not None  # noqa: S101 - a committed story operation
    label = f"{op.narrative_turn}⁺{op.slot_k}"
    return DirectiveApplied(**applied), label, int(op.timeline_revision)


async def _route_writes(
    conn: AsyncConnection,
    op: Any,
    story_id: str,
    directive_id: str,
    routed: Sequence[tuple[Any, Any]],
    slot_k: int,
    *,
    principal: str,
) -> list[str]:
    """#242 (FR-EXT-10): the game-owned changes as ``intended`` outbox rows (the author's
    authorization recorded, #214), each dispatched like any effect; their ids."""
    from epistrel.core import outbox as core_outbox
    from epistrel.core import states
    from epistrel.core.errors import StateUnpermitted
    from epistrel.core.states.api import sources_admit
    from epistrel.core.states.model import to_json
    from epistrel.orchestrator.jobs.handlers import outbox as dispatcher

    rows: list[core_outbox.Intended] = []
    for change, judged in routed:
        definition = await states.definition(conn, story_id, change.state)
        if not sources_admit(definition, AUTHOR):
            raise StateUnpermitted(
                f"State {change.state!r} does not admit changes from the author",
                {"state": change.state, "reason": "source", "source": AUTHOR},
            )
        effect: dict[str, Any] = {
            "kind": "change",
            "state": judged.state,
            "reason": "author directive",
            "source": "author",
            "directive_id": directive_id,
            "requested_by": principal,
            "authorized": {"state": judged.state, "source": "author"},
        }
        if change.delta is not None:
            effect["delta"] = float(change.delta)
        else:
            effect["value"] = to_json(change.value)
        rows.append(
            core_outbox.Intended(
                id=new_ulid(),
                provider_name=judged.provider,
                value_name=judged.value_name,
                scope_key=judged.scope_key,
                effect=effect,
                beat=0,
                slot_k=slot_k,
            )
        )
    await core_outbox.write_intended(conn, op, rows)
    for row in rows:
        await dispatcher.enqueue(conn, row.id, story_id)
    return [r.id for r in rows]


async def apply_directive(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    request: DirectiveRequest,
    *,
    principal: str,
) -> DirectiveResult:
    """Classify and apply a directive (see the module docstring)."""
    from epistrel.orchestrator.rewrite import session as rewrite_session

    async with db.connect() as conn:
        await _guard(conn, story_id, request.text)
        settings = await read_section(conn, RewriteSettings)
        from epistrel.core.propositions.registry import load

        story = await parsing.load_story(
            conn, story_id, await load(conn, story_id), margin=settings.rewrite_ambiguity_margin
        )
    if request.kind == "ephemeral":
        # #696 (Rule 1): the request's text (up to ``TEXT_MAX``) is the steer as sent; the
        # parse schema's 2,000-character bound is for a model's answer, and building it here
        # failed a long steer with a 500 before the steer budget could refuse it.
        result = DirectiveParseResult.model_construct(
            classification="ephemeral", steer_text=request.text
        )
    else:
        result = await _parse(models, story, request.text)
    classification = request.kind or result.classification
    directive_id = new_ulid()
    payload = [c.model_dump(mode="json", by_alias=True) for c in result.changes]
    lite = await require_pipeline(db, story_id) != "structured"
    if lite and classification != "ephemeral" and result.temporal_scope != "past":
        # #244 (FR-LITE-8): a lasting author fact (a past one is a Lite rewrite, #245, routed
        # below like a structured story's).
        from epistrel.core import world_clock

        clock = [c for c in result.changes if c.kind == "clock"]
        async with db.connect() as conn:
            clock_on = await world_clock.clock_level(conn, story_id) is not None
        # #699: with the prompt-only clock on, the parse's clock advance moves it in the same
        # slot (422 ``clock_backward``/``clock_unanchored``, nothing written); off, it is
        # dropped and the text is the fact.
        advances = [{"minutes": c.minutes, "to": c.to} for c in clock] if clock and clock_on else []
        time_only = (
            bool(advances)
            and not (result.steer_text or "").strip()
            and all(c.kind == "clock" for c in result.changes)
        )
        fact = "" if time_only else (result.steer_text or "").strip() or request.text
        noted, at, moved = await _apply_now(
            db,
            story_id,
            directive_id,
            request.model_copy(update={"text": fact}) if fact else request,
            _Normalized(clock=advances),
            payload,
            principal=principal,
            lite_fact=fact,
            fact_limit=settings.lite_author_facts_max,
        )
        return DirectiveResult(
            directive_id,
            "durable",
            "applied",
            position=at,
            applied=noted.model_copy(
                update={
                    "author_fact": {"id": directive_id, "position": at, "text": fact}
                    if fact
                    else None
                }
            ),
            timeline_revision=moved,
            hints=(USE_CUSTOM_INSTRUCTIONS,) if result.standing else (),
        )
    if classification == "ephemeral":
        steer = result.steer_text.strip() or request.text
        async with db.begin() as conn:
            tokens = await _steer_tokens(conn, story_id, steer)
            if tokens > STEER_BUDGET_TOKENS:
                raise SteerBudgetExceeded(
                    "the story's queued steers would pass their token budget",
                    {"tokens": tokens, "max": STEER_BUDGET_TOKENS},
                )
            await conn.execute(
                insert(directives).values(
                    id=directive_id,
                    story_id=story_id,
                    principal=principal,
                    text=request.text,
                    kind="ephemeral",
                    classified_by="request" if request.kind is not None else "model",
                    disposition="steer_queued",
                    temporal_scope="now",
                    changes=[{"steer_text": steer}],
                )
            )
            await _index(conn, directive_id)
            revision = await conn.scalar(
                select(stories.c.timeline_revision).where(stories.c.id == story_id)
            )
        return DirectiveResult(
            directive_id,
            "ephemeral",
            "steer_queued",
            steer_text=steer,
            timeline_revision=int(revision or 0),
            hints=(USE_CUSTOM_INSTRUCTIONS,) if result.standing else (),
        )
    async with db.connect() as conn:
        normalized = await _normalize(conn, story, result.changes)
    if (normalized.clock or normalized.states or normalized.events) and (
        result.temporal_scope == "past" or normalized.past
    ):
        # #696: a rewrite cannot advance the clock, set a value now or move an event: the
        # author sends the change to the past and the change to now apart.
        raise DirectiveSplitRequired(
            "the directive mixes a change to now with a change to the past",
            {
                "now": [
                    k
                    for k, v in (
                        ("clock", normalized.clock),
                        ("state", normalized.states),
                        ("scheduled_event", normalized.events),
                    )
                    if v
                ]
            },
        )
    if result.temporal_scope == "past" or normalized.past or normalized.ambiguous:
        routed = await rewrite_session.open_session(
            db,
            models,
            story_id,
            RewriteRequest(
                instruction=request.text,
                resolution_mode=request.resolution_mode,
                preview=request.preview,
            ),
            principal=principal,
            kind="directive",
        )
        async with db.begin() as conn:
            await conn.execute(
                insert(directives).values(
                    id=directive_id,
                    story_id=story_id,
                    principal=principal,
                    text=request.text,
                    kind="durable",
                    classified_by="request" if request.kind is not None else "model",
                    disposition="routed_to_rewrite",
                    temporal_scope="past",
                    changes=payload,
                    rewrite_session_id=routed.session.id,
                )
            )
            await _index(conn, directive_id)
        return DirectiveResult(
            directive_id,
            "durable",
            "routed_to_rewrite",
            rewrite_session=routed.session,
            timeline_revision=routed.timeline_revision,
        )
    if not (
        normalized.facts
        or normalized.states
        or normalized.clock
        or normalized.beliefs
        or normalized.events
    ):
        raise DirectiveNoChanges(
            "the directive asks for no change", {"text_length": len(request.text)}
        )
    from epistrel.orchestrator import external_guard

    async with db.connect() as conn:
        judged = await external_guard.classify(conn, story_id, normalized.states)
        facts_judged = await external_guard.classify_facts(conn, story_id, normalized.facts)
    refused = [v for _, v in [*judged, *facts_judged] if v.action == "refuse"]
    if refused:
        # #242: a game-owned value on a read-only provider: the whole directive is refused.
        raise external_guard.refusal(refused)
    writes = [(c, v) for c, v in judged if v.action == "route"]
    if writes:
        normalized = dataclasses.replace(
            normalized, states=[c for c in normalized.states if all(c is not r for r, _ in writes)]
        )
    applied, label, revision = await _apply_now(
        db,
        story_id,
        directive_id,
        request,
        normalized,
        payload,
        principal=principal,
        routed=writes,
    )
    return DirectiveResult(
        directive_id,
        "durable",
        "routed_write" if applied.outbox_ids else "applied",
        position=label,
        applied=applied,
        timeline_revision=revision,
    )


# -- the steer (ephemeral directives) ------------------------------------------------------------


async def queued_steers(conn: AsyncConnection, story_id: str) -> list[tuple[str, str]]:
    """The story's unconsumed ephemeral directives, oldest first: ``(id, steer text)``."""
    d = directives.c
    found = await conn.execute(
        select(d.id, d.changes, d.text)
        .where(
            d.story_id == story_id,
            d.disposition == "steer_queued",
            d.consumed_by_operation_id.is_(None),
        )
        .order_by(d.created_at, d.id)
    )
    steers: list[tuple[str, str]] = []
    for row in found:
        changes = list(row.changes or [])
        text = changes[0].get("steer_text") if changes and isinstance(changes[0], Mapping) else None
        steers.append((str(row.id), str(text or row.text)))
    return steers


async def consume_steers(conn: AsyncConnection, ids: Sequence[str], operation_id: str) -> None:
    """Mark the steers a committing turn carried consumed (in its commit)."""
    if not ids:
        return
    await conn.execute(
        update(directives)
        .where(directives.c.id.in_(list(ids)), directives.c.consumed_by_operation_id.is_(None))
        .values(consumed_by_operation_id=operation_id)
    )


async def record_directed(db: AsyncEngine, story_id: str, text: str, *, principal: str) -> str:
    """#244: a Lite directed regeneration's ``directives`` row (``directed_regeneration``; the
    text is a nudge for that regeneration only, no binding and no reconciliation)."""
    directive_id = new_ulid()
    async with db.begin() as conn:
        await conn.execute(
            insert(directives).values(
                id=directive_id,
                story_id=story_id,
                principal=principal,
                text=text,
                kind="ephemeral",
                classified_by="request",
                disposition="directed_regeneration",
                temporal_scope="now",
                changes=[{"steer_text": text}],
            )
        )
        await _index(conn, directive_id)
    return directive_id


async def author_facts(conn: AsyncConnection, story_id: str, before_turn: int) -> list[str]:
    """#244 (FR-LITE-8): the lasting author facts a Lite prompt carries, in narrative order:
    the text of every ``directive_applied`` event on the active chain at a slot before
    ``before_turn`` (a Lite fact, or a structured story's durable directive after a
    downgrade). Rewind, truncation, swap and fork follow the event's operation."""
    from epistrel.core.membership import active_operation
    from epistrel.core.tables import operations

    o, e = operations.c, events.c
    rows = await conn.execute(
        select(o.narrative_turn, e.payload)
        .join(operations, o.operation_id == e.operation_id)
        .where(
            e.story_id == story_id,
            e.type == "directive_applied",
            active_operation(operations),
            o.narrative_turn < before_turn,
        )
        .order_by(o.narrative_turn, o.slot_k, e.seq)
    )
    return [
        f"(after turn {int(r.narrative_turn)}) {str(dict(r.payload or {}).get('text', '')).strip()}"
        for r in rows
        if str(dict(r.payload or {}).get("text", "")).strip()
    ]


async def lite_fact_count(
    conn: AsyncConnection, story_id: str, *, excluding: str | None = None
) -> int:
    """#699: the story's active Lite lasting author facts (``directive_applied`` events marked
    ``lite`` whose slot operation is on the active chain), ``excluding`` an operation's own
    (the slot being written, whose event is already staged in its transaction)."""
    from sqlalchemy import func

    from epistrel.core.membership import active_operation
    from epistrel.core.tables import operations

    o, e = operations.c, events.c
    found = await conn.scalar(
        select(func.count())
        .select_from(events.join(operations, o.operation_id == e.operation_id))
        .where(
            e.story_id == story_id,
            e.type == "directive_applied",
            e.payload["lite"].as_boolean().is_(True),
            active_operation(operations),
            *([e.operation_id != excluding] if excluding is not None else []),
        )
    )
    return int(found or 0)


async def lite_facts(conn: AsyncConnection, story_id: str) -> list[tuple[str, int, int, str]]:
    """#699: the active Lite lasting author facts in narrative order: ``(event id, turn,
    slot, text)``."""
    from epistrel.core.membership import active_operation
    from epistrel.core.tables import operations

    o, e = operations.c, events.c
    rows = await conn.execute(
        select(e.id, o.narrative_turn, o.slot_k, e.payload)
        .join(operations, o.operation_id == e.operation_id)
        .where(
            e.story_id == story_id,
            e.type == "directive_applied",
            e.payload["lite"].as_boolean().is_(True),
            active_operation(operations),
        )
        .order_by(o.narrative_turn, o.slot_k, e.seq)
    )
    return [
        (str(r.id), int(r.narrative_turn or 0), int(r.slot_k or 0), str(r.payload.get("text")))
        for r in rows
        if str(dict(r.payload or {}).get("text", "")).strip()
    ]


async def kept_author_facts(conn: AsyncConnection, story_id: str, before_turn: int) -> str:
    """#699: under the structured pipeline, the Lite lasting author facts an upgrade kept as
    text (no completed backfill converted them), set before ``before_turn``: the narrator's
    "Author facts" block (a character learns them only through its context, NFR-PRIV-4)."""
    from epistrel.core.tables import story_backfills

    facts = [f for f in await lite_facts(conn, story_id) if f[1] < before_turn]
    if not facts:
        return ""
    converted: set[str] = set()
    for (report,) in await conn.execute(
        select(story_backfills.c.report).where(
            story_backfills.c.story_id == story_id, story_backfills.c.status == "completed"
        )
    ):
        converted.update(str(i) for i in dict(report or {}).get("author_facts", {}).get("ids", []))
    kept = [
        f"(after turn {turn}) {text.strip()}" for i, turn, _, text in facts if i not in converted
    ]
    if not kept:
        return ""
    return lite_notes((), kept)


CORRECTIONS_MAX = 50
"""#700: at most the 50 most recent active corrections in the "Author corrections" block."""


async def author_corrections(conn: AsyncConnection, story_id: str) -> list[str]:
    """#700 (FR-LITE-8): the "Author corrections" block's lines, oldest first: each active
    ``lite_note`` part's text, and (under Lite, after a downgrade) each active structured
    rewrite's instruction ("Author correction: ..."), whose record parts already apply through
    the overlay. Position-less: they survive truncation like any amendment (FR-STORE-21)."""
    from epistrel.core.tables import amendment_parts, amendments, rewrite_sessions

    a, p, r = amendments.c, amendment_parts.c, rewrite_sessions.c
    notes = await conn.execute(
        select(a.tx_time, p.patch_after)
        .select_from(amendment_parts.join(amendments, a.id == p.amendment_id))
        .where(
            p.story_id == story_id,
            p.kind == "lite_note",
            p.status == "active",
            a.status == "active",
        )
    )
    found: list[tuple[Any, str]] = [(t, str((after or {}).get("text") or "")) for t, after in notes]
    structured = await conn.execute(
        select(a.tx_time, r.instruction)
        .select_from(amendments.join(rewrite_sessions, r.id == a.session_id))
        .where(
            a.story_id == story_id,
            a.status == "active",
            a.kind == "rewrite",
            r.pipeline == "structured",
            r.instruction.is_not(None),
        )
    )
    found.extend((t, "Author correction: " + str(text)) for t, text in structured)
    found = sorted((f for f in found if f[1]), key=lambda f: f[0])[-CORRECTIONS_MAX:]
    return [text for _, text in found]


async def lite_note_parts(conn: AsyncConnection, story_id: str) -> list[tuple[str, str]]:
    """#700: the story's active ``lite_note`` parts, oldest first: ``(part_id, text)`` (an
    upgrade converts each like a lasting author fact)."""
    from epistrel.core.tables import amendment_parts, amendments

    a, p = amendments.c, amendment_parts.c
    found = await conn.execute(
        select(p.id, p.patch_after)
        .select_from(amendment_parts.join(amendments, a.id == p.amendment_id))
        .where(
            p.story_id == story_id,
            p.kind == "lite_note",
            p.status == "active",
            a.status == "active",
        )
        .order_by(p.tx_from, p.id)
    )
    return [(str(i), str((after or {}).get("text") or "")) for i, after in found]


def lite_notes(
    steers: Sequence[tuple[str, str]], facts: Sequence[str], corrections: Sequence[str] = ()
) -> str:
    """#244: the Lite prompt's author blocks, as author notes (#700: "Author corrections"
    right after the facts)."""
    blocks: list[str] = []
    if facts:
        blocks.append(
            "Author facts (true from where each was set; a later fact wins over an earlier "
            "one):\n" + "\n".join(f"- {f}" for f in facts)
        )
    if corrections:
        blocks.append(
            "Author corrections (the story's history as the author corrected it):\n"
            + "\n".join(f"- {c}" for c in corrections)
        )
    if steers:
        blocks.append("Author direction (this turn only): " + " ".join(text for _, text in steers))
    return "\n\n".join(blocks)


def steering_line(steers: Sequence[tuple[str, str]]) -> str:
    """The renders' author direction line for queued steers."""
    if not steers:
        return ""
    return "Author direction for this reply: " + " ".join(text for _, text in steers)


__all__ = [
    "LITE_DIRECTIVES",
    "DirectiveApplied",
    "DirectiveRequest",
    "DirectiveResult",
    "apply_directive",
    "author_corrections",
    "author_facts",
    "consume_steers",
    "kept_author_facts",
    "lite_fact_count",
    "lite_facts",
    "lite_note_parts",
    "lite_notes",
    "precheck",
    "queued_steers",
    "record_directed",
    "require_pipeline",
    "steering_line",
]
