"""A consumer purpose called inside a story, with one character's viewpoint (FR-MODEL-16; #826).

``call_story_purpose`` answers ``POST /stories/{id}/purposes/{name}/calls``
(``Engine.call_story_purpose``): #825's call (``orchestrator.purposes.call``) with the story
supplying what a story-less call names, and optionally one character's viewpoint as context.
The call **reads** the story and never changes it: it takes no story lock, opens no operation,
moves no revision and writes no event, so it runs while a turn is in flight. A refused input's
or output's ``audit_log`` row is the only thing it writes, as for #825. In order:

1. **Prechecks**, each refusing before anything else: an unknown story is 404, an archived one
   409 ``story_archived`` (a paused story is answered: the call is read-only); with a
   ``viewpoint_character_id``, a character the story does not hold (an erased one included:
   erasure deletes its row) is 404, the player's character 422 ``validation_error``
   (``details.reason: player_has_no_context``: it is never rendered, FR-EPI-12), and a story on
   the Lite pipeline 409 ``module_off`` (Lite keeps no partitions to build a viewpoint from).
   Any structured story accepts one, with per-character partitions or shared knowledge.
2. **Prepare** (``call.prepare``'s steps): the purpose version, then the story's rating and its
   prompt guidance (first in the preamble, FR-RATE-4), and the story's guard context
   (``policies.store.guard_context``): its effective policies and its cast at their true ages,
   each present when co-located at the latest scene's location, so the minors policy applies
   as it would to a turn there. A character-scoped policy applies only to the viewpoint
   character (``PreparedCall.actor``), or to none without one. Routing is the deployment's
   (#824): the purpose's own model configuration, never the story's profile or routing.
3. **Erased subjects**: an input naming a subject erased from the story is 409
   ``erased_subject_referenced`` (``core.erasure.guard.check_content``, consumer text, with no
   base revision: a call reads the head, so every erasure counts).
4. **The input gate** (``call.gate_inputs``, under the story's guard context, its audit row
   naming the story): a violation is 422 ``guardrail_violation`` with no model call at all,
   neither the purpose's nor an embedding for the viewpoint.
5. **The viewpoint** (with ``viewpoint_character_id``): in one ``REPEATABLE READ READ ONLY``
   transaction, the story's head (``next_turn_index``) and its latest committed scene, then
   ``core.epistemic.context.build_context`` for the character with nothing pending, the call's
   string inputs joined as the retrieval query (the real embedder and lexical ports), and lazy
   resolution off. The memories retrieval returns are not touched (``last_accessed_at``): the
   touches are deferred and never flushed. ``orchestrator.structured.viewpoint`` renders it
   into ``viewpoint.name`` and ``viewpoint.text`` through the render's own partials and prompt
   token budget.
6. **Render** (``call.render``): ``story`` is ``{id, title}``; with a viewpoint it adds
   ``location`` (the scene's location name), ``present`` (the names of who is there) and
   ``scene`` (the viewpoint's scene line, with apparent ages). The rolling summary is never
   there: it is not viewpoint-scoped (#544). A template that reads ``viewpoint`` when none was
   requested is 422 ``template_render_error``.
7. **Produce and assemble** (``call.produce``, ``call.assemble``): the output gate, its one
   retry, then 422 ``output_policy_violation``; the answer is #825's.

Nothing here logs, counts or traces an input, a template, a viewpoint or an output (invariant
3).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, StringConstraints
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.core.enums import StoryStatus
from epistrel.core.epistemic import context as character_context
from epistrel.core.erasure import guard as erasure_guard
from epistrel.core.errors import (
    InvalidRequest,
    ModuleOff,
    StoryArchived,
    StoryCharacterNotFound,
    StoryNotFound,
)
from epistrel.core.profiles import effective
from epistrel.core.profiles.capabilities import level_at_least
from epistrel.core.profiles.schema import ProfileSettings
from epistrel.core.projections.structured import scene_view
from epistrel.core.purposes.store import VERSION_MAX
from epistrel.core.ratings import story_guidance
from epistrel.core.retrieval.pipeline import deferring_touches
from epistrel.core.settings_schema import OMNISCIENT
from epistrel.core.tables import characters, stories
from epistrel.core.turns import CastEntry
from epistrel.orchestrator.models.access import ModelAccessLayer
from epistrel.orchestrator.models.errors import ModelUnavailable
from epistrel.orchestrator.models.metrics import recording
from epistrel.orchestrator.policies.store import guard_context
from epistrel.orchestrator.purposes import call as purpose_call
from epistrel.orchestrator.purposes.call import (
    DEFAULT_DEADLINE_MS,
    MAX_DEADLINE_MS,
    CallRating,
    PreparedCall,
    PurposeResult,
    check_inputs,
    input_texts,
)
from epistrel.orchestrator.structured.viewpoint import (
    Viewpoint,
    render_viewpoint,
    viewpoint_inputs,
)

if TYPE_CHECKING:
    from epistrel.core.retrieval.lexical import LexicalSearch
    from epistrel.orchestrator.embeddings.service import EmbeddingService

PIPELINE = "pipeline"
STRUCTURED_LEVEL = "structured"
PLAYER_HAS_NO_CONTEXT = "player_has_no_context"
VIEWPOINT_FIELD = "body.viewpoint_character_id"
QUERY_MAX = 2_000
"""The retrieval query's text, at most (``build_context`` cuts it there too)."""
ERASURE_PATH = "epistrel.orchestrator.purposes.story"


