"""A story's States (FR-STATE-3,9, FR-STORE-31, FR-RESP-12; #186).

- ``GET /stories/{id}/states``: the States one output viewpoint may read, and the world clock.
- ``GET /stories/{id}/states/{name}``: one State, through a checkpoint (``as_of=N`` or
  ``N+k``).
- ``PUT /stories/{id}/states/{name}``: set a story-owned State between turns.
- ``POST /stories/{id}/states``: declare a State between turns.
- ``PATCH /stories/{id}/states/{name}/definition``: change a State's definition (a story
  configuration change).

Every handler makes one ``Engine`` call (``core.states.api`` holds the rules).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path, Query, Response
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt

from epistrel.api.deps import ContextDep, EngineDep, StoryId
from epistrel.api.idempotency import answered
from epistrel.api.responses import MutationResponse
from epistrel.api.routes.clock import ClockBody
from epistrel.core.states.api import (
    StateDeclared,
    StateDefineRequest,
    StateDefinitionWrite,
    StateRead,
    StateSetRequest,
    StatesView,
    StateView,
    StateWritten,
)

router = APIRouter(prefix="/stories", tags=["stories"])

StateName = Annotated[
    str,
    Path(
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$",
        description="The State's name.",
        examples=["gold"],
    ),
]

_VIEWPOINT = (
    "The read is produced for one **output viewpoint**: `?viewpoint=` (`character:<id>` or "
    "`omniscient`), else `?actor_character_id=`, else the story's `default_output_viewpoint`; "
    "when none resolves it is refused (422 `viewpoint_unresolved`). A character viewpoint is "
    "never given a `hidden` State: it is left out entirely (the character's own value of a "
    "per-character State too), and the story's `player_view` never widens a States read. "
    "`omniscient`, used only when named, reads every State. 409 `module_off` while the story's "
    "configuration has States off."
)
_SLOT = (
    "The write is an inter-turn slot operation at `N⁺k`, after the last completed turn `N` "
    "(ordered by commit): a checkpoint `N` excludes it and `N⁺k` includes it, and a truncation "
    "to `N` or earlier discards it. It bumps the story's `timeline_revision`. Like a turn, it "
    "waits for a turn in flight under `turn_concurrency: queue` and is 423 `story_busy` under "
    "`reject` (with `Retry-After`). It takes `Idempotency-Key` and "
    "`X-Epistrel-Expected-Revision` (409 `revision_conflict`). A refusal writes nothing."
)


class StateBody(BaseModel):
    """One State as the viewpoint reads it: its `name`, `type`, `scope`, `visibility`, `owner`
    and `unit`; its `value` (a story-scoped State) or `values` by character id (a per-character
    one; `null`: undefined); and `since_position`, the position of the last change folded in
    (`null`: the initial value)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    type: str
    scope: Literal["story", "character"]
    visibility: Literal["player_visible", "hidden"]
    owner: Literal["story", "external"]
    unit: str | None
    value: Any = None
    values: dict[str, Any] | None = None
    since_position: dict[str, int] | None

    @classmethod
    def of(cls, read: StateRead) -> StateBody:
        found: dict[str, Any] = {
            "name": read.name,
            "type": read.type,
            "scope": read.scope,
            "visibility": read.visibility,
            "owner": read.owner,
            "unit": read.unit,
            "since_position": read.since_position,
        }
        if read.values is not None:
            found["values"] = read.values
        else:
            found["value"] = read.value
        return cls(**found)


class StatesBody(BaseModel):
    """The States the viewpoint may read, the world clock (only here, never among `states`),
    the story's revision and the viewpoint the read was produced for."""

    states: list[StateBody]
    clock: ClockBody
    timeline_revision: int
    output_viewpoint: str

    @classmethod
    def of(cls, view: StatesView) -> StatesBody:
        return cls(
            states=[StateBody.of(s) for s in view.states],
            clock=ClockBody.of(view.clock),
            timeline_revision=view.timeline_revision,
            output_viewpoint=view.output_viewpoint,
        )


class StateViewBody(BaseModel):
    """One State through `as_of` (`null`: the story as it stands)."""

    state: StateBody
    as_of: str | None
    timeline_revision: int
    output_viewpoint: str

    @classmethod
    def of(cls, view: StateView) -> StateViewBody:
        return cls(
            state=StateBody.of(view.state),
            as_of=view.as_of,
            timeline_revision=view.timeline_revision,
            output_viewpoint=view.output_viewpoint,
        )


