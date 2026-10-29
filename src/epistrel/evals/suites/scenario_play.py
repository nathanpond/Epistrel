"""The intervention scenarios E-12 and E-13 replay (#158; the set is
``evals/sets/scenarios/cases.jsonl``, authored by ``evals.authoring.scenarios``).

``play(ctx, case)`` makes one scratch story: Wren (the player) and Bobby (who renders, with a
full definition: E-13's voice reference). It runs the story on ``Structured (M4)``, or its
``gated`` variant (``CONFIG_DIALS``), plants the S-COAT canon (the coat on the hallway hook,
the author's facts) with the cast in the parlor (#572: the profile routes perception, and an
unplaced character perceives nothing of the player's line) and sets the drivers as the story's
custom instructions. Then it plays each turn through ``Engine.respond``, with Bobby as the
render character and the case's requested length, if any.

For each turn it rebuilds the **first draft** (``first_draft``): the turn's raw renders in
order, so a beat the repair ladder corrected is read back from its ``render_rejected`` event
(attempt 0; #151) and a beat it dropped is kept at its position. These are put together with
#152's deterministic assembly for the turn's viewpoint, exactly as the message would have read
had nothing intervened (for a turn where only the composition changed, the pre-composition
beats). It also names the **intervention types** that fired (``interventions``), read from the
turn report:

- ``repair_rung_1`` to ``repair_rung_4``: a ``turn_report.repairs`` entry's rung (#151);
- ``composition_polished``: the polish was released and its text differs from the deterministic
  assembly of the verified beats (#152);
- ``composition_fallback``: the polish failed its check and the assembly was released;
- ``age_repair``: the render's introductions needed an age repair (#150).

A turn **changed** its first draft when the final message differs from it after white space is
collapsed. A turn that changed nothing is not a pair. A multi-intervention turn counts for every
type that fired.

On the stub, ``script(case, stub)`` scripts each turn's render, regeneration and polish by the
case's ``X-Eval-Case`` line and the turn's own player line, as the user block carries it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from epistrel.core.mutation import FactRequest
from epistrel.core.propositions import store
from epistrel.core.propositions.model import Interval, Proposition
from epistrel.core.propositions.vocabulary import CORE_BY_NAME
from epistrel.core.stories import CharacterSpec, StorySpec
from epistrel.core.story_texts import CustomInstructionsRequest
from epistrel.core.tables import events, operations
from epistrel.core.turns.assembly import AssemblyBeat, deterministic_assembly
from epistrel.evals import scenes
from epistrel.evals.judging import normalized
from epistrel.evals.suites import CaseModel
from epistrel.orchestrator.compose.compose import COMPOSITION_FALLBACK, COMPOSITION_TEMPLATE
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models.adapter import EVAL_CASE_HEADER
from epistrel.orchestrator.models.registry import GENERATION, REGENERATION
from epistrel.orchestrator.models.stub import StubAdapter

if TYPE_CHECKING:
    from epistrel.evals.runner import CaseContext

InterventionType = Literal[
    "repair_rung_1",
    "repair_rung_2",
    "repair_rung_3",
    "repair_rung_4",
    "composition_polished",
    "composition_fallback",
    "age_repair",
]
INTERVENTION_TYPES: tuple[InterventionType, ...] = (
    "repair_rung_1",
    "repair_rung_2",
    "repair_rung_3",
    "repair_rung_4",
    "composition_polished",
    "composition_fallback",
    "age_repair",
)
Config = Literal["structured", "gated"]
CONFIGS: tuple[Config, ...] = ("structured", "gated")
CONFIG_DIALS: Mapping[Config, Mapping[str, Any]] = {
    "structured": {},
    "gated": {"verification": {"extraction_mode": "gated"}},
}
"""Each configuration E-12 and E-13 report per fingerprint: the seeded ``Structured (M4)``
profile, and its ``gated`` variant (claim extraction gated, the rest unchanged)."""

PLAYER = "Wren"
RENDERER = "Bobby"
DEFINITION = (
    "Bobby is Wren's lodger, thirty-four, easy-going and a little lazy. Personality: wry, "
    "warm, evasive about anything that sounds like a chore. When threatened or challenged, he "
    "deflects; he tends to defer to others; he is as honest as most people. Interaction style: "
    "short, dry sentences; teases rather than argues; calls Wren 'boss'. Sample dialogue: "
    '"Me? Lazy? I\'m conserving energy, boss."'
)
SAFE_FALLBACK = "Bobby shrugs and says nothing."
CAST = (
    CharacterSpec(
        name=PLAYER, definition="The householder, the player.", true_age=38, is_player=True
    ),
    CharacterSpec(
        name=RENDERER, definition=DEFINITION, true_age=34, safe_fallback_text=SAFE_FALLBACK
    ),
)
ITEMS = ("coat",)
PLACES = ("hook", "armchair", "hallway", "parlor")
LOCATED_AT = CORE_BY_NAME["located_at"].id
LOCATED_IN = CORE_BY_NAME["located_in"].id
RENDER_PREFIX = "beat_"


class ScenarioStub(BaseModel):
    """A turn's scripted stub answers: the render, and the regeneration and polish if any."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    render: str
    regeneration: str | None = None
    composition: str | None = None


class ScenarioTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    say: str = Field(min_length=1, max_length=500)
    expected: list[InterventionType] = Field(default_factory=list)
    stub: ScenarioStub
    stub_judge: Literal["final", "draft", "tie"] = "final"
    stub_voice: int = Field(default=4, ge=1, le=5)


class Drivers(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hooks: list[str] = Field(default_factory=list)
    objectives: list[str] = Field(default_factory=list)
    planned_reveals: list[str] = Field(default_factory=list)


class ScenarioCase(CaseModel):
    family: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    origin: Literal["whitepaper", "authored", "paraphrase"]
    config: Config
    length: Literal["short", "medium", "long"] | None = None
    drivers: Drivers
    turns: list[ScenarioTurn] = Field(min_length=1, max_length=12)
    stub_driver: int = Field(default=4, ge=1, le=5)


def drivers_text(drivers: Drivers) -> str:
    """The drivers as the story's custom instructions (and E-13's driver reference)."""
    lines = ["Story drivers (the author's intentions, not facts the characters know):"]
    lines += [f"- Plot hook: {h}" for h in drivers.hooks]
    lines += [f"- Objective: {o}" for o in drivers.objectives]
    lines += [f"- Planned reveal (not before its time): {r}" for r in drivers.planned_reveals]
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class PlayedTurn:
    """One played turn: its player line, the first draft, the final message, the intervention
    types that fired and whether the final message differs from the draft."""

    index: int
    say: str
    draft: str
    final: str
    types: tuple[InterventionType, ...]
    fingerprint: str | None

    @property
    def changed(self) -> bool:
        return normalized(self.draft) != normalized(self.final)


@dataclass(frozen=True, slots=True)
class Played:
    story_id: str
    config: Config
    turns: tuple[PlayedTurn, ...]
    names: tuple[str, ...]
    places: tuple[str, ...]


async def _plant(ctx: CaseContext, story_id: str, cast: Sequence[str]) -> None:
    """S-COAT's canon: the coat on the hook, the hook in the hallway, the armchair in the
    parlor (the author's facts, so the two places are told apart), and ``cast`` in the parlor:
    the seeded profile routes perception (#181), and an unplaced character perceives nothing,
    so Bobby hears the player's line only when both are placed together (#572)."""
    async with ctx.engine.db.begin() as conn:
        found = {name: await store.add_entity(conn, story_id, "item", name) for name in ITEMS}
        for name in PLACES:
            found[name] = await store.add_entity(conn, story_id, "place", name)
    facts = (
        (found["hook"], LOCATED_IN, found["hallway"]),
        (found["armchair"], LOCATED_IN, found["parlor"]),
        (found["coat"], LOCATED_AT, found["hook"]),
        *((character, LOCATED_AT, found["parlor"]) for character in cast),
    )
    for subject, predicate, obj in facts:
        await ctx.engine.commit_fact(
            ctx.principal,
            story_id,
            FactRequest(
                proposition=Proposition(
                    subject_id=subject,
                    predicate_id=predicate,
                    object_id=obj,
                    valid_time=Interval.of(None, None),
                )
            ),
        )


async def _rejected_originals(ctx: CaseContext, operation_id: str) -> dict[int, str]:
    """Each repaired beat's original text (its ``render_rejected`` event, attempt 0), by beat."""
    async with ctx.engine.db.connect() as conn:
        rows = await conn.execute(
            select(events.c.payload).where(
                events.c.operation_id == operation_id, events.c.type == "render_rejected"
            )
        )
        found: dict[int, str] = {}
        for (payload,) in rows:
            if isinstance(payload, dict) and payload.get("attempt") == 0:
                found[int(payload["beat"])] = str(payload.get("text") or "")
        return found


async def _report(ctx: CaseContext, operation_id: str) -> dict[str, Any]:
    async with ctx.engine.db.connect() as conn:
        found = await conn.scalar(
            select(operations.c.turn_report).where(operations.c.operation_id == operation_id)
        )
    return found if isinstance(found, dict) else {}


def interventions(report: Mapping[str, Any], final: str, verified: str) -> tuple[str, ...]:
    """The intervention types a turn report records (module docstring); ``verified`` is the
    deterministic assembly of the released beats."""
    found: list[str] = []
    rungs = sorted({int(e["rung"]) for e in report.get("repairs") or [] if "rung" in e})
    found += [f"repair_rung_{r}" for r in rungs]
    composition = report.get("composition") or {}
    degradation = report.get("degradation") or []
    if composition.get("mode") == "polished" and normalized(final) != normalized(verified):
        found.append("composition_polished")
    if COMPOSITION_FALLBACK in degradation:
        found.append("composition_fallback")
    if int((report.get("render") or {}).get("age_repairs") or 0) > 0:
        found.append("age_repair")
    return tuple(found)


async def first_draft(
    ctx: CaseContext, result: RespondResult, names: Mapping[str, str]
) -> tuple[str, str, tuple[str, ...]]:
    """The turn's first draft, the deterministic assembly of its released beats, and the
    intervention types that fired (module docstring)."""
    rendered = [b for b in result.beats if b.kind.startswith(RENDER_PREFIX) and b.beat is not None]
    operation_id = rendered[0].operation_id if rendered else result.message.message_id
    originals = await _rejected_originals(ctx, operation_id) if rendered else {}
    released = [
        AssemblyBeat(b.kind.removeprefix(RENDER_PREFIX), b.text, b.actor_character_id, b.beat or 0)
        for b in rendered
    ]
    raw = [
        AssemblyBeat(b.kind, originals.get(b.beat, b.text), b.character_id, b.beat)
        for b in released
    ]
    present = {b.beat for b in released}
    actor = released[0].character_id if released else None
    raw += [
        AssemblyBeat("do", text, actor, beat)
        for beat, text in originals.items()
        if beat not in present
    ]
    raw.sort(key=lambda b: b.beat)
    viewpoint = result.output_viewpoint
    draft = deterministic_assembly(raw, names, viewpoint)
    verified = deterministic_assembly(released, names, viewpoint)
    report = await _report(ctx, operation_id) if rendered else {}
    return draft, verified, interventions(report, result.message.text, verified)


async def play(ctx: CaseContext, case: ScenarioCase) -> Played:
    """Play ``case`` on a new scratch story (module docstring)."""
    created = await ctx.story(StorySpec(characters=list(CAST)))
    story_id = created.story_id
    await scenes.structured(ctx.engine, ctx.principal, story_id, CONFIG_DIALS[case.config])
    # The seeded profile routes perception (#181): the player reads the turn as the story does
    # (``player_view = story``: every beat in full, FR-RESP-13), and ``_plant`` places the cast.
    await ctx.engine.update_story_settings(ctx.principal, story_id, {"player_view": "story"})
    await _plant(ctx, story_id, [c.id for c in created.characters])
    await ctx.engine.set_custom_instructions(
        ctx.principal, story_id, CustomInstructionsRequest(text=drivers_text(case.drivers))
    )
    ids = {c.name: c.id for c in created.characters}
    names = {c.id: c.name for c in created.characters}
    played: list[PlayedTurn] = []
    for index, turn in enumerate(case.turns, start=1):
        request = RespondRequest.model_validate(
            {
                "actor_character_id": ids[PLAYER],
                "player_input": {"say": turn.say},
                "render_character_ids": [ids[RENDERER]],
                **({"prompt_params": {"length": case.length}} if case.length else {}),
            }
        )
        result = await ctx.engine.respond(ctx.principal, story_id, request)
        draft, _, types = await first_draft(ctx, result, names)
        played.append(
            PlayedTurn(
                index=index,
                say=turn.say,
                draft=draft,
                final=result.message.text,
                types=tuple(t for t in INTERVENTION_TYPES if t in types),
                fingerprint=result.fingerprint,
            )
        )
    return Played(story_id, case.config, tuple(played), (PLAYER, RENDERER), (*ITEMS, *PLACES))


def user_line(say: str) -> str:
    """The player's line as the render's user block carries it (unique to its own turn: the
    working memory renders earlier lines differently)."""
    return f'{PLAYER}: "{say}"'


def script(case: ScenarioCase, stub: StubAdapter) -> None:
    """Script the stub's render, regeneration and polish of each of ``case``'s turns."""
    marker = f"{EVAL_CASE_HEADER}: {case.id}"
    for turn in case.turns:
        line = user_line(turn.say)
        stub.when((marker, line), turn.stub.render, purpose=GENERATION.name)
        beats = json.loads(turn.stub.render).get("beats", [])
        texts = [str(b.get("text", "")) for b in beats if b.get("text")]
        if turn.stub.regeneration is not None:
            for text in texts:
                stub.when((marker, text), turn.stub.regeneration, purpose=REGENERATION.name)
        if turn.stub.composition is not None and texts:
            stub.when((marker, texts[0]), turn.stub.composition, purpose=COMPOSITION_TEMPLATE)


def changed_pairs(played: Sequence[PlayedTurn]) -> list[PlayedTurn]:
    """The turns whose final message differs from their first draft."""
    return [t for t in played if t.changed]


__all__ = [
    "CAST",
    "CONFIGS",
    "CONFIG_DIALS",
    "DEFINITION",
    "INTERVENTION_TYPES",
    "PLAYER",
    "RENDERER",
    "Config",
    "Drivers",
    "InterventionType",
    "Played",
    "PlayedTurn",
    "ScenarioCase",
    "ScenarioTurn",
    "changed_pairs",
    "drivers_text",
    "first_draft",
    "interventions",
    "play",
    "script",
    "user_line",
]