class StoryPurposeCall(BaseModel):
    """``POST /stories/{id}/purposes/{name}/calls``: one call of a consumer purpose inside a
    story. The story supplies the content rating and the true ages of everyone in it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "inputs": {"challenge": "the raft race", "winner": "Ash"},
                    "viewpoint_character_id": "01J9Z8Q4N6M3W2V1T0S9R8P7Q6",
                    "deadline_ms": 30000,
                }
            ]
        },
    )

    inputs: Annotated[dict[str, Any], AfterValidator(purpose_call._json_only)] = Field(
        description="The template's `inputs`: a JSON object of strings, numbers, booleans, "
        "nulls, and arrays and objects of them (depth at most 6, at most 64 KB as canonical "
        "JSON). Every string in it passes the input gate under the story's guardrails, and "
        "they are joined as the viewpoint's retrieval query."
    )
    version: StrictInt | None = Field(
        default=None,
        ge=1,
        le=VERSION_MAX,
        description="The purpose version to call (default: the latest).",
    )
    viewpoint_character_id: (
        Annotated[str, StringConstraints(min_length=1, max_length=64)] | None
    ) = Field(
        default=None,
        description="A character of the story whose viewpoint the template receives as "
        "`viewpoint` (`name`, and `text`: what that character knows, believes and has seen, "
        "as its own render would show it). Not the player's character; not on a Lite story.",
    )
    deadline_ms: StrictInt = Field(
        default=DEFAULT_DEADLINE_MS,
        ge=1,
        le=MAX_DEADLINE_MS,
        description="The call's deadline, from its start, the input gate and the viewpoint "
        "included (default 60 000, at most 300 000). Once it passes, the call is 503 "
        "`model_unavailable` with `details.reason: deadline`.",
    )


# -- 1. the prechecks ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoryRead:
    """What the prechecks read: the story's title, its effective settings and, with a
    viewpoint, the character."""

    story_id: str
    title: str | None
    timeline_revision: int
    settings: ProfileSettings
    character: CastEntry | None


async def read_story(
    db: AsyncEngine, story_id: str, viewpoint_character_id: str | None
) -> StoryRead:
    """The prechecks, in order (module docs, step 1)."""
    async with db.connect() as conn:
        row = (
            await conn.execute(
                select(stories.c.status, stories.c.name, stories.c.timeline_revision).where(
                    stories.c.id == story_id
                )
            )
        ).one_or_none()
        if row is None:
            raise StoryNotFound(f"story {story_id} does not exist", {"story_id": story_id})
        if row.status == StoryStatus.ARCHIVED.value:
            raise StoryArchived(
                f"story {story_id} is archived; unarchive it to call a purpose in it",
                {"story_id": story_id},
            )
        settings = (
            await effective.effective_configuration(conn, story_id, validate=False)
        ).settings
        character = None
        if viewpoint_character_id is not None:
            character = await _character(conn, story_id, viewpoint_character_id)
    if character is not None and not level_at_least(settings, PIPELINE, STRUCTURED_LEVEL):
        raise ModuleOff(
            "a Lite story keeps no character knowledge to build a viewpoint from; call without "
            "`viewpoint_character_id`",
            {"story_id": story_id, "module": PIPELINE, "field": VIEWPOINT_FIELD},
        )
    return StoryRead(
        story_id=story_id,
        title=row.name,
        timeline_revision=int(row.timeline_revision),
        settings=settings,
        character=character,
    )


async def _character(conn: AsyncConnection, story_id: str, character_id: str) -> CastEntry:
    c = characters.c
    row = (
        await conn.execute(
            select(c.id, c.name, c.definition, c.is_player, c.deflection_style).where(
                c.story_id == story_id, c.id == character_id
            )
        )
    ).one_or_none()
    if row is None:
        raise StoryCharacterNotFound(
            f"story {story_id} has no character {character_id}",
            {"story_id": story_id, "character_id": character_id},
        )
    if row.is_player:
        raise InvalidRequest(
            "the player's character has no generation context, so no viewpoint",
            {"field": VIEWPOINT_FIELD, "reason": PLAYER_HAS_NO_CONTEXT},
        )
    return CastEntry(
        id=str(row.id),
        name=str(row.name),
        definition=str(row.definition or ""),
        is_player=False,
        deflection_style=row.deflection_style,
    )


# -- 2. prepare ------------------------------------------------------------------------------


async def prepare_story(
    db: AsyncEngine,
    models: ModelAccessLayer,
    name: str,
    request: StoryPurposeCall,
    story: StoryRead,
    *,
    started: float,
) -> PreparedCall:
    """Step 2 (module docs): the purpose, the story's rating and guard context, the route."""
    purpose = await purpose_call.resolve(models, name, request.version)
    guidance = await story_guidance(db, story.story_id)
    context = await guard_context(db, story.story_id)
    endpoint, model, revision = await models.endpoint(name)
    actor = None if story.character is None else story.character.id
    return PreparedCall(
        purpose=purpose,
        rating=CallRating(
            rating_id=guidance.rating_id, guidance=str(guidance.prompt_guidance or "")
        ),
        context=context,
        endpoint=endpoint,
        model=model,
        config_revision=revision,
        started=started,
        deadline_s=request.deadline_ms / 1000,
        actor=actor,
        render_ids=() if actor is None else (actor,),
    )


