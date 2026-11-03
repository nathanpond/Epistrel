"""Parsing a rewrite instruction (FR-REWRITE-1,2; #223).

``call_parse`` makes the one ``rewrite_parse`` call (temperature 0, #53's ``structured_call``)
with the instruction, the answers so far and the story's names (``story_summary``: its
entities, the registered predicates, its States and the clock). An unusable answer after the
call's retries is 422 ``rewrite_unparseable``.

``normalize`` turns the structured changes into normalized ones (``Normalized``: a change key,
its kind, the #221/#236 part it becomes, and its JSON for the preview) against the story as it
stands, and asks instead of guessing (a ``Question`` with a recommended option, always):

- an entity mention whose candidates' scores are within ``rewrite_ambiguity_margin`` of the
  top one (``clarification``, the top candidate recommended, then the most recent); a mention
  with no candidate names a new entity, created at apply and listed in ``new_entities``;
- a predicate the registry does not know (the three closest registered ones and "register it";
  the closest recommended);
- a time expression the clock cannot place (``before the story began`` recommended when the
  expression names a past life stage, else the story's current time);
- a quote that matches more than one active line (the most recent recommended), or a turn and
  beat with no line;
- an ambiguity the model reported (its recommended option when valid, else the first).

``bindings`` are the answers so far, by question key: a bound question is not asked again.
Changes the side-channel never applies are ``refused`` with a reason: a State a provider owns
(``external_owned``), a clock advance (``clock_advance``: corrected with a forward directive,
the owner's decision 3) and a scheduled event's plan (``scheduled_event_plan``, naming
``PATCH /stories/{id}/scheduled-events/{event_id}``, the owner's decision 4).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core.amendments.store import (
    SCHEDULE_ROUTE,
    PropositionPartDraft,
    RecordPartDraft,
    StatePartDraft,
)
from epistrel.core.errors import EpistrelError, RewriteUnparseable
from epistrel.core.membership import active_operation
from epistrel.core.propositions.model import (
    Interval,
    LiteralKind,
    Polarity,
    Proposition,
    TypedLiteral,
)
from epistrel.core.propositions.registry import Registry
from epistrel.core.propositions.vocabulary import SCHEDULED
from epistrel.core.tables import (
    entities,
    entity_aliases,
    events,
    operations,
    transcript_messages,
)
from epistrel.orchestrator.budget import TurnBudget
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.adapter import ModelParams
from epistrel.orchestrator.models.registry import (
    REWRITE_PARSE,
    REWRITE_SUMMARY_MAX,
    RewriteChange,
    RewriteEntity,
    RewriteParseInputs,
    RewriteParseResult,
    RewriteState,
)
from epistrel.orchestrator.models.structured import Uncertain
from epistrel.orchestrator.rewrite import templates as t
from epistrel.orchestrator.rewrite import timing

PARSE_CALLS = 4
"""The calls one parse may make (#53's retries included)."""
PARSE_DEADLINE_S = 120.0
BEFORE_STORY = -1
"""A valid start "before the story began": a clock offset before minute 0."""
OBSERVABLE_EVENTS = (
    "message_rendered",
    "player_input",
    "beat_say",
    "beat_do",
    "beat_narration",
)
"""The events a record change may point at: a line someone saw."""


# -- the story's names -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Entity:
    id: str
    name: str
    kind: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Story:
    """What one parse normalizes against: the story's entities, registry, active States and
    the clock's current offset (``None``: no clock)."""

    story_id: str
    entities: tuple[Entity, ...]
    registry: Registry
    states: tuple[Any, ...]
    now_minutes: int | None
    margin: float
    retcon_timing: str = "include_if_possible"
    """#240: the story's ``ambiguous_retcon_timing`` when the rewrite is planned."""
    head_turn: int = 0


async def load_story(
    conn: AsyncConnection, story_id: str, registry: Registry, *, margin: float
) -> Story:
    from epistrel.core import states, world_clock

    e, a = entities.c, entity_aliases.c
    rows = list(
        await conn.execute(
            select(e.id, e.canonical_name, e.kind)
            .where(e.story_id == story_id, e.merged_into.is_(None))
            .order_by(e.canonical_name, e.id)
        )
    )
    aliases: dict[str, list[str]] = {}
    for row in await conn.execute(
        select(a.entity_id, a.alias).where(a.story_id == story_id).order_by(a.alias)
    ):
        aliases.setdefault(str(row.entity_id), []).append(str(row.alias))
    found = tuple(
        Entity(str(r.id), str(r.canonical_name), str(r.kind), tuple(aliases.get(str(r.id), ())))
        for r in rows
    )
    defined = tuple(await states.definitions(conn, story_id))
    now: int | None = None
    if any(d.name == "world_clock" for d in defined):
        try:
            now = (await world_clock.clock_at(conn, story_id, world_clock.HEAD)).offset_minutes
        except EpistrelError:
            now = None
    return Story(
        story_id,
        found,
        registry,
        defined,
        now,
        margin,
        await timing.setting(conn, story_id),
        await timing.head(conn, story_id),
    )


def summary_inputs(story: Story, instruction: str, answers: Sequence[str]) -> RewriteParseInputs:
    """``rewrite_parse``'s inputs: the instruction, the answers and the story's names (capped,
    sorted by name)."""
    return RewriteParseInputs(
        instruction=instruction,
        answers=list(answers)[:20],
        entities=[
            RewriteEntity(name=x.name, kind=x.kind, aliases=list(x.aliases)[:16])
            for x in story.entities[:REWRITE_SUMMARY_MAX]
        ],
        predicates=sorted(p.name for p in story.registry.predicates.values())[:REWRITE_SUMMARY_MAX],
        states=[
            RewriteState(
                name=s.name,
                type=str(getattr(s.type, "value", s.type)),
                scope=str(getattr(s.scope, "value", s.scope)),
                unit=s.unit,
                aliases=list(s.aliases)[:16],
                owner=str(getattr(s.owner, "value", s.owner)),
            )
            for s in story.states[:REWRITE_SUMMARY_MAX]
        ],
        clock="" if story.now_minutes is None else f"now: minute {story.now_minutes}",
    )


async def call_parse(
    models: ModelAccessLayer, story: Story, instruction: str, answers: Sequence[str]
) -> RewriteParseResult:
    """The one ``rewrite_parse`` call; 422 ``rewrite_unparseable`` when it yields nothing
    usable."""
    found = await models.structured_call(
        REWRITE_PARSE.name,
        RewriteParseResult,
        summary_inputs(story, instruction, answers),
        params=ModelParams(temperature=0.0),
        budget=TurnBudget(max_calls=PARSE_CALLS, deadline_s=PARSE_DEADLINE_S),
    )
    if isinstance(found, Uncertain):
        raise RewriteUnparseable(
            "the instruction could not be parsed into changes",
            {"reason": str(found.reason), "attempts": found.attempts},
        )
    return found


# -- questions ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Question:
    """A question to ask (stored as a ``rewrite_questions`` row): ``key`` is stable across
    re-plans (#237 carries answers by it)."""

    kind: str
    key: str
    prompt: str
    options: tuple[dict[str, Any], ...]
    recommended: str
    reason: str
    severity: str | None = None
    required: bool = True


def question_key(kind: str, *parts: Any) -> str:
    """sha256 over the kind and the question's subject (record refs, change key)."""
    raw = json.dumps([kind, *parts], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _options(labels_effects: Sequence[tuple[str, dict[str, Any]]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"id": f"o{n}", "label": label, "effect": effect}
        for n, (label, effect) in enumerate(labels_effects, start=1)
    )


# -- normalizing ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Normalized:
    """One normalized change: its key, kind, the part draft it becomes at apply (``None`` while
    it names an entity to create) and its preview JSON."""

    key: str
    kind: str
    draft: Any
    summary: dict[str, Any]
    superseded_object: str | None = None


@dataclass
class Parsed:
    """What a parse normalized to: the changes, the questions it asks, the changes refused,
    the entities apply would create, and the changes already true (``noops``)."""

    changes: list[Normalized] = field(default_factory=list)
    questions: list[Question] = field(default_factory=list)
    refused: list[dict[str, Any]] = field(default_factory=list)
    new_entities: list[dict[str, Any]] = field(default_factory=list)
    noops: list[dict[str, Any]] = field(default_factory=list)
    timing_uncertain: list[dict[str, Any]] = field(default_factory=list)
    """#240: the changes placed by an absolute time the clock could not decide."""


_WORDS = re.compile(r"[a-z0-9]+")
_ARTICLES = frozenset({"the", "a", "an", "of", "at", "in"})


def _words(text: str) -> set[str]:
    return set(_WORDS.findall(text.casefold())) - _ARTICLES


def score(mention: str, candidate: Entity) -> float:
    """How well ``mention`` names ``candidate``: 1.0 an exact name or alias (case folded),
    0.8 every word of the mention among the candidate's (or the reverse), else the share of
    shared words scaled to 0.7."""
    wanted = mention.casefold().strip()
    names = (candidate.name, *candidate.aliases)
    if any(wanted == n.casefold().strip() for n in names):
        return 1.0
    mine = _words(mention)
    best = 0.0
    for name in names:
        theirs = _words(name)
        if not mine or not theirs:
            continue
        if mine <= theirs or theirs <= mine:
            best = max(best, 0.8)
        else:
            best = max(best, 0.7 * len(mine & theirs) / len(mine | theirs))
    return best


def candidates(story: Story, mention: str) -> list[tuple[float, Entity]]:
    """The entities ``mention`` may name, best first (ties: the most recently created, a ULID
    being creation order)."""
    found = [(score(mention, x), x) for x in story.entities]
    kept = [(s, x) for s, x in found if s >= 0.5]
    return sorted(kept, key=lambda item: (-item[0], [-ord(c) for c in item[1].id]))


@dataclass
class _Context:
    story: Story
    bindings: Mapping[str, dict[str, Any]]
    parsed: Parsed
    blocked: bool = False


def _entity(ctx: _Context, mention: str, change_key: str, kind: str = "entity") -> str | None:
    """The entity id ``mention`` binds (``new:<name>`` for one apply creates); ``None`` while
    a question about it is open."""
    key = question_key("clarification", "entity", mention.casefold())
    bound = ctx.bindings.get(key)
    if bound is not None and isinstance(bound.get("bind_entity"), str):
        return str(bound["bind_entity"])
    found = candidates(ctx.story, mention)
    if not found:
        new = {"name": mention, "for": change_key}
        if new not in ctx.parsed.new_entities:
            ctx.parsed.new_entities.append(new)
        return f"new:{mention}"
    top = found[0][0]
    close = [(s, x) for s, x in found if top - s <= ctx.story.margin]
    if len(close) == 1:
        return close[0][1].id
    options = _options([(x.name, {"bind_entity": x.id}) for _, x in close])
    ctx.parsed.questions.append(
        Question(
            kind="clarification",
            key=key,
            prompt=t.ENTITY_PROMPT.format(kind=kind, mention=mention),
            options=options,
            recommended=str(options[0]["id"]),
            reason=t.ENTITY_REASON.format(name=close[0][1].name, mention=mention),
        )
    )
    return None


def _predicate(ctx: _Context, name: str) -> str | None:
    key = question_key("clarification", "predicate", name.casefold())
    bound = ctx.bindings.get(key)
    if bound is not None:
        if isinstance(bound.get("predicate_id"), str):
            return str(bound["predicate_id"])
        if isinstance(bound.get("register_predicate"), str):
            return f"register:{bound['register_predicate']}"
    normal = name.strip().casefold().replace(" ", "_").replace("-", "_")
    found = ctx.story.registry.named(normal)
    if found is not None:
        return str(found.id)
    names = sorted(p.name for p in ctx.story.registry.predicates.values())
    closest = difflib.get_close_matches(normal, names, n=3, cutoff=0.0)
    by_name = {p.name: p for p in ctx.story.registry.predicates.values()}
    choices = [(n, {"predicate_id": str(by_name[n].id)}) for n in closest if n in by_name]
    choices.append((t.REGISTER_LABEL.format(predicate=name), {"register_predicate": normal}))
    options = _options(choices)
    ctx.parsed.questions.append(
        Question(
            kind="clarification",
            key=key,
            prompt=t.PREDICATE_PROMPT.format(predicate=name),
            options=options,
            recommended=str(options[0]["id"]),
            reason=t.PREDICATE_REASON.format(name=choices[0][0])
            if closest
            else t.MODEL_FALLBACK_REASON,
        )
    )
    return None


_MINUTES = re.compile(r"^\s*(-?\d+)\s*(?:min(?:ute)?s?)?\s*$")
_BEFORE = re.compile(r"before the story (?:began|begins|started)|story start", re.IGNORECASE)


def _time(ctx: _Context, expression: str | None, change_key: str) -> tuple[bool, int | None]:
    """``(placed, minutes)``: a valid-time bound as a clock offset (``None``: open), or
    ``(False, None)`` while a timing question is open."""
    if expression is None or not expression.strip():
        return True, None
    match = _MINUTES.match(expression)
    if match is not None:
        return True, int(match.group(1))
    if _BEFORE.search(expression):
        return True, BEFORE_STORY
    key = question_key("timing", change_key, expression.casefold())
    bound = ctx.bindings.get(key)
    if bound is not None and "valid" in bound:
        value = bound["valid"]
        return True, None if value is None else int(value)
    past = any(word in expression.casefold() for word in t.PAST_LIFE_STAGES)
    now = ctx.story.now_minutes if ctx.story.now_minutes is not None else 0
    choices = [
        (t.TIME_BEFORE_LABEL, {"valid": BEFORE_STORY}),
        (t.TIME_NOW_LABEL, {"valid": now}),
    ]
    options = _options(choices)
    ctx.parsed.questions.append(
        Question(
            kind="timing",
            key=key,
            prompt=t.TIME_PROMPT.format(change=change_key, expression=expression),
            options=options,
            recommended=str(options[0 if past else 1]["id"]),
            reason=t.TIME_BEFORE_REASON.format(expression=expression)
            if past
            else t.TIME_NOW_REASON,
        )
    )
    return False, None


def _literal(value: Any) -> TypedLiteral:
    if isinstance(value, bool):
        return TypedLiteral(kind=LiteralKind.BOOL, value=value)
    if isinstance(value, int):
        return TypedLiteral(kind=LiteralKind.INT, value=value)
    if isinstance(value, float):
        return TypedLiteral(kind=LiteralKind.FLOAT, value=value)
    return TypedLiteral(kind=LiteralKind.STR, value=str(value))


def prop_key(subject: str, predicate: str, qualifiers: Mapping[str, Any] | None = None) -> str:
    raw = json.dumps(dict(qualifiers or {}), sort_keys=True)
    return f"prop:{subject}:{predicate}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


async def _proposition(conn: AsyncConnection, ctx: _Context, n: int, change: RewriteChange) -> None:
    if not change.subject or not change.predicate:
        ctx.parsed.refused.append({"index": n, "reason": "incomplete", "change": _dump(change)})
        return
    predicate = _predicate(ctx, change.predicate)
    subject = _entity(ctx, change.subject, f"change:{n}")
    obj = None if change.object is None else _entity(ctx, change.object, f"change:{n}")
    if predicate is None or subject is None or (change.object is not None and obj is None):
        return
    if predicate == SCHEDULED:
        ctx.parsed.refused.append(
            {
                "index": n,
                "reason": "scheduled_event_plan",
                "route": SCHEDULE_ROUTE,
                "change": _dump(change),
            }
        )
        return
    owned = ctx.story.registry.get(str(predicate))
    if owned is not None and str(getattr(owned.owner, "value", owned.owner)) == "external":
        # #697 (FR-EXT-8): a predicate the registry gives an external owner is the game's:
        # its past cannot be rewritten (as an externally owned State's).
        from epistrel.orchestrator.external_guard import CODE, PAST

        ctx.parsed.refused.append(
            {
                "index": n,
                "reason": "external_owned",
                "code": CODE,
                "explanation": PAST,
                "predicate": owned.name,
                "provider": None,
                "value_name": owned.name,
                "change": _dump(change),
            }
        )
        return
    key = prop_key(subject, predicate)
    absolute = timing.absolute(change.valid.from_)
    uncertain, bound = False, None
    if absolute is not None:
        placed_from, valid_from = True, None
        placed_abs, uncertain, bound = await _absolute_timing(conn, ctx, key, absolute)
        placed_from = placed_from and placed_abs
    else:
        placed_from, valid_from = _time(ctx, change.valid.from_, key)
    placed_to, valid_to = _time(ctx, change.valid.to, key)
    if not (placed_from and placed_to):
        return
    summary: dict[str, Any] = {
        "kind": "proposition",
        "key": key,
        "subject_id": subject,
        "predicate_id": predicate,
        "object_id": obj,
        "literal": None if change.literal is None else change.literal,
        "polarity": change.polarity,
        "valid_from_minutes": valid_from,
        "valid_to_minutes": valid_to,
    }
    if absolute is not None:
        summary["valid_spec"] = {"absolute": absolute}
        summary["timing_uncertain"] = uncertain
        summary["position_bound"] = bound
    pending = subject.startswith(("new:", "register:")) or (obj or "").startswith(
        ("new:", "register:")
    )
    pending = pending or predicate.startswith("register:")
    draft = None
    superseded: str | None = None
    if not pending:
        proposition = Proposition(
            subject_id=subject,
            predicate_id=predicate,
            object_id=obj,
            literal=None if change.literal is None or obj is not None else _literal(change.literal),
            polarity=Polarity.NEGATED if change.polarity == "negated" else Polarity.AFFIRMED,
            valid_time=Interval.of(valid_from, valid_to),
        )
        current = await _current_objects(conn, ctx.story.story_id, subject, predicate)
        wanted = obj if obj is not None else proposition.object_key()
        if change.polarity == "affirmed" and wanted in current:
            ctx.parsed.noops.append(summary)
            return
        superseded = next((o for o in current if o != wanted and isinstance(o, str)), None)
        draft = PropositionPartDraft(
            proposition,
            valid_from_minutes=valid_from,
            valid_to_minutes=valid_to,
            absolute=absolute,
            timing_uncertain=uncertain,
            position_bound=bound,
        )
    summary["superseded_object_id"] = superseded
    ctx.parsed.changes.append(Normalized(key, "proposition", draft, summary, superseded))


async def _absolute_timing(
    conn: AsyncConnection, ctx: _Context, change_key: str, text: str
) -> tuple[bool, bool, dict[str, Any] | None]:
    """``(placed, timing_uncertain, position_bound)`` for a part beginning at an absolute time
    (#240; ``rewrite.timing``): decided by the anchor, or flagged under
    ``include_if_possible``, or placed by the author's answer under ``ask_author`` (a required
    ``timing`` question until answered: ``placed`` false)."""
    undecidable = await timing.undecidable_at(conn, ctx.story.story_id, text)
    if not undecidable:
        return True, False, None
    if ctx.story.retcon_timing != timing.ASK:
        ctx.parsed.timing_uncertain.append(
            {
                "change_key": change_key,
                "valid_spec": {"absolute": text},
                "undecidable_at": undecidable,
            }
        )
        return True, True, None
    key = question_key("timing", change_key, "absolute", text)
    bound = ctx.bindings.get(key)
    if bound is not None and isinstance(bound.get("bound"), dict):
        return True, False, await timing.with_variant(conn, ctx.story.story_id, bound["bound"])
    choices, best = timing.options(ctx.story.head_turn, text)
    options = _options(choices)
    ctx.parsed.questions.append(
        Question(
            kind="timing",
            key=key,
            prompt=t.ABSOLUTE_PROMPT.format(expression=text),
            options=options,
            recommended=str(options[best]["id"]),
            reason=t.ABSOLUTE_REASONS[best],
        )
    )
    return False, False, None


async def _current_objects(
    conn: AsyncConnection, story_id: str, subject: str, predicate: str
) -> list[Any]:
    from epistrel.core.canon import facts as canon

    found = await canon.canon(conn, story_id, subject=subject, predicate=predicate)
    objects: list[Any] = []
    for fact in found:
        if fact.proposition.polarity is not Polarity.AFFIRMED or fact.valid_to is not None:
            continue
        p = fact.proposition
        objects.append(p.object_id if p.object_id is not None else p.object_key())
    return objects


async def _active_lines(conn: AsyncConnection, story_id: str) -> list[Any]:
    tm, e, o = transcript_messages.c, events.c, operations.c
    return list(
        await conn.execute(
            select(tm.message_id, tm.text, e.narrative_turn, e.beat, e.type)
            .select_from(
                transcript_messages.join(events, events.c.id == tm.message_id).join(
                    operations, operations.c.operation_id == e.operation_id
                )
            )
            .where(e.story_id == story_id, active_operation(operations), o.story_id == story_id)
            .order_by(e.seq)
        )
    )


def _normal(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


async def _record(conn: AsyncConnection, ctx: _Context, n: int, change: RewriteChange) -> None:
    ref = change.ref
    if ref is None or (ref.quote is None and ref.turn is None):
        ctx.parsed.refused.append({"index": n, "reason": "no_record", "change": _dump(change)})
        return
    lines = await _active_lines(conn, ctx.story.story_id)
    if ref.quote is not None:
        quote = _normal(ref.quote)
        matched = [line for line in lines if quote and quote in _normal(str(line.text or ""))]
    else:
        matched = [
            line
            for line in lines
            if line.narrative_turn == ref.turn
            and (ref.beat is None or line.beat == ref.beat)
            and line.type in OBSERVABLE_EVENTS
        ]
        if ref.beat is None:
            rendered = [line for line in matched if line.type == "message_rendered"]
            matched = rendered or matched[-1:]
    if not matched:
        ctx.parsed.refused.append({"index": n, "reason": "no_record", "change": _dump(change)})
        return
    if len(matched) > 1:
        key = question_key("clarification", "record", ref.model_dump(mode="json"))
        bound = ctx.bindings.get(key)
        chosen = None if bound is None else bound.get("record_ref")
        if not isinstance(chosen, dict):
            recent = list(reversed(matched))
            options = _options(
                [
                    (
                        f"Turn {line.narrative_turn}, beat {line.beat or 0}",
                        {"record_ref": {"table": "events", "id": str(line.message_id)}},
                    )
                    for line in recent[:8]
                ]
            )
            ctx.parsed.questions.append(
                Question(
                    kind="clarification",
                    key=key,
                    prompt=t.RECORD_PROMPT.format(quote=ref.quote or "", count=len(matched)),
                    options=options,
                    recommended=str(options[0]["id"]),
                    reason=t.RECORD_REASON,
                )
            )
            return
        record_id = str(chosen["id"])
    else:
        record_id = str(matched[0].message_id)
    action = change.action or ("drop" if change.new_text is None else "replace")
    if action == "replace" and not change.new_text:
        ctx.parsed.refused.append({"index": n, "reason": "no_text", "change": _dump(change)})
        return
    patch = {"text": change.new_text} if action == "replace" else {}
    key = f"record:events:{record_id}"
    summary = {
        "kind": "record",
        "key": key,
        "record": {"table": "events", "id": record_id},
        "action": action,
        "new_text": change.new_text if action == "replace" else None,
    }
    draft = RecordPartDraft("events", record_id, action, patch)
    ctx.parsed.changes.append(Normalized(key, "record", draft, summary))


async def _state(conn: AsyncConnection, ctx: _Context, n: int, change: RewriteChange) -> None:
    from epistrel.core import states
    from epistrel.core.states.model import WORLD_CLOCK, derived_rule

    wanted = (change.state or "").strip().casefold()
    found = next(
        (
            s
            for s in ctx.story.states
            if s.name.casefold() == wanted or wanted in {a.casefold() for a in s.aliases}
        ),
        None,
    )
    if found is None:
        ctx.parsed.refused.append({"index": n, "reason": "unknown_state", "change": _dump(change)})
        return
    if found.name == WORLD_CLOCK:
        ctx.parsed.refused.append(
            {
                "index": n,
                "reason": "clock_advance",
                "note": "the clock moves forward only: use a directive (the next morning, 08:00)",
                "change": _dump(change),
            }
        )
        return
    if str(getattr(found.owner, "value", found.owner)) != "story":
        # #242 (FR-EXT-8): a game-owned value's past is the game's: always refused.
        from epistrel.orchestrator.external_guard import CODE, PAST

        ctx.parsed.refused.append(
            {
                "index": n,
                "reason": "external_owned",
                "code": CODE,
                "explanation": PAST,
                "state": found.name,
                "provider": None if found.provider_id is None else str(found.provider_id),
                "value_name": found.external_value_name or found.name,
                "change": _dump(change),
            }
        )
        return
    rule = derived_rule(found)
    if rule is not None:
        reads = [s for s in ctx.story.states if s.name in str(getattr(rule, "expression", ""))]
        key = question_key("clarification", "derived_state", found.name)
        options = _options(
            [(s.name, {"state": s.name}) for s in reads] or [(found.name, {"state": found.name})]
        )
        ctx.parsed.questions.append(
            Question(
                kind="clarification",
                key=key,
                prompt=t.STATE_PROMPT.format(state=found.name),
                options=options,
                recommended=str(options[0]["id"]),
                reason=t.STATE_REASON.format(name=options[0]["label"], state=found.name),
            )
        )
        return
    scope = None
    if change.scope:
        scope = _entity(ctx, change.scope, f"change:{n}", kind="character")
        if scope is None:
            return
        scope = await _character_of(conn, ctx.story.story_id, scope)
    key = f"state:{found.id}:{scope or ''}"
    placed_from, valid_from = _time(ctx, change.valid.from_, key)
    placed_to, valid_to = _time(ctx, change.valid.to, key)
    if not (placed_from and placed_to):
        return
    value: Any = change.value
    if value is None and change.delta is not None:
        current = await states.value_of(conn, ctx.story.story_id, found.name, scope)
        base = 0 if isinstance(current, states.UndefinedType) else current.value
        value = float(base) + float(change.delta)
        if value == int(value):
            value = int(value)
    if value is None:
        ctx.parsed.refused.append({"index": n, "reason": "no_value", "change": _dump(change)})
        return
    summary = {
        "kind": "state",
        "key": key,
        "state": found.name,
        "state_id": found.id,
        "scope_key": scope,
        "value": value,
        "valid_from_minutes": valid_from,
        "valid_to_minutes": valid_to,
    }
    draft = StatePartDraft(
        found.name,
        value,
        scope_key=scope,
        valid_from_minutes=valid_from,
        valid_to_minutes=valid_to,
    )
    ctx.parsed.changes.append(Normalized(key, "state", draft, summary))


async def _character_of(conn: AsyncConnection, story_id: str, entity_id: str) -> str:
    """A per-character State's scope key: the character an entity is (its own id otherwise)."""
    found = await conn.scalar(
        select(entities.c.character_id).where(
            entities.c.id == entity_id, entities.c.story_id == story_id
        )
    )
    return entity_id if found is None else str(found)


INTENT_LABELS = {"joking": "joke", "sincere": "sincere", "unspecified": "sincere"}


async def _intent(conn: AsyncConnection, ctx: _Context, n: int, change: RewriteChange) -> None:
    """An explicit intent change (#225, FR-DIRECT-8): the utterance said at ``ref`` (by
    ``subject``, the speaker, when named) takes the intent; its classification follows (a joke
    is a joke; a deception against a contrary belief a lie; a sincere line sincere, or a sincere
    error when canon says it is false)."""
    from epistrel.core.tables import utterances

    ref = change.ref
    if ref is None or ref.turn is None or change.intent is None:
        ctx.parsed.refused.append({"index": n, "reason": "no_record", "change": _dump(change)})
        return
    u, e = utterances.c, events.c
    query = (
        select(u.id, u.intent, u.classification, u.objective_relation, u.belief_relation)
        .join(events, events.c.id == u.event_id)
        .join(operations, operations.c.operation_id == e.operation_id)
        .where(
            u.story_id == ctx.story.story_id,
            e.narrative_turn == ref.turn,
            active_operation(operations),
        )
        .order_by(e.seq)
    )
    if ref.beat is not None:
        query = query.where(e.beat == ref.beat)
    if change.subject:
        speaker = _entity(ctx, change.subject, f"change:{n}", kind="character")
        if speaker is None:
            return
        query = query.where(u.speaker_character_id == speaker)
    found = list(await conn.execute(query))
    if len(found) != 1:
        ctx.parsed.refused.append(
            {"index": n, "reason": "no_record" if not found else "ambiguous_record"}
        )
        return
    row = found[0]
    label = INTENT_LABELS.get(change.intent)
    if change.intent == "deceptive":
        label = "lie" if row.belief_relation != "consistent" else "sincere"
    if label == "sincere" and row.objective_relation == "false":
        label = "sincere_error"
    patch = {"intent": change.intent, "classification": label}
    key = f"record:utterances:{row.id}"
    summary = {
        "kind": "intent",
        "key": key,
        "record": {"table": "utterances", "id": str(row.id)},
        "intent": change.intent,
        "notice": {
            "kind": "scene_meaning_may_change",
            "utterance_id": str(row.id),
            "before": {"intent": str(row.intent), "label": row.classification},
            "after": {"intent": change.intent, "label": label},
        },
    }
    draft = RecordPartDraft("utterances", str(row.id), "set_intent", patch)
    ctx.parsed.changes.append(Normalized(key, "intent", draft, summary))


def _dump(change: RewriteChange) -> dict[str, Any]:
    return change.model_dump(mode="json", by_alias=True, exclude_defaults=True)


def _ambiguity(ctx: _Context, n: int, ambiguity: Any) -> None:
    key = question_key("clarification", "model", ambiguity.about.casefold(), ambiguity.question)
    if key in ctx.bindings:
        return
    options = _options([(label, {"choice": label}) for label in ambiguity.options])
    valid = 0 <= ambiguity.recommended < len(options)
    ctx.parsed.questions.append(
        Question(
            kind="clarification",
            key=key,
            prompt=ambiguity.question,
            options=options,
            recommended=str(options[ambiguity.recommended if valid else 0]["id"]),
            reason=t.MODEL_REASON if valid else t.MODEL_FALLBACK_REASON,
        )
    )


async def normalize(
    conn: AsyncConnection,
    story: Story,
    result: RewriteParseResult,
    bindings: Mapping[str, dict[str, Any]],
) -> Parsed:
    """The normalized changes and questions of ``result`` (see the module docstring)."""
    ctx = _Context(story, bindings, Parsed())
    for n, ambiguity in enumerate(result.ambiguities):
        _ambiguity(ctx, n, ambiguity)
    for n, change in enumerate(result.changes):
        if change.kind == "proposition":
            await _proposition(conn, ctx, n, change)
        elif change.kind == "record":
            await _record(conn, ctx, n, change)
        elif change.kind == "intent":
            await _intent(conn, ctx, n, change)
        else:
            await _state(conn, ctx, n, change)
    return ctx.parsed


__all__ = [
    "BEFORE_STORY",
    "Entity",
    "Normalized",
    "Parsed",
    "Question",
    "RewriteUnparseable",
    "Story",
    "call_parse",
    "candidates",
    "load_story",
    "normalize",
    "prop_key",
    "question_key",
    "score",
    "summary_inputs",
]