class StateWrittenBody(MutationResponse):
    """A set: the State's `old_value` and `new_value` (`null`: undefined), its inter-turn slot
    `position` (`N⁺k`; `slot` the full position), the slot's `operation_id`, the `change_id`
    it wrote and the story's revision after it."""

    state: str
    old_value: Any
    new_value: Any
    position: str
    slot: dict[str, int]
    operation_id: str
    change_id: str

    @classmethod
    def of(cls, written: StateWritten) -> StateWrittenBody:
        return cls(
            state=written.state,
            old_value=written.old_value,
            new_value=written.new_value,
            position=written.position,
            slot=dict(written.slot),
            operation_id=written.operation_id,
            change_id=written.change_id,
            timeline_revision=written.timeline_revision,
        )


class StateDeclaredBody(MutationResponse):
    """A declaration: the stored `definition`, its inter-turn slot `position` (`N⁺k`; `slot`
    the full position), the slot's `operation_id` and the story's revision after it."""

    definition: dict[str, Any]
    position: str
    slot: dict[str, int]
    operation_id: str

    @classmethod
    def of(cls, declared: StateDeclared) -> StateDeclaredBody:
        return cls(
            definition=dict(declared.definition),
            position=declared.position,
            slot=dict(declared.slot),
            operation_id=declared.operation_id,
            timeline_revision=declared.timeline_revision,
        )