# -- 3. erased subjects ----------------------------------------------------------------------


async def refuse_erased(db: AsyncEngine, story_id: str, inputs: Mapping[str, Any]) -> None:
    """409 ``erased_subject_referenced`` for an input naming a subject erased from the story."""
    texts = tuple(t.text for t in input_texts(inputs) if t.text)
    if not texts:
        return
    async with db.connect() as conn:
        await erasure_guard.check_content(
            conn,
            story_id,
            [erasure_guard.Content(texts=texts, consumer=True)],
            base_revision=None,
            path=ERASURE_PATH,
        )


# -- 5. the viewpoint ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scene:
    """The latest committed scene, as the story's head shows it."""

    location: str | None
    present: tuple[str, ...]
    """The present entities' names, sorted (ids are random across runs)."""


def query_of(inputs: Mapping[str, Any]) -> str:
    """The call's string inputs, joined: the viewpoint's retrieval query."""
    return " ".join(t.text for t in input_texts(inputs) if t.text)[:QUERY_MAX]


async def build_viewpoint(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story: StoryRead,
    character: CastEntry,
    inputs: Mapping[str, Any],
    *,
    lexical: LexicalSearch | None,
) -> tuple[Viewpoint, Scene, str]:
    """The character's viewpoint at the story's head (module docs, step 5), the scene it was
    read at, and its scene line."""
    embeddings = models.embeddings
    # A call never touches a memory it retrieved (``last_accessed_at``, #618): it writes
    # nothing, so the touches are deferred and dropped, never flushed.
    with deferring_touches():
        return await _viewpoint(db, story, character, inputs, embeddings, lexical)


async def _viewpoint(
    db: AsyncEngine,
    story: StoryRead,
    character: CastEntry,
    inputs: Mapping[str, Any],
    embeddings: EmbeddingService,
    lexical: LexicalSearch | None,
) -> tuple[Viewpoint, Scene, str]:
    async with db.connect() as conn, conn.begin():
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        head = int(
            await conn.scalar(
                select(stories.c.next_turn_index).where(stories.c.id == story.story_id)
            )
            or 1
        )
        found = None
        if head - 1 >= 0:
            try:
                found = await scene_view(conn, story.story_id, as_of=head - 1, viewpoint=OMNISCIENT)
            except InvalidRequest:
                found = None
        location = None if found is None or found.location is None else str(found.location.name)
        present = tuple(sorted(e.name for e in found.present)) if found is not None else ()
        shared = ""
        if found is not None and (location is not None or present):
            shared = f"Location: {location or 'an unnamed place'}." + (
                f" Present: {', '.join(present)}." if present else ""
            )
        state = character_context.TurnState(
            story_id=story.story_id,
            turn=head,
            settings=story.settings,
            location_name=location,
            present=tuple(e.entity_id for e in found.present) if found is not None else (),
            scene_text=shared,
            embed=embeddings.query_embedder(),
            embed_batch=embeddings.batch_embedder(),
            lexical=lexical,
        )
        context = await character_context.build_context(
            conn, character.id, state, query=query_of(inputs) or None, lazy=False
        )
    inputs_ = viewpoint_inputs(context, character, budget=story.settings.render.prompt_token_budget)
    return (
        render_viewpoint(inputs_),
        Scene(location=location, present=present),
        context.scene,
    )


def story_of(story: StoryRead, scene: Scene | None = None, line: str = "") -> dict[str, Any]:
    """The template's ``story`` (module docs, step 6)."""
    found: dict[str, Any] = {"id": story.story_id, "title": story.title}
    if scene is not None:
        found |= {"location": scene.location, "present": list(scene.present), "scene": line}
    return found


# -- the call --------------------------------------------------------------------------------


async def call_story_purpose(
    db: AsyncEngine,
    models: ModelAccessLayer,
    story_id: str,
    name: str,
    request: StoryPurposeCall,
    *,
    principal: str,
    lexical: LexicalSearch | None = None,
) -> PurposeResult:
    """A call of consumer purpose ``name`` inside story ``story_id`` (module docs)."""
    started = time.monotonic()
    check_inputs(request.inputs)
    story = await read_story(db, story_id, request.viewpoint_character_id)
    with recording() as log:
        prepared = await prepare_story(db, models, name, request, story, started=started)
        await refuse_erased(db, story_id, request.inputs)
        await purpose_call.gate_inputs(
            db, models, prepared, request.inputs, principal=principal, story_id=story_id
        )
        viewpoint: Viewpoint | None = None
        context_story = story_of(story)
        if story.character is not None:
            viewpoint, scene, line = await build_viewpoint(
                db, models, story, story.character, request.inputs, lexical=lexical
            )
            context_story = story_of(story, scene, line)
        messages = purpose_call.render(
            prepared, request.inputs, viewpoint=viewpoint, story=context_story
        )
        if prepared.expired():
            raise ModelUnavailable(name, "deadline", attempts=0)
        produced = await purpose_call.produce(
            db, models, prepared, messages, principal=principal, story_id=story_id
        )
        answered = await purpose_call.assemble(db, prepared, produced, log)
    return replace(answered, timeline_revision=story.timeline_revision)


__all__ = [
    "PLAYER_HAS_NO_CONTEXT",
    "Scene",
    "StoryPurposeCall",
    "StoryRead",
    "build_viewpoint",
    "call_story_purpose",
    "prepare_story",
    "query_of",
    "read_story",
    "refuse_erased",
    "story_of",
]