class StateDefinitionPatch(BaseModel):
    """`PATCH /stories/{id}/states/{name}/definition`: a JSON merge patch over the definition's
    mutable members (`constraints`, `authorization`, `transfer`, `rules` and `visibility`,
    interpretive; `aliases`, `unit`, `silent` and `reservation_expiry_minutes`, prospective). A
    member of the State's identity (`name`, `type`, `scope`, `owner`, `provider_id`,
    `entity_id`, `initial_value`, `created_by`) is 422 `immutable_field`."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"constraints": {"max": 500}}]},
    )

    constraints: dict[str, Any] | None = None
    authorization: dict[str, Any] | None = None
    transfer: dict[str, Any] | None = None
    rules: dict[str, Any] | None = None
    visibility: Literal["player_visible", "hidden"] | None = None
    aliases: list[str] | None = None
    unit: str | None = None
    silent: StrictBool | None = None
    reservation_expiry_minutes: StrictInt | None = None
    name: Any = None
    type: Any = None
    scope: Any = None
    owner: Any = None
    provider_id: Any = None
    entity_id: Any = None
    initial_value: Any = None
    created_by: Any = None


class StateDefinitionWriteBody(MutationResponse):
    """The definition after a `PATCH`, the story's `config_revision` (moved by a change) and its
    unchanged `timeline_revision`."""

    definition: dict[str, Any]
    config_revision: int

    @classmethod
    def of(cls, write: StateDefinitionWrite) -> StateDefinitionWriteBody:
        return cls(
            definition=dict(write.definition),
            config_revision=write.config_revision,
            timeline_revision=write.timeline_revision,
        )


@router.get(
    "/{story_id}/states",
    response_model=StatesBody,
    operation_id="get_states",
    summary="Read a story's States for one output viewpoint",
    description=(
        "The story's States as it stands (FR-STATE-9), each with its value (`values` by "
        "character for a per-character State) and the position of its last change, and the "
        "world clock under `clock` (never among `states`; `GET /stories/{id}/clock`). " + _VIEWPOINT
    ),
)
async def get_states(
    story_id: StoryId,
    ctx: ContextDep,
    engine: EngineDep,
    actor_character_id: str | None = None,
) -> StatesBody:
    view = await engine.get_states(ctx, story_id, actor_character_id=actor_character_id)
    return StatesBody.of(view)


@router.get(
    "/{story_id}/states/{name}",
    response_model=StateViewBody,
    operation_id="get_state",
    summary="Read one State of a story",
    description=(
        "One State (FR-STATE-9), folded through `as_of`: `N` (the state after turn N's reply, "
        "without its inter-turn slots) or `N+k` (`N⁺k`: with slots 1..k; send `+` as `%2B`); "
        "the story as it stands when absent. A State declared after `as_of` is not there "
        "(404). A `hidden` State read for a character viewpoint is 404 exactly as an unknown "
        "name (as is `world_clock`: `GET /stories/{id}/clock`). " + _VIEWPOINT
    ),
)
async def get_state(
    story_id: StoryId,
    name: StateName,
    ctx: ContextDep,
    engine: EngineDep,
    actor_character_id: str | None = None,
    as_of: Annotated[str | None, Query(max_length=24, examples=["14", "14+2"])] = None,
) -> StateViewBody:
    view = await engine.get_state(
        ctx, story_id, name, actor_character_id=actor_character_id, as_of=as_of
    )
    return StateViewBody.of(view)


@router.put(
    "/{story_id}/states/{name}",
    response_model=StateWrittenBody,
    operation_id="set_state",
    summary="Set a story-owned State between turns",
    description=(
        "Set a State as the consumer (FR-STATE-9): a `value`, or a signed `delta` for a number "
        "State, for `scope_key`'s value of a per-character State, with a `reason`. Answers the "
        "`old_value` and `new_value` and the slot's `position`. " + _SLOT + " 422 "
        "`state_constraint_violation` (`{constraint, attempted, current}`), "
        "`state_readonly_external` (an externally owned value), `state_unpermitted` "
        "(`reason: source`: the State's `authorization.sources` omit `consumer`), "
        "`state_derived`, `validation_error` (`reason: reserved_state` for `world_clock`: use "
        "`POST /stories/{id}/clock/advance`); 409 `module_off` (States off; `prompt_only` and "
        "`on` both take API sets), `story_archived`, `story_paused`."
    ),
)
async def set_state(
    story_id: StoryId,
    name: StateName,
    body: StateSetRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StateWrittenBody:
    written = await engine.set_state(ctx, story_id, name, body)
    return answered(StateWrittenBody.of(written), ctx, response, replayed=written.replayed)


@router.post(
    "/{story_id}/states",
    response_model=StateDeclaredBody,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="define_state",
    summary="Declare a State on a story",
    description=(
        "Declare a State as the consumer (FR-STATE-3): its `definition` (name, type, scope, "
        "constraints, initial value, aliases, unit, visibility, owner and effect rules). "
        "Answers 201 with the stored definition and its slot's `position`. The declaration "
        "follows its slot: a truncation to `N` or earlier discards it and frees its name. "
        + _SLOT
        + " 409 `state_name_taken` for a name an active State holds, `module_off`, "
        "`story_archived`, `story_paused`; 422 `validation_error` for a shape error or "
        "`world_clock` (`reason: reserved_state`)."
    ),
)
async def define_state(
    story_id: StoryId,
    body: StateDefineRequest,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StateDeclaredBody:
    declared = await engine.define_state(ctx, story_id, body)
    return answered(StateDeclaredBody.of(declared), ctx, response, replayed=declared.replayed)


@router.patch(
    "/{story_id}/states/{name}/definition",
    response_model=StateDefinitionWriteBody,
    operation_id="update_state_definition",
    summary="Change a State's definition",
    description=(
        "Change a State's definition with a JSON merge patch (RFC 7396) over its mutable "
        "members (FR-STATE-3): a story configuration change. Each changed member records a "
        "`config_changes` row (`state:<name>.<member>`) and the story's `config_revision` moves "
        "by one; the timeline revision does not. `constraints`, `authorization`, `transfer`, "
        "`rules` and `visibility` are interpretive (a turn checked under the old ones is "
        "re-checked), the others prospective. A patch that changes nothing records nothing. "
        "422 `validation_error` (`reason: immutable_field` for the State's identity, "
        "`reserved_state` for `world_clock`: `PATCH /stories/{id}/clock`), "
        "`state_constraint_violation` when the current value breaks new constraints; 409 "
        "`module_off`, `story_archived`."
    ),
)
async def update_state_definition(
    story_id: StoryId,
    name: StateName,
    body: StateDefinitionPatch,
    response: Response,
    ctx: ContextDep,
    engine: EngineDep,
) -> StateDefinitionWriteBody:
    done = await engine.update_state_definition(
        ctx, story_id, name, body.model_dump(exclude_unset=True)
    )
    return answered(StateDefinitionWriteBody.of(done), ctx, response, replayed=done.replayed)


__all__ = [
    "StateBody",
    "StateDeclaredBody",
    "StateDefinitionPatch",
    "StateDefinitionWriteBody",
    "StateViewBody",
    "StateWrittenBody",
    "StatesBody",
    "router",
]
